package gateway

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const maxSpoolFiles = 10000
const maxEvidenceMetadataBytes = 64 << 10
const maxEvidenceBodyBytes = 100_000_000

func prepareSpool(root string, maxBytes int64, maxFiles int) (int64, int, error) {
	if root == "" || maxBytes < 1 || maxFiles < 1 || maxFiles > maxSpoolFiles {
		return 0, 0, errors.New("evidence_spool_configuration_invalid")
	}
	if err := os.MkdirAll(root, 0700); err != nil {
		return 0, 0, errors.New("evidence_spool_unavailable")
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
		return 0, 0, errors.New("evidence_spool_unsafe")
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) > maxFiles*2 {
		return 0, 0, errors.New("evidence_spool_capacity_exhausted")
	}
	bodies := map[string]int64{}
	metadata := map[string]struct{}{}
	for _, entry := range entries {
		name := entry.Name()
		path := filepath.Join(root, name)
		fileInfo, statErr := os.Lstat(path)
		if statErr != nil || !fileInfo.Mode().IsRegular() || fileInfo.Mode()&os.ModeSymlink != 0 || fileInfo.Mode().Perm()&0077 != 0 {
			return 0, 0, errors.New("evidence_spool_file_unsafe")
		}
		if strings.HasPrefix(name, ".fetch-") && strings.HasSuffix(name, ".part") || strings.HasPrefix(name, ".evidence-meta-") {
			if err := os.Remove(path); err != nil {
				return 0, 0, errors.New("evidence_spool_recovery_failed")
			}
			continue
		}
		if !strings.HasSuffix(name, ".body") && !strings.HasSuffix(name, ".json") {
			return 0, 0, errors.New("evidence_spool_membership_invalid")
		}
		if strings.HasSuffix(name, ".body") {
			if fileInfo.Size() < 0 {
				return 0, 0, errors.New("evidence_spool_capacity_exhausted")
			}
			bodies[strings.TrimSuffix(name, ".body")] = fileInfo.Size()
		} else {
			metadata[strings.TrimSuffix(name, ".json")] = struct{}{}
		}
	}
	var total int64
	files := 0
	for id, size := range bodies {
		if _, committed := metadata[id]; !committed {
			if err := os.Remove(filepath.Join(root, id+".body")); err != nil {
				return 0, 0, errors.New("evidence_spool_recovery_failed")
			}
			continue
		}
		if size > maxBytes-total {
			return 0, 0, errors.New("evidence_spool_capacity_exhausted")
		}
		total += size
		files += 2
	}
	for id := range metadata {
		if _, bodyExists := bodies[id]; !bodyExists {
			if err := os.Remove(filepath.Join(root, id+".json")); err != nil {
				return 0, 0, errors.New("evidence_spool_recovery_failed")
			}
		}
	}
	if files > maxFiles*2 || total > maxBytes {
		return 0, 0, errors.New("evidence_spool_capacity_exhausted")
	}
	return total, files, nil
}

func commitEvidence(root string, evidence Evidence, bodyPath string) error {
	if len(evidence.FetchID) != 32 || evidence.BodySize < 0 || evidence.BodyRef != evidence.FetchID+".body" {
		return errors.New("evidence_identity_invalid")
	}
	finalBodyPath := filepath.Join(root, evidence.BodyRef)
	if fileExists(finalBodyPath) {
		return errors.New("evidence_body_write_failed")
	}
	bodyFile, err := os.OpenFile(bodyPath, os.O_RDWR, 0600)
	if err != nil {
		return errors.New("evidence_body_write_failed")
	}
	info, err := bodyFile.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() != evidence.BodySize || info.Mode().Perm()&0077 != 0 {
		_ = bodyFile.Close()
		return errors.New("evidence_body_write_failed")
	}
	if err = bodyFile.Sync(); err != nil {
		_ = bodyFile.Close()
		return errors.New("evidence_body_write_failed")
	}
	if err = bodyFile.Close(); err != nil {
		return errors.New("evidence_body_write_failed")
	}
	if err = os.Rename(bodyPath, finalBodyPath); err != nil {
		return errors.New("evidence_body_write_failed")
	}
	metaPath := filepath.Join(root, evidence.FetchID+".json")
	metaTemp, err := os.CreateTemp(root, ".evidence-meta-*")
	if err != nil {
		_ = os.Remove(finalBodyPath)
		return errors.New("evidence_metadata_write_failed")
	}
	tempPath := metaTemp.Name()
	defer os.Remove(tempPath)
	if err = metaTemp.Chmod(0600); err == nil {
		err = json.NewEncoder(metaTemp).Encode(evidence)
	}
	if err == nil {
		err = metaTemp.Sync()
	}
	if closeErr := metaTemp.Close(); err == nil {
		err = closeErr
	}
	if err != nil || fileExists(metaPath) {
		_ = os.Remove(finalBodyPath)
		return errors.New("evidence_metadata_write_failed")
	}
	if err = os.Rename(tempPath, metaPath); err != nil {
		_ = os.Remove(finalBodyPath)
		return errors.New("evidence_metadata_write_failed")
	}
	dir, err := os.Open(root)
	if err != nil {
		_ = os.Remove(metaPath)
		_ = os.Remove(finalBodyPath)
		return errors.New("evidence_metadata_write_failed")
	}
	defer dir.Close()
	if err = dir.Sync(); err != nil {
		_ = os.Remove(metaPath)
		_ = os.Remove(finalBodyPath)
		return errors.New("evidence_metadata_write_failed")
	}
	return nil
}

