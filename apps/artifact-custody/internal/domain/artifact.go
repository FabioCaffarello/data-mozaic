package domain

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	ContractVersion        = "artifact-custody.v1"
	DefaultMaxBytes  int64 = 100 << 20
	MaxMetadataBytes       = 8 << 10
)

var identifierPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

type Classification string

const (
	ClassificationPublic       Classification = "public"
	ClassificationInternal     Classification = "internal"
	ClassificationConfidential Classification = "confidential"
	ClassificationRestricted   Classification = "restricted"
)

type Representation string

const (
	RepresentationDecodedHTTPBody   Representation = "decoded_http_entity"
	RepresentationCapturedHTTPWire  Representation = "captured_http_wire_bytes"
	RepresentationDownloadedFile    Representation = "downloaded_file_bytes"
	RepresentationBrowserScreenshot Representation = "browser_screenshot"
	RepresentationBrowserDOM        Representation = "browser_dom"
	RepresentationOtherDeclared     Representation = "other_declared"
)

type SourceRef struct {
	SourceID string `json:"source_id"`
	Revision string `json:"revision"`
}

type ArtifactRef struct {
	ArtifactID       string `json:"artifact_id"`
	Digest           string `json:"digest"`
	SizeBytes        int64  `json:"size_bytes"`
	MediaType        string `json:"media_type,omitempty"`
	MediaTypePresent bool   `json:"-"`
}

func (artifact *ArtifactRef) UnmarshalJSON(raw []byte) error {
	type wire ArtifactRef
	var value wire
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	*artifact = ArtifactRef(value)
	_, artifact.MediaTypePresent = fields["media_type"]
	return nil
}

type WriteIntent struct {
	ContractVersion    string            `json:"contract_version"`
	OperationID        string            `json:"operation_id"`
	TenantID           string            `json:"tenant_id"`
	ArtifactID         string            `json:"artifact_id"`
	Source             SourceRef         `json:"source"`
	Artifact           ArtifactRef       `json:"artifact"`
	Representation     Representation    `json:"representation"`
	Classification     Classification    `json:"classification"`
	RetentionPolicyRef string            `json:"retention_policy_ref"`
	Metadata           map[string]string `json:"metadata,omitempty"`
}

// MarshalJSON preserves omitted metadata versus an explicitly empty object.
func (intent WriteIntent) MarshalJSON() ([]byte, error) {
	value := map[string]any{"contract_version": intent.ContractVersion, "operation_id": intent.OperationID, "tenant_id": intent.TenantID, "artifact_id": intent.ArtifactID, "source": intent.Source, "artifact": intent.Artifact, "representation": intent.Representation, "classification": intent.Classification, "retention_policy_ref": intent.RetentionPolicyRef}
	if intent.Metadata != nil {
		value["metadata"] = intent.Metadata
	}
	return json.Marshal(value)
}

type IntentIdentity struct {
	TenantID     string
	OperationID  string
	IntentDigest string
}

type UploadAttempt struct {
	AttemptID  string
	Generation int64
	LeaseToken string
	LeaseUntil string
}

func (attempt UploadAttempt) Validate() error {
	if !identifierPattern.MatchString(attempt.AttemptID) || attempt.Generation < 1 || len(attempt.LeaseToken) < 32 || attempt.LeaseUntil == "" {
		return ErrInvalidIntent
	}
	return nil
}

type ArtifactState string

const (
	StatePending      ArtifactState = "pending_upload"
	StateUploading    ArtifactState = "uploading"
	StateAvailable    ArtifactState = "available"
	StateUnavailable  ArtifactState = "unavailable"
	StateInconsistent ArtifactState = "inconsistent"
)

// ContentDigest is the logical SHA-256 identity of the exact byte stream accepted.
type ContentDigest string

func (digest ContentDigest) Validate() error {
	if !validDigest(string(digest)) {
		return ErrInvalidIntent
	}
	return nil
}

type RetentionPolicyRef string

func (ref RetentionPolicyRef) Validate() error {
	if !identifierPattern.MatchString(string(ref)) {
		return ErrInvalidIntent
	}
	return nil
}

type ReconciliationState string

const (
	ReconciliationUnchecked    ReconciliationState = "not_checked"
	ReconciliationPending      ReconciliationState = "pending"
	ReconciliationConsistent   ReconciliationState = "consistent"
	ReconciliationInconsistent ReconciliationState = "inconsistent"
)

// RawArtifact is the BC06-owned logical record. Its location is private adapter
// state and its identity is never derived solely from ContentDigest.
type RawArtifact struct {
	TenantID           string
	Reference          ArtifactRef
	Source             SourceRef
	Representation     Representation
	Classification     Classification
	RetentionPolicyRef RetentionPolicyRef
	State              ArtifactState
	CreatedAt          string
	ConfirmedAt        string
	Reconciliation     ReconciliationState
}

// CustodyIntent names the domain intent independently of its HTTP DTO name.
type CustodyIntent = WriteIntent

type CustodyReceipt struct {
	ContractVersion    string         `json:"contract_version"`
	ReceiptID          string         `json:"receipt_id"`
	TenantID           string         `json:"tenant_id"`
	Artifact           ArtifactRef    `json:"artifact"`
	Source             SourceRef      `json:"source"`
	Representation     Representation `json:"representation"`
	Classification     Classification `json:"classification"`
	RetentionPolicyRef string         `json:"retention_policy_ref"`
	ConfirmedAt        string         `json:"confirmed_at"`
}

