package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

const maxLedgerBytes = 16 << 20
const maxLedgerRuns = 10000

type ledgerSnapshot map[string]budget

func openLedger(path string) (ledgerSnapshot, error) {
	if path == "" {
		return nil, errors.New("quota_state_path_required")
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, errors.New("quota_state_unavailable")
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 {
		return nil, errors.New("quota_state_directory_unsafe")
	}
	info, err = os.Lstat(path)
	if os.IsNotExist(err) {
		return ledgerSnapshot{}, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 || info.Size() > maxLedgerBytes {
		return nil, errors.New("quota_state_file_unsafe")
	}
	data, err := os.ReadFile(path)
	if err != nil || rejectDuplicateJSON(data) != nil {
		return nil, errors.New("quota_state_corrupt")
	}
	var state ledgerSnapshot
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&state) != nil || state == nil || len(state) > maxLedgerRuns {
		return nil, errors.New("quota_state_corrupt")
	}
	for key, entry := range state {
		if key == "" || entry.Requests < 0 || entry.Bytes < 0 || entry.Reserved < 0 {
			return nil, errors.New("quota_state_corrupt")
		}
		// A request that was in flight during process failure consumes its full
		// reserved response allowance. Restart never returns that budget.
		entry.Bytes += entry.Reserved
		entry.Reserved = 0
		state[key] = entry
	}
	return state, nil
}

func persistLedger(path string, state ledgerSnapshot) error {
	data, err := json.Marshal(state)
	if err != nil || len(data) > maxLedgerBytes || len(state) > maxLedgerRuns {
		return errors.New("quota_state_capacity_exhausted")
	}
	dir := filepath.Dir(path)
	temp, err := os.CreateTemp(dir, ".quota-state-*")
	if err != nil {
		return errors.New("quota_state_unavailable")
	}
	name := temp.Name()
	defer os.Remove(name)
	if err = temp.Chmod(0600); err != nil {
		temp.Close()
		return errors.New("quota_state_unavailable")
	}
	if _, err = temp.Write(data); err != nil {
		temp.Close()
		return errors.New("quota_state_unavailable")
	}
	if err = temp.Sync(); err != nil {
		temp.Close()
		return errors.New("quota_state_unavailable")
	}
	if err = temp.Close(); err != nil {
		return errors.New("quota_state_unavailable")
	}
	if err = os.Rename(name, path); err != nil {
		return errors.New("quota_state_unavailable")
	}
	folder, err := os.Open(dir)
	if err != nil {
		return errors.New("quota_state_unavailable")
	}
	defer folder.Close()
	if err = folder.Sync(); err != nil {
		return errors.New("quota_state_unavailable")
	}
	return nil
}
