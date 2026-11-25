package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/postgres"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	dsn := os.Getenv("ARTIFACT_CUSTODY_MIGRATOR_DATABASE_URL")
	if dsn == "" {
		slog.Error("ARTIFACT_CUSTODY_MIGRATOR_DATABASE_URL is required")
		os.Exit(2)
	}
	directory := os.Getenv("ARTIFACT_CUSTODY_MIGRATIONS_DIR")
	if directory == "" {
		directory = "migrations"
	}
	if err := postgres.Migrate(ctx, dsn, directory); err != nil {
		slog.Error("BC06 migration failed", "error", err)
		os.Exit(1)
	}
	slog.Info("BC06 migrations current")
}
