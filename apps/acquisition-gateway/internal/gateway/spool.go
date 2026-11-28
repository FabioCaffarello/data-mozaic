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
const defaultEvidenceArchiveBytes = 16 << 20

func validEvidenceID(fetchID string) bool {
	if len(fetchID) != 32 {
		return false
	}
	_, err := hex.DecodeString(fetchID)
	return err == nil
}

func prepareSpool(root string, maxBytes int64, maxFiles int) (int64, int, error) {
	return prepareSpoolWithArchive(root, maxBytes, maxFiles, defaultEvidenceArchiveBytes)
}

func prepareSpoolWithArchive(root string, maxBytes int64, maxFiles int, maxArchiveBytes int64) (int64, int, error) {
	if root == "" || maxBytes < 1 || maxFiles < 1 || maxFiles > maxSpoolFiles {
		return 0, 0, errors.New("evidence_spool_configuration_invalid")
	}
	if err := os.MkdirAll(root, 0700); err != nil {
		return 0, 0, errors.New("evidence_spool_unavailable")
	}
	if err := os.MkdirAll(filepath.Join(root, "archive"), 0700); err != nil {
		return 0, 0, errors.New("evidence_archive_unavailable")
	}
	if err := reconcileEvidenceArchive(root); err != nil {
		return 0, 0, err
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
		return 0, 0, errors.New("evidence_spool_unsafe")
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) > maxFiles*2+1 {
		return 0, 0, errors.New("evidence_spool_capacity_exhausted")
	}
	bodies := map[string]int64{}
	metadata := map[string]struct{}{}
	for _, entry := range entries {
		name := entry.Name()
		if name == "archive" {
			continue
		}
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
			if _, metadataErr := readEvidenceMetadata(root, id); metadataErr != nil {
				if err := os.Remove(filepath.Join(root, id+".json")); err != nil {
					return 0, 0, errors.New("evidence_spool_recovery_failed")
				}
				continue
			}
			// A crash after archive durability and body removal can leave the
			// active metadata name behind. The archive is the release commit
			// record; remove this duplicate and restore active entry capacity.
			if archived, archiveErr := readEvidenceMetadataFrom(filepath.Join(root, "archive", id+".json"), id); archiveErr == nil {
				active, activeErr := readEvidenceMetadata(root, id)
				left, _ := json.Marshal(active)
				right, _ := json.Marshal(archived)
				if activeErr != nil || !bytes.Equal(left, right) {
					return 0, 0, errors.New("evidence_archive_conflict")
				}
				if removeErr := os.Remove(filepath.Join(root, id+".json")); removeErr != nil {
					return 0, 0, errors.New("evidence_spool_recovery_failed")
				}
				if syncErr := syncDirectory(root); syncErr != nil {
					return 0, 0, errors.New("evidence_spool_recovery_failed")
				}
				continue
			}
			// A valid metadata-only record has no recoverable body. Compact it
			// into the bounded historical archive while preserving exact identity.
			archivedBytes, archiveErr := archiveEvidenceMetadata(root, id, maxArchiveBytes)
			if archiveErr == nil {
				_ = archivedBytes
				continue
			}
			files += 2
		}
	}
	if files > maxFiles*2 || total > maxBytes {
		return 0, 0, errors.New("evidence_spool_capacity_exhausted")
	}
	return total, files, nil
}

