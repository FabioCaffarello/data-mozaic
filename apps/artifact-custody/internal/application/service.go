package application

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
)

type Service struct {
	store       CustodyStore
	objects     ObjectStorage
	access      AccessPolicy
	maxBytes    int64
	lease       time.Duration
	uploadLimit time.Duration
}

func NewService(store CustodyStore, objects ObjectStorage, access AccessPolicy, maxBytes int64) (*Service, error) {
	if store == nil || objects == nil || access == nil || maxBytes < 1 || maxBytes > domain.DefaultMaxBytes {
		return nil, errors.New("invalid artifact custody service configuration")
	}
	return &Service{store: store, objects: objects, access: access, maxBytes: maxBytes, lease: 15 * time.Minute, uploadLimit: 10 * time.Minute}, nil
}

func (s *Service) CreateIntent(ctx context.Context, principal Principal, intent domain.WriteIntent) (Reservation, error) {
	if err := intent.Validate(s.maxBytes); err != nil {
		return Reservation{}, err
	}
	if principal.TenantID != intent.TenantID {
		return Reservation{}, errors.New("tenant scope mismatch")
	}
	if err := s.access.Authorize(ctx, principal, "artifact:write", intent); err != nil {
		return Reservation{}, err
	}
	identity, err := intent.Identity()
	if err != nil {
		return Reservation{}, err
	}
	return s.store.ReserveIntent(ctx, intent, identity.IntentDigest)
}

func (s *Service) Upload(ctx context.Context, principal Principal, tenantID, operationID string, body io.Reader, attemptID string) (domain.CustodyReceipt, error) {
	if principal.TenantID != tenantID {
		return domain.CustodyReceipt{}, errors.New("tenant scope mismatch")
	}
	reservation, err := s.store.GetReservation(ctx, tenantID, operationID)
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	if err := s.access.Authorize(ctx, principal, "artifact:write", reservation.Intent); err != nil {
		return domain.CustodyReceipt{}, err
	}
	identity, err := reservation.Intent.Identity()
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	claim, err := s.store.ClaimUpload(ctx, tenantID, operationID, identity.IntentDigest, attemptID, s.lease)
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	if claim.Receipt != nil {
		return *claim.Receipt, nil
	}
	if claim.LeaseToken == "" {
		return domain.CustodyReceipt{}, errors.New("upload claim has no lease token")
	}
	if claim.AttemptID != attemptID || claim.ObjectKey == "" {
		_ = s.store.ReleaseUpload(ctx, claim, domain.StatePending)
		return domain.CustodyReceipt{}, errors.New("upload claim identity mismatch")
	}
	uploadCtx, cancel := context.WithTimeout(ctx, s.uploadLimit)
	defer cancel()
	location, err := s.objects.PutVerified(uploadCtx, claim.ObjectKey, claim.Intent.Artifact.SizeBytes, claim.Intent.Artifact.Digest, claim.Intent.Artifact.MediaType, body)
	if err != nil {
		state := domain.StatePending
		if errors.Is(err, domain.ErrDigestMismatch) {
			state = domain.StateUnavailable
		}
		_ = s.store.ReleaseUpload(context.WithoutCancel(ctx), claim, state)
		return domain.CustodyReceipt{}, err
	}
	return s.store.CompleteUpload(uploadCtx, claim, location)
}

func (s *Service) Verify(ctx context.Context, principal Principal, tenantID, artifactID, digest string, size int64, source domain.SourceRef) (domain.CustodyReceipt, error) {
	if principal.TenantID != tenantID {
		return domain.CustodyReceipt{}, errors.New("tenant scope mismatch")
	}
	intent := domain.WriteIntent{TenantID: tenantID, ArtifactID: artifactID, Artifact: domain.ArtifactRef{ArtifactID: artifactID, Digest: digest, SizeBytes: size}, Source: source}
	if err := s.access.Authorize(ctx, principal, "artifact:verify", intent); err != nil {
		return domain.CustodyReceipt{}, err
	}
	return s.store.VerifyArtifact(ctx, tenantID, artifactID, digest, size, source)
}

func (s *Service) Metadata(ctx context.Context, principal Principal, tenantID, artifactID string) (domain.ArtifactMetadata, error) {
	if principal.TenantID != tenantID {
		return domain.ArtifactMetadata{}, errors.New("tenant scope mismatch")
	}
	intent := domain.WriteIntent{TenantID: tenantID, ArtifactID: artifactID}
	if err := s.access.Authorize(ctx, principal, "artifact:read", intent); err != nil {
		return domain.ArtifactMetadata{}, err
	}
	return s.store.ReadArtifact(ctx, tenantID, artifactID)
}

