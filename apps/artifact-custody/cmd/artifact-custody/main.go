package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/httpapi"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/postgres"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/s3store"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		if err := healthcheck(); err != nil {
			os.Exit(1)
		}
		return
	}
	if err := run(); err != nil {
		slog.Error("artifact custody stopped", "error", err)
		os.Exit(1)
	}
}
func healthcheck() error {
	client := http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://127.0.0.1:8086/healthz")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return errors.New("health endpoint is not ready")
	}
	return nil
}
func run() error {
	mode := os.Getenv("ARTIFACT_CUSTODY_MODE")
	if mode != "local" && mode != "test" {
		return errors.New("ARTIFACT_CUSTODY_MODE must be local or test; production authorization is not implemented")
	}
	token := os.Getenv("ARTIFACT_CUSTODY_LOCAL_TOKEN")
	databaseURL := os.Getenv("ARTIFACT_CUSTODY_DATABASE_URL")
	bucket := os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET")
	temp := os.Getenv("ARTIFACT_CUSTODY_TEMP_DIR")
	maxBytes := int64(100 << 20)
	if raw := os.Getenv("ARTIFACT_CUSTODY_MAX_BYTES"); raw != "" {
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return err
		}
		maxBytes = n
	}
	concurrency := 4
	if raw := os.Getenv("ARTIFACT_CUSTODY_MAX_CONCURRENT"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > 64 {
			return errors.New("ARTIFACT_CUSTODY_MAX_CONCURRENT must be between 1 and 64")
		}
		concurrency = n
	}
	tempBudget := int64(420 << 20)
	if raw := os.Getenv("ARTIFACT_CUSTODY_TEMP_BUDGET_BYTES"); raw != "" {
		n, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || n < 1 {
			return errors.New("ARTIFACT_CUSTODY_TEMP_BUDGET_BYTES must be a positive integer")
		}
		tempBudget = n
	}
	if err := validateTransferBudget(maxBytes, concurrency, tempBudget); err != nil {
		return err
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	store, err := postgres.Open(ctx, databaseURL, 16)
	if err != nil {
		return err
	}
	defer store.Close()
	objects, err := s3store.New(ctx, s3store.Config{Region: envOr("AWS_REGION", "us-east-1"), Endpoint: os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT"), Bucket: bucket, TempDir: temp, MaxBytes: maxBytes, PathStyle: true})
	if err != nil {
		return err
	}
	service, err := application.NewService(store, objects, httpapi.LocalPolicy{}, maxBytes)
	if err != nil {
		return err
	}
	handler, err := httpapi.NewServer(service, mode, token, maxBytes, concurrency)
	if err != nil {
		return err
	}
	address := envOr("ARTIFACT_CUSTODY_LISTEN", ":8086")
	server := httpapi.NewHTTPServer(handler, address)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	slog.Info("BC06 local/test API listening", "address", address, "max_bytes", maxBytes, "max_concurrent", concurrency)
	if err = server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}
func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func validateTransferBudget(maxBytes int64, concurrency int, tempBudget int64) error {
	if maxBytes < 1 || maxBytes > 100<<20 || concurrency < 1 || concurrency > 64 || tempBudget < 1 {
		return errors.New("artifact transfer limits are outside supported bounds")
	}
	if int64(concurrency) > tempBudget/maxBytes {
		return errors.New("aggregate upload spool budget is below max_bytes multiplied by max_concurrent")
	}
	return nil
}
