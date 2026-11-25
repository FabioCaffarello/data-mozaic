package postgres

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Migrate applies ordered BC06 SQL files and rejects changed historical bytes.
// The caller must connect with the schema provisioning identity, never bc06_app.
func Migrate(ctx context.Context, dsn, directory string) error {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	if err = db.PingContext(ctx); err != nil {
		return err
	}
	if _, err = db.ExecContext(ctx, `SELECT pg_advisory_lock(8421770606)`); err != nil {
		return err
	}
	defer db.ExecContext(context.Background(), `SELECT pg_advisory_unlock(8421770606)`)
	files, err := filepath.Glob(filepath.Join(directory, "*.sql"))
	if err != nil {
		return err
	}
	sort.Strings(files)
	if len(files) == 0 {
		return fmt.Errorf("no BC06 migrations found")
	}
	for _, path := range files {
		version := filepath.Base(path)
		body, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(body)
		checksum := hex.EncodeToString(sum[:])
		if _, err = db.ExecContext(ctx, "CREATE SCHEMA IF NOT EXISTS artifact_custody"); err != nil {
			return err
		}
		if _, err = db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS artifact_custody.schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT clock_timestamp())`); err != nil {
			return err
		}
		var prior string
		err = db.QueryRowContext(ctx, `SELECT checksum FROM artifact_custody.schema_migrations WHERE version=$1`, version).Scan(&prior)
		if err == nil {
			if prior != checksum {
				return fmt.Errorf("migration checksum drift: %s", version)
			}
			continue
		}
		if err != sql.ErrNoRows {
			return err
		}
		tx, err := db.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, string(body)); err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("apply %s: %w", version, err)
		}
		if _, err = tx.ExecContext(ctx, `INSERT INTO artifact_custody.schema_migrations(version,checksum) VALUES($1,$2)`, version, checksum); err != nil {
			_ = tx.Rollback()
			return err
		}
		if err = tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

func ValidateDSN(dsn string) error {
	if strings.TrimSpace(dsn) == "" || strings.ContainsAny(dsn, "\r\n") {
		return fmt.Errorf("BC06 database DSN is required")
	}
	return nil
}