func (s *Service) Download(ctx context.Context, principal Principal, tenantID, artifactID string) (domain.ArtifactMetadata, io.ReadCloser, error) {
	metadata, err := s.Metadata(ctx, principal, tenantID, artifactID)
	if err != nil {
		return domain.ArtifactMetadata{}, nil, err
	}
	if metadata.State != domain.StateAvailable {
		return domain.ArtifactMetadata{}, nil, domain.ErrNotAvailable
	}
	location := ObjectLocation{Provider: metadata.Location.Provider, Bucket: metadata.Location.Bucket, Key: metadata.Location.Key, VersionID: metadata.Location.VersionID}
	if metadata.Artifact.SizeBytes < 0 || metadata.Artifact.SizeBytes > s.maxBytes {
		return domain.ArtifactMetadata{}, nil, errors.New("artifact metadata exceeds configured read limit")
	}
	object, err := s.objects.Head(ctx, location)
	if err != nil {
		return domain.ArtifactMetadata{}, nil, err
	}
	if object.Size != metadata.Artifact.SizeBytes || object.Digest != metadata.Artifact.Digest {
		return domain.ArtifactMetadata{}, nil, domain.ErrDigestMismatch
	}
	reader, err := s.objects.Get(ctx, location)
	if err != nil {
		return domain.ArtifactMetadata{}, nil, err
	}
	return metadata, reader, nil
}

// ReconcileExpiredUpload claims a finite set of expired upload leases and
// confirms only objects whose immutable location metadata matches the intent.
// Missing or divergent objects remain inspectable and are marked inconsistent.
func (s *Service) ReconcileExpiredUpload(ctx context.Context, limit int) (int, error) {
	claims, err := s.store.ClaimReconciliation(ctx, limit, s.lease)
	if err != nil {
		return 0, err
	}
	completed := 0
	for _, claim := range claims {
		metadata, headErr := s.objects.HeadKey(ctx, claim.ObjectKey)
		if headErr != nil {
			_ = s.store.ReleaseUpload(context.WithoutCancel(ctx), claim, domain.StatePending)
			continue
		}
		if metadata.Size != claim.Intent.Artifact.SizeBytes || metadata.Digest != claim.Intent.Artifact.Digest {
			_ = s.store.ReleaseUpload(context.WithoutCancel(ctx), claim, domain.StateInconsistent)
			continue
		}
		body, readErr := s.objects.Get(ctx, metadata.Location)
		if readErr != nil {
			_ = s.store.ReleaseUpload(context.WithoutCancel(ctx), claim, domain.StatePending)
			continue
		}
		actualSize, actualDigest, verifyErr := ValidateReadDigest(claim.Intent.Artifact.Digest, body, s.maxBytes)
		_ = body.Close()
		if verifyErr != nil || actualSize != claim.Intent.Artifact.SizeBytes || actualDigest != claim.Intent.Artifact.Digest {
			_ = s.store.ReleaseUpload(context.WithoutCancel(ctx), claim, domain.StateInconsistent)
			continue
		}
		if _, err = s.store.CompleteUpload(ctx, claim, metadata.Location); err != nil {
			return completed, err
		}
		completed++
	}
	return completed, nil
}

func (s *Service) PendingEvents(ctx context.Context, tenantID string, limit int) ([]OutboxEvent, error) {
	if tenantID == "" {
		return nil, errors.New("pending event tenant is required")
	}
	return s.store.ListPendingEvents(ctx, tenantID, limit)
}

func ReceiptID(tenantID, operationID, intentDigest string) string {
	digest := sha256.Sum256([]byte(tenantID + "\x00" + operationID + "\x00" + intentDigest))
	return "receipt-" + hex.EncodeToString(digest[:16])
}

func ValidateReadDigest(expected string, reader io.Reader, maxBytes int64) (int64, string, error) {
	hash := sha256.New()
	count, err := io.CopyN(hash, reader, maxBytes+1)
	if err != nil && !errors.Is(err, io.EOF) {
		return count, "", fmt.Errorf("read artifact stream: %w", err)
	}
	if count > maxBytes {
		return count, "", errors.New("artifact exceeds read limit")
	}
	digest := "sha256:" + hex.EncodeToString(hash.Sum(nil))
	if expected != "" && digest != expected {
		return count, digest, domain.ErrDigestMismatch
	}
	return count, digest, nil
}