func archiveEvidenceMetadata(root, id string, maxArchiveBytes int64) (int64, error) {
	if !validEvidenceID(id) {
		return 0, errors.New("evidence_id_invalid")
	}
	activePath := filepath.Join(root, id+".json")
	if _, err := readEvidenceMetadataFrom(activePath, id); err != nil {
		return 0, err
	}
	archivePath := filepath.Join(root, "archive", id+".json")
	if existing, err := readEvidenceMetadataFrom(archivePath, id); err == nil {
		active, activeErr := readEvidenceMetadataFrom(activePath, id)
		left, _ := json.Marshal(active)
		right, _ := json.Marshal(existing)
		if activeErr != nil || !bytes.Equal(left, right) {
			return 0, errors.New("evidence_archive_conflict")
		}
		if err := os.Remove(activePath); err != nil {
			return 0, err
		}
		return 0, syncDirectory(root)
	}
	raw, err := os.ReadFile(activePath)
	if err != nil {
		return 0, err
	}
	used, err := evidenceArchiveUsage(root)
	if err != nil || int64(len(raw)) > maxArchiveBytes-used {
		return 0, errors.New("evidence_archive_capacity_exhausted")
	}
	dir := filepath.Join(root, "archive")
	temp, err := os.CreateTemp(dir, ".archive-*")
	if err != nil {
		return 0, err
	}
	tempPath := temp.Name()
	writeErr := temp.Chmod(0600)
	if writeErr == nil {
		_, writeErr = temp.Write(raw)
	}
	if writeErr == nil {
		writeErr = temp.Sync()
	}
	if closeErr := temp.Close(); writeErr == nil {
		writeErr = closeErr
	}
	if writeErr == nil {
		writeErr = os.Rename(tempPath, archivePath)
	}
	_ = os.Remove(tempPath)
	if writeErr != nil {
		return 0, writeErr
	}
	if err := syncDirectory(dir); err != nil {
		return 0, err
	}
	if err := os.Remove(activePath); err != nil {
		return 0, err
	}
	if err := syncDirectory(root); err != nil {
		return int64(len(raw)), err
	}
	return int64(len(raw)), nil
}