type ArtifactMetadata struct {
	Artifact           ArtifactRef
	Source             SourceRef
	Representation     Representation
	Classification     Classification
	RetentionPolicyRef string
	Location           ArtifactLocation
	State              ArtifactState
	CreatedAt          string
	ConfirmedAt        string
}

// ArtifactLocation is private BC06 persistence state; it is never the logical identity.
type ArtifactLocation struct {
	Provider  string
	Bucket    string
	Key       string
	VersionID string
}

var (
	ErrInvalidIntent  = errors.New("invalid custody intent")
	ErrConflict       = errors.New("custody operation conflicts with existing intent")
	ErrNotAvailable   = errors.New("artifact is not available")
	ErrInconsistent   = errors.New("artifact custody is inconsistent")
	ErrInProgress     = errors.New("custody upload is already in progress")
	ErrDigestMismatch = errors.New("uploaded bytes do not match declared digest and size")
)

func (intent WriteIntent) Validate(maxBytes int64) error {
	invalid := func(reason string) error { return fmt.Errorf("%w: %s", ErrInvalidIntent, reason) }
	if intent.ContractVersion != ContractVersion {
		return invalid("unsupported contract_version")
	}
	for label, value := range map[string]string{
		"operation_id":         intent.OperationID,
		"tenant_id":            intent.TenantID,
		"artifact_id":          intent.ArtifactID,
		"source_id":            intent.Source.SourceID,
		"source revision":      intent.Source.Revision,
		"retention_policy_ref": intent.RetentionPolicyRef,
	} {
		if !identifierPattern.MatchString(value) {
			return invalid(label + " is malformed")
		}
	}
	if intent.Artifact.ArtifactID != intent.ArtifactID {
		return invalid("artifact identity differs from write intent")
	}
	if !validDigest(intent.Artifact.Digest) {
		return invalid("digest must be lowercase sha256 hex")
	}
	if intent.Artifact.SizeBytes < 0 || intent.Artifact.SizeBytes > maxBytes {
		return invalid("size_bytes is outside configured bounds")
	}
	if intent.Artifact.MediaType != "" {
		if len(intent.Artifact.MediaType) > 256 || strings.ContainsAny(intent.Artifact.MediaType, "\r\n") {
			return invalid("media_type is malformed")
		}
		if _, _, err := mimeParseMediaType(intent.Artifact.MediaType); err != nil {
			return invalid("media_type is malformed")
		}
	} else if intent.Artifact.MediaTypePresent {
		return invalid("an explicitly present media_type cannot be empty")
	}
	switch intent.Representation {
	case RepresentationDecodedHTTPBody, RepresentationCapturedHTTPWire, RepresentationDownloadedFile, RepresentationBrowserScreenshot, RepresentationBrowserDOM, RepresentationOtherDeclared:
	default:
		return invalid("representation is unsupported")
	}
	switch intent.Classification {
	case ClassificationPublic, ClassificationInternal, ClassificationConfidential, ClassificationRestricted:
	default:
		return invalid("classification is unsupported")
	}
	encoded, err := json.Marshal(intent.Metadata)
	if err != nil || len(encoded) > MaxMetadataBytes {
		return invalid("metadata exceeds its byte budget")
	}
	if len(intent.Metadata) > 32 {
		return invalid("metadata has more than 32 properties")
	}
	for key, value := range intent.Metadata {
		if !identifierPattern.MatchString(key) || len(value) > 1024 || strings.ContainsRune(value, '\x00') || !utf8.ValidString(value) {
			return invalid("metadata contains an unsupported key or value")
		}
	}
	return nil
}

func (intent WriteIntent) IntentDigest() (string, error) {
	encoded, err := json.Marshal(intent)
	if err != nil {
		return "", fmt.Errorf("encode custody intent: %w", err)
	}
	digest := sha256.Sum256(encoded)
	return "sha256:" + hex.EncodeToString(digest[:]), nil
}

func (intent WriteIntent) Identity() (IntentIdentity, error) {
	digest, err := intent.IntentDigest()
	if err != nil {
		return IntentIdentity{}, err
	}
	return IntentIdentity{TenantID: intent.TenantID, OperationID: intent.OperationID, IntentDigest: digest}, nil
}

func (artifact ArtifactRef) Validate() error {
	if !identifierPattern.MatchString(artifact.ArtifactID) || !validDigest(artifact.Digest) || artifact.SizeBytes < 0 || artifact.SizeBytes > 9007199254740991 {
		return ErrInvalidIntent
	}
	if artifact.MediaType != "" {
		if len(artifact.MediaType) > 256 || strings.ContainsAny(artifact.MediaType, "\r\n") {
			return ErrInvalidIntent
		}
		if _, _, err := mime.ParseMediaType(artifact.MediaType); err != nil {
			return ErrInvalidIntent
		}
	} else if artifact.MediaTypePresent {
		return ErrInvalidIntent
	}
	return nil
}

func validDigest(value string) bool {
	if len(value) != len("sha256:")+64 || !strings.HasPrefix(value, "sha256:") {
		return false
	}
	decoded, err := hex.DecodeString(strings.TrimPrefix(value, "sha256:"))
	return err == nil && len(decoded) == sha256.Size && value == strings.ToLower(value)
}

// email.ParseMediaType is wrapped here so domain tests exercise the same strict parser.
func mimeParseMediaType(value string) (string, map[string]string, error) {
	return mime.ParseMediaType(value)
}
