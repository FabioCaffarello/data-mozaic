package application

import (
	"context"
	"io"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
)

type UploadClaim struct {
	Intent         domain.WriteIntent
	Generation     int64
	AttemptID      string
	LeaseToken     string
	ObjectKey      string
	LeaseUntil     time.Time
	Reconciliation bool
	Receipt        *domain.CustodyReceipt
}

type ObjectLocation struct {
	Provider  string
	Bucket    string
	Key       string
	VersionID string
}

type ObjectMetadata struct {
	Location  ObjectLocation
	Size      int64
	Digest    string
	MediaType string
}

type Reservation struct {
	Intent  domain.WriteIntent
	State   domain.ArtifactState
	Claim   *UploadClaim
	Receipt *domain.CustodyReceipt
}

type CustodyStore interface {
	ReserveIntent(context.Context, domain.WriteIntent, string) (Reservation, error)
	GetReservation(context.Context, string, string) (Reservation, error)
	ClaimUpload(context.Context, string, string, string, string, time.Duration) (UploadClaim, error)
	CompleteUpload(context.Context, UploadClaim, ObjectLocation) (domain.CustodyReceipt, error)
	ReleaseUpload(context.Context, UploadClaim, domain.ArtifactState) error
	VerifyArtifact(context.Context, string, string, string, int64, domain.SourceRef) (domain.CustodyReceipt, error)
	ReadArtifact(context.Context, string, string) (domain.ArtifactMetadata, error)
	ClaimReconciliation(context.Context, int, time.Duration) ([]UploadClaim, error)
	ListPendingEvents(context.Context, int) ([]OutboxEvent, error)
}

type ObjectStorage interface {
	PutVerified(context.Context, string, int64, string, string, io.Reader) (ObjectLocation, error)
	Head(context.Context, ObjectLocation) (ObjectMetadata, error)
	HeadKey(context.Context, string) (ObjectMetadata, error)
	Get(context.Context, ObjectLocation) (io.ReadCloser, error)
}

type AccessPolicy interface {
	Authorize(context.Context, Principal, string, domain.WriteIntent) error
}

type Principal struct {
	TenantID string
	Scopes   []string
}

type OutboxEvent struct {
	ContractVersion string             `json:"contract_version"`
	EventID         string             `json:"event_id"`
	EventSource     string             `json:"event_source"`
	EventType       string             `json:"event_type"`
	AggregateType   string             `json:"aggregate_type"`
	AggregateID     string             `json:"aggregate_id"`
	TenantID        string             `json:"tenant_id"`
	OccurredAt      string             `json:"occurred_at"`
	CorrelationID   string             `json:"correlation_id"`
	CausationID     string             `json:"causation_id"`
	ReceiptID       string             `json:"receipt_id"`
	Artifact        domain.ArtifactRef `json:"artifact"`
}