func fileExists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil || !errors.Is(err, os.ErrNotExist)
}

func openVerifiedEvidence(root, fetchID string) (Evidence, *os.File, error) {
	if len(fetchID) != 32 {
		return Evidence{}, nil, errors.New("evidence_id_invalid")
	}
	metaPath := filepath.Join(root, fetchID+".json")
	info, err := os.Lstat(metaPath)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() > maxEvidenceMetadataBytes {
		return Evidence{}, nil, errors.New("evidence_metadata_missing")
	}
	file, err := os.Open(metaPath)
	if err != nil {
		return Evidence{}, nil, errors.New("evidence_metadata_missing")
	}
	defer file.Close()
	openedInfo, err := file.Stat()
	if err != nil || !os.SameFile(info, openedInfo) {
		return Evidence{}, nil, errors.New("evidence_metadata_invalid")
	}
	metadataBytes, err := io.ReadAll(io.LimitReader(file, maxEvidenceMetadataBytes+1))
	if err != nil || len(metadataBytes) > maxEvidenceMetadataBytes || rejectDuplicateJSON(metadataBytes) != nil {
		return Evidence{}, nil, errors.New("evidence_metadata_invalid")
	}
	var evidence Evidence
	decoder := json.NewDecoder(bytes.NewReader(metadataBytes))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&evidence) != nil || decoder.Decode(new(any)) != io.EOF || evidence.FetchID != fetchID || evidence.BodyRef != fetchID+".body" || evidence.BodySize < 0 || evidence.BodySize > maxEvidenceBodyBytes {
		return Evidence{}, nil, errors.New("evidence_metadata_invalid")
	}
	bodyPath := filepath.Join(root, evidence.BodyRef)
	bodyInfo, err := os.Lstat(bodyPath)
	if err != nil || !bodyInfo.Mode().IsRegular() || bodyInfo.Mode()&os.ModeSymlink != 0 || bodyInfo.Size() != evidence.BodySize {
		return Evidence{}, nil, errors.New("evidence_body_invalid")
	}
	bodyFile, err := os.Open(bodyPath)
	if err != nil {
		return Evidence{}, nil, errors.New("evidence_body_invalid")
	}
	openedBodyInfo, err := bodyFile.Stat()
	if err != nil || !os.SameFile(bodyInfo, openedBodyInfo) || bodyInfo.Size() > maxEvidenceBodyBytes {
		_ = bodyFile.Close()
		return Evidence{}, nil, errors.New("evidence_body_invalid")
	}
	hash := sha256.New()
	count, err := io.CopyBuffer(hash, io.LimitReader(bodyFile, evidence.BodySize+1), make([]byte, 64<<10))
	if err != nil || count != evidence.BodySize || "sha256:"+hex.EncodeToString(hash.Sum(nil)) != evidence.BodyDigest {
		_ = bodyFile.Close()
		return Evidence{}, nil, errors.New("evidence_digest_mismatch")
	}
	if _, err := bodyFile.Seek(0, io.SeekStart); err != nil {
		_ = bodyFile.Close()
		return Evidence{}, nil, fmt.Errorf("evidence_body_seek_failed: %w", err)
	}
	return evidence, bodyFile, nil
}

func readEvidence(root, fetchID string) (Evidence, []byte, error) {
	evidence, bodyFile, err := openVerifiedEvidence(root, fetchID)
	if err != nil {
		return Evidence{}, nil, err
	}
	defer bodyFile.Close()
	body, err := io.ReadAll(io.LimitReader(bodyFile, evidence.BodySize+1))
	if err != nil || int64(len(body)) != evidence.BodySize {
		return Evidence{}, nil, errors.New("evidence_body_invalid")
	}
	return evidence, body, nil
}
