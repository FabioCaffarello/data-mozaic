package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

func main() {
	if err := run(); err != nil {
		slog.Error("local object provisioning failed", "error", err)
		os.Exit(1)
	}
}
func run() error {
	endpoint := os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT")
	bucket := os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET")
	rootKey := os.Getenv("RUSTFS_ROOT_ACCESS_KEY")
	rootSecret := os.Getenv("RUSTFS_ROOT_SECRET_KEY")
	appKey := os.Getenv("ARTIFACT_CUSTODY_S3_ACCESS_KEY")
	appSecret := os.Getenv("ARTIFACT_CUSTODY_S3_SECRET_KEY")
	region := env("AWS_REGION", "us-east-1")
	if endpoint == "" || bucket == "" || rootKey == "" || rootSecret == "" || appKey == "" || appSecret == "" {
		return errors.New("local object provisioning configuration incomplete")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	root := s3.New(s3.Options{Region: region, BaseEndpoint: aws.String(endpoint), UsePathStyle: true, Credentials: credentials.NewStaticCredentialsProvider(rootKey, rootSecret, "")})
	if _, err := root.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)}); err != nil {
		if _, createErr := root.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(bucket)}); createErr != nil && !strings.Contains(strings.ToLower(createErr.Error()), "already owned") {
			return fmt.Errorf("create local BC06 bucket: %w", createErr)
		}
	}
	app := s3.New(s3.Options{Region: region, BaseEndpoint: aws.String(endpoint), UsePathStyle: true, Credentials: credentials.NewStaticCredentialsProvider(appKey, appSecret, "")})
	if _, err := app.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(bucket)}); err == nil {
		return nil
	}
	policy := map[string]any{"Version": "2012-10-17", "Statement": []map[string]any{
		{"Effect": "Allow", "Action": []string{"s3:ListBucket"}, "Resource": []string{"arn:aws:s3:::" + bucket}},
		{"Effect": "Allow", "Action": []string{"s3:GetObject", "s3:PutObject"}, "Resource": []string{"arn:aws:s3:::" + bucket + "/bc06/*"}},
	}}
	body, _ := json.Marshal(map[string]any{"accessKey": appKey, "secretKey": appSecret, "name": "data-mozaic-bc06-local", "description": "Local BC06 application account", "policy": policy})
	parsed, err := url.Parse(strings.TrimRight(endpoint, "/") + "/rustfs/admin/v3/add-service-account")
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, parsed.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	hash := sha256.Sum256(body)
	payloadHash := hex.EncodeToString(hash[:])
	req.Header.Set("x-amz-content-sha256", payloadHash)
	credentialsValue := aws.Credentials{AccessKeyID: rootKey, SecretAccessKey: rootSecret, Source: "local-provision"}
	if err = v4.NewSigner().SignHTTP(ctx, credentialsValue, req, payloadHash, "s3", region, time.Now().UTC()); err != nil {
		return err
	}
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	payload, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("RustFS service-account provisioning returned HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(payload)))
	}
	return nil
}
func env(key, fallback string) string {
	if x := os.Getenv(key); x != "" {
		return x
	}
	return fallback
}
