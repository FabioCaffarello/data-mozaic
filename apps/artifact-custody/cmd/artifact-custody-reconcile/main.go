package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/httpapi"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/postgres"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/s3store"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx); err != nil {
		slog.Error("BC06 reconciliation failed", "error_type", fmt.Sprintf("%T", err))
		os.Exit(1)
	}
}
func run(ctx context.Context) error {
	maxBytes := int64(100 << 20)
	store, err := postgres.Open(ctx, os.Getenv("ARTIFACT_CUSTODY_DATABASE_URL"), 8)
	if err != nil {
		return err
	}
	defer store.Close()
	objects, err := s3store.New(ctx, s3store.Config{Region: os.Getenv("AWS_REGION"), Endpoint: os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT"), Bucket: os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET"), TempDir: os.Getenv("ARTIFACT_CUSTODY_TEMP_DIR"), MaxBytes: maxBytes, PathStyle: true})
	if err != nil {
		return err
	}
	service, err := application.NewService(store, objects, httpapi.LocalPolicy{}, maxBytes)
	if err != nil {
		return err
	}
	limit := 25
	if raw := os.Getenv("ARTIFACT_CUSTODY_RECONCILE_LIMIT"); raw != "" {
		limit, err = strconv.Atoi(raw)
		if err != nil {
			return err
		}
	}
	completed, err := service.ReconcileExpiredUpload(ctx, limit)
	if err != nil {
		return err
	}
	slog.Info("BC06 reconciliation batch finished", "claimed_limit", limit, "confirmed", completed, "remaining_requires_inspection", true)
	return nil
}