func commitEvidence(root string, evidence Evidence, bodyPath string) error {
	if !validEvidenceID(evidence.FetchID) || evidence.BodySize < 0 || evidence.BodyRef != evidence.FetchID+".body" {
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
	evidence, err := readEvidenceMetadata(root, fetchID)
	if err != nil {
		return Evidence{}, nil, err
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

func readEvidenceMetadata(root, fetchID string) (Evidence, error) {
	if !validEvidenceID(fetchID) {
		return Evidence{}, errors.New("evidence_id_invalid")
	}
	metaPath := filepath.Join(root, fetchID+".json")
	if _, err := os.Lstat(metaPath); errors.Is(err, os.ErrNotExist) {
		metaPath = filepath.Join(root, "archive", fetchID+".json")
	}
	return readEvidenceMetadataFrom(metaPath, fetchID)
}

func readEvidenceMetadataFrom(metaPath, fetchID string) (Evidence, error) {
	if !validEvidenceID(fetchID) {
		return Evidence{}, errors.New("evidence_id_invalid")
	}
	info, err := os.Lstat(metaPath)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() > maxEvidenceMetadataBytes {
		return Evidence{}, errors.New("evidence_metadata_missing")
	}
	file, err := os.Open(metaPath)
	if err != nil {
		return Evidence{}, errors.New("evidence_metadata_missing")
	}
	defer file.Close()
	openedInfo, err := file.Stat()
	if err != nil || !os.SameFile(info, openedInfo) {
		return Evidence{}, errors.New("evidence_metadata_invalid")
	}
	metadataBytes, err := io.ReadAll(io.LimitReader(file, maxEvidenceMetadataBytes+1))
	if err != nil || len(metadataBytes) > maxEvidenceMetadataBytes || rejectDuplicateJSON(metadataBytes) != nil {
		return Evidence{}, errors.New("evidence_metadata_invalid")
	}
	var evidence Evidence
	decoder := json.NewDecoder(bytes.NewReader(metadataBytes))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&evidence) != nil || decoder.Decode(new(any)) != io.EOF || evidence.FetchID != fetchID || evidence.BodyRef != fetchID+".body" || evidence.TenantID == "" || evidence.RunID == "" || evidence.AttemptID == "" || evidence.Generation < 1 || !digestValue(evidence.BodyDigest) || evidence.BodySize < 0 || evidence.BodySize > maxEvidenceBodyBytes {
		return Evidence{}, errors.New("evidence_metadata_invalid")
	}
	return evidence, nil
}

// releaseEvidenceBody is idempotent. A missing body with valid immutable
// metadata represents an already completed release (including crash recovery
// after unlink and before in-memory accounting was updated).
func releaseEvidenceBody(root, fetchID string) (bool, error) {
	removed, _, err := releaseEvidenceBodyBounded(root, fetchID, 1<<62)
	return removed, err
}

func releaseEvidenceBodyBounded(root, fetchID string, archiveLimit int64) (bool, int64, error) {
	return releaseEvidenceBodyUsing(root, fetchID, archiveLimit, syncDirectory)
}

func releaseEvidenceBodyUsing(root, fetchID string, archiveLimit int64, syncDir func(string) error) (bool, int64, error) {
	evidence, err := readEvidenceMetadata(root, fetchID)
	if err != nil {
		return false, 0, err
	}
	bodyPath := filepath.Join(root, evidence.BodyRef)
	info, err := os.Lstat(bodyPath)
	if errors.Is(err, os.ErrNotExist) {
		// Finish a previously authorized release that stopped after body
		// unlink. Archive metadata is the durable release intent.
		archivePath := filepath.Join(root, "archive", fetchID+".json")
		if archived, archiveErr := readEvidenceMetadataFrom(archivePath, fetchID); archiveErr == nil {
			left, _ := json.Marshal(archived)
			right, _ := json.Marshal(evidence)
			if !bytes.Equal(left, right) {
				return false, 0, errors.New("evidence_archive_conflict")
			}
			if err := os.Remove(filepath.Join(root, fetchID+".json")); err != nil && !errors.Is(err, os.ErrNotExist) {
				return false, 0, errors.New("evidence_release_metadata_failed")
			}
			if err := syncDir(root); err != nil {
				return false, 0, errors.New("evidence_release_sync_failed")
			}
		}
		// An archived record with no body is an already completed release.
		return false, 0, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() != evidence.BodySize || info.Mode().Perm()&0077 != 0 {
		return false, 0, errors.New("evidence_body_invalid")
	}
	verified, body, err := openVerifiedEvidence(root, fetchID)
	if err != nil || verified.BodyDigest != evidence.BodyDigest {
		if body != nil {
			_ = body.Close()
		}
		return false, 0, errors.New("evidence_digest_mismatch")
	}
	_ = body.Close()
	metadata, err := json.Marshal(evidence)
	if err != nil {
		return false, 0, errors.New("evidence_archive_write_failed")
	}
	archiveDir := filepath.Join(root, "archive")
	if err := os.MkdirAll(archiveDir, 0700); err != nil {
		return false, 0, errors.New("evidence_archive_write_failed")
	}
	archivePath := filepath.Join(archiveDir, fetchID+".json")
	if existing, readErr := readEvidenceMetadataFrom(archivePath, fetchID); readErr == nil {
		left, _ := json.Marshal(existing)
		right, _ := json.Marshal(evidence)
		if !bytes.Equal(left, right) {
			return false, 0, errors.New("evidence_archive_conflict")
		}
	} else {
		used, usageErr := evidenceArchiveUsage(root)
		archiveRecordBytes := int64(len(metadata) + 1) // json.Encoder newline
		if usageErr != nil || archiveRecordBytes > archiveLimit-used {
			return false, 0, errors.New("evidence_archive_capacity_exhausted")
		}
		temp, createErr := os.CreateTemp(archiveDir, ".archive-*")
		if createErr != nil {
			return false, 0, errors.New("evidence_archive_write_failed")
		}
		tempPath := temp.Name()
		writeErr := temp.Chmod(0600)
		if writeErr == nil {
			_, writeErr = temp.Write(append(metadata, '\n'))
		}
		if writeErr == nil {
			writeErr = temp.Sync()
		}
		if closeErr := temp.Close(); writeErr == nil {
			writeErr = closeErr
		}
		if writeErr == nil {
			writeErr = os.Rename(tempPath, archivePath)
		}
		_ = os.Remove(tempPath)
		if writeErr != nil {
			return false, 0, errors.New("evidence_archive_write_failed")
		}
		if err := syncDir(archiveDir); err != nil {
			return false, 0, errors.New("evidence_archive_sync_failed")
		}
	}
	if err := os.Remove(bodyPath); err != nil {
		return false, 0, errors.New("evidence_body_release_failed")
	}
	if err := syncDir(root); err != nil {
		return true, 0, errors.New("evidence_release_sync_failed")
	}
	if err := os.Remove(filepath.Join(root, fetchID+".json")); err != nil && !errors.Is(err, os.ErrNotExist) {
		return true, 0, errors.New("evidence_release_metadata_failed")
	}
	if err := syncDir(root); err != nil {
		return true, int64(len(metadata)) + 1, errors.New("evidence_release_sync_failed")
	}
	return true, int64(len(metadata)) + 1, nil
}

func syncDirectory(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func evidenceArchiveUsage(root string) (int64, error) {
	dir := filepath.Join(root, "archive")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return 0, errors.New("evidence_archive_unavailable")
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
		return 0, errors.New("evidence_archive_unsafe")
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) > maxSpoolFiles {
		return 0, errors.New("evidence_archive_capacity_exhausted")
	}
	var used int64
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".archive-") {
			if err := os.Remove(filepath.Join(dir, name)); err != nil {
				return 0, errors.New("evidence_archive_recovery_failed")
			}
			continue
		}
		if !strings.HasSuffix(name, ".json") {
			return 0, errors.New("evidence_archive_membership_invalid")
		}
		id := strings.TrimSuffix(name, ".json")
		if !validEvidenceID(id) {
			return 0, errors.New("evidence_archive_membership_invalid")
		}
		path := filepath.Join(dir, name)
		fileInfo, statErr := os.Lstat(path)
		if statErr != nil || !fileInfo.Mode().IsRegular() || fileInfo.Mode()&os.ModeSymlink != 0 || fileInfo.Mode().Perm()&0077 != 0 || fileInfo.Size() < 1 || fileInfo.Size() > maxEvidenceMetadataBytes {
			return 0, errors.New("evidence_archive_record_invalid")
		}
		if _, readErr := readEvidenceMetadataFrom(path, id); readErr != nil {
			return 0, errors.New("evidence_archive_record_invalid")
		}
		used += fileInfo.Size()
	}
	return used, nil
}

func reconcileEvidenceArchive(root string) error {
	dir := filepath.Join(root, "archive")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return errors.New("evidence_archive_unavailable")
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".archive-") {
			if err := os.Remove(filepath.Join(dir, entry.Name())); err != nil {
				return errors.New("evidence_archive_recovery_failed")
			}
			continue
		}
		if !strings.HasSuffix(entry.Name(), ".json") {
			return errors.New("evidence_archive_membership_invalid")
		}
		id := strings.TrimSuffix(entry.Name(), ".json")
		archived, err := readEvidenceMetadataFrom(filepath.Join(dir, entry.Name()), id)
		if err != nil {
			return errors.New("evidence_archive_record_invalid")
		}
		activePath := filepath.Join(root, id+".json")
		if _, statErr := os.Lstat(activePath); statErr == nil {
			active, readErr := readEvidenceMetadataFrom(activePath, id)
			left, _ := json.Marshal(active)
			right, _ := json.Marshal(archived)
			if readErr != nil || !bytes.Equal(left, right) {
				return errors.New("evidence_archive_conflict")
			}
			if _, _, err := releaseEvidenceBodyBounded(root, id, 1<<62); err != nil {
				return errors.New("evidence_archive_recovery_failed")
			}
		} else if !errors.Is(statErr, os.ErrNotExist) {
			return errors.New("evidence_archive_recovery_failed")
		}
	}
	return nil
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
