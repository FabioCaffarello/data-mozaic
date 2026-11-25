package s3store

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

const copyBufferBytes = 64 << 10

type Store struct {
	client   *s3.Client
	bucket   string
	tmpDir   string
	maxBytes int64
}

type Config struct {
	Region, Endpoint, Bucket, TempDir string
	MaxBytes                          int64
	PathStyle                         bool
}

func New(ctx context.Context, cfg Config) (*Store, error) {
	if cfg.Region == "" || cfg.Bucket == "" || cfg.MaxBytes < 1 || cfg.MaxBytes > domain.DefaultMaxBytes || cfg.TempDir == "" {
		return nil, errors.New("invalid BC06 object storage configuration")
	}
	if err := os.MkdirAll(cfg.TempDir, 0700); err != nil {
		return nil, fmt.Errorf("create bounded upload temp directory: %w", err)
	}
	if err := ValidateTempDir(cfg.TempDir); err != nil {
		return nil, err
	}
	awsCfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(cfg.Region))
	if err != nil {
		return nil, fmt.Errorf("load S3 credentials: %w", err)
	}
	client := s3.NewFromConfig(awsCfg, func(options *s3.Options) {
		options.UsePathStyle = cfg.PathStyle
		if cfg.Endpoint != "" {
			options.BaseEndpoint = aws.String(cfg.Endpoint)
		}
	})
	return &Store{client: client, bucket: cfg.Bucket, tmpDir: cfg.TempDir, maxBytes: cfg.MaxBytes}, nil
}

// PutVerified first streams to a private bounded temporary file while checking
// exact size and SHA-256, then performs one conditional immutable S3 PutObject.
// The configured cap is <=100 MiB, far below the S3 single-part 5 GiB limit;
// multipart is deliberately unsupported until a larger cap has a consumer.
func (s *Store) PutVerified(ctx context.Context, key string, expectedSize int64, expectedDigest, mediaType string, source io.Reader) (application.ObjectLocation, error) {
	if expectedSize < 0 || expectedSize > s.maxBytes || !strings.HasPrefix(key, "bc06/") {
		return application.ObjectLocation{}, errors.New("object request exceeds configured bounds")
	}
	f, err := os.CreateTemp(s.tmpDir, "bc06-upload-*.part")
	if err != nil {
		return application.ObjectLocation{}, fmt.Errorf("create upload spool: %w", err)
	}
	path := f.Name()
	defer os.Remove(path)
	defer f.Close()
	hash := sha256.New()
	limited := io.LimitReader(source, s.maxBytes+1)
	count, err := io.CopyBuffer(io.MultiWriter(f, hash), limited, make([]byte, copyBufferBytes))
	if err != nil {
		return application.ObjectLocation{}, fmt.Errorf("spool upload: %w", err)
	}
	if count != expectedSize || count > s.maxBytes {
		return application.ObjectLocation{}, domain.ErrDigestMismatch
	}
	digest := "sha256:" + hex.EncodeToString(hash.Sum(nil))
	if digest != expectedDigest {
		return application.ObjectLocation{}, domain.ErrDigestMismatch
	}
	if err = f.Sync(); err != nil {
		return application.ObjectLocation{}, fmt.Errorf("sync upload spool: %w", err)
	}
	if _, err = f.Seek(0, io.SeekStart); err != nil {
		return application.ObjectLocation{}, err
	}
	input := &s3.PutObjectInput{Bucket: aws.String(s.bucket), Key: aws.String(key), Body: f, ContentLength: aws.Int64(count), ChecksumSHA256: aws.String(base64.StdEncoding.EncodeToString(hash.Sum(nil))), Metadata: map[string]string{"sha256": digest, "size-bytes": fmt.Sprint(count)}, IfNoneMatch: aws.String("*")}
	if mediaType != "" {
		input.ContentType = aws.String(mediaType)
	}
	output, err := s.client.PutObject(ctx, input)
	if err != nil {
		// A lost response can leave a committed immutable object. Verify by the
		// exact attempt key before surfacing an ambiguous write failure.
		if stored, headErr := s.HeadKey(ctx, key); headErr == nil && stored.Size == count && stored.Digest == digest {
			if body, getErr := s.Get(ctx, stored.Location); getErr == nil {
				actual, hashErr := sha256Of(body, s.maxBytes)
				_ = body.Close()
				if hashErr == nil && actual == digest {
					return stored.Location, nil
				}
			}
		}
		var apiError interface{ ErrorCode() string }
		if errors.As(err, &apiError) {
			slog.Error("BC06 object PUT failed", "code", apiError.ErrorCode())
		} else {
			slog.Error("BC06 object PUT failed", "error_type", fmt.Sprintf("%T", err))
		}
		return application.ObjectLocation{}, fmt.Errorf("write immutable S3 object: %w", err)
	}
	return application.ObjectLocation{Provider: "s3", Bucket: s.bucket, Key: key, VersionID: aws.ToString(output.VersionId)}, nil
}

func sha256Of(reader io.Reader, maxBytes int64) (string, error) {
	hash := sha256.New()
	count, err := io.CopyBuffer(hash, io.LimitReader(reader, maxBytes+1), make([]byte, copyBufferBytes))
	if err != nil {
		return "", err
	}
	if count > maxBytes {
		return "", errors.New("stored object exceeds configured limit")
	}
	return "sha256:" + hex.EncodeToString(hash.Sum(nil)), nil
}

func (s *Store) Head(ctx context.Context, loc application.ObjectLocation) (application.ObjectMetadata, error) {
	if loc.Provider != "s3" || loc.Bucket != s.bucket || loc.Key == "" {
		return application.ObjectMetadata{}, errors.New("object location outside configured bucket")
	}
	out, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(loc.Bucket), Key: aws.String(loc.Key), VersionId: optional(loc.VersionID), ChecksumMode: "ENABLED"})
	if err != nil {
		return application.ObjectMetadata{}, err
	}
	digest := out.Metadata["sha256"]
	resolved := loc
	resolved.VersionID = aws.ToString(out.VersionId)
	return application.ObjectMetadata{Location: resolved, Size: aws.ToInt64(out.ContentLength), Digest: digest, MediaType: aws.ToString(out.ContentType)}, nil
}
func (s *Store) HeadKey(ctx context.Context, key string) (application.ObjectMetadata, error) {
	return s.Head(ctx, application.ObjectLocation{Provider: "s3", Bucket: s.bucket, Key: key})
}
func (s *Store) Get(ctx context.Context, loc application.ObjectLocation) (io.ReadCloser, error) {
	if loc.Provider != "s3" || loc.Bucket != s.bucket || loc.Key == "" {
		return nil, errors.New("object location outside configured bucket")
	}
	out, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(loc.Bucket), Key: aws.String(loc.Key), VersionId: optional(loc.VersionID)})
	if err != nil {
		return nil, err
	}
	return out.Body, nil
}
func optional(value string) *string {
	if value == "" {
		return nil
	}
	return aws.String(value)
}

func ValidateTempDir(path string) error {
	abs, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	info, err := os.Stat(abs)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return errors.New("temporary upload path is not a directory")
	}
	if info.Mode().Perm()&0077 != 0 {
		return errors.New("temporary upload directory must not be accessible by group or others")
	}
	return nil
}
