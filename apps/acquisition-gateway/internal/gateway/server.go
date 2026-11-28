package gateway

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxRequestBytes = 8192
const maxRedirects = 3

type Options struct {
	Audience                string
	Keys                    map[string]ed25519.PublicKey
	Policies                []Policy
	Started                 time.Time
	GlobalConcurrency       int
	StatePath               string
	EvidenceDirectory       string
	MaxSpoolBytes           int64
	MaxEvidenceFiles        int
	MaxEvidenceArchiveBytes int64
	TestRoots               *x509.CertPool
	OperationAuthorizer     OperationAuthorizer
	EvidenceToken           string
	EvidenceReleaseToken    string
}

// OperationAuthorizer must durably admit each upstream hop against BC04's
// current attempt before the gateway opens a source connection.
type OperationAuthorizer interface {
	AuthorizeOperation(context.Context, Capability, string, string) error
}

var ErrOperationDenied = errors.New("acquisition_operation_denied")

const maxEvidenceTransfers = 4

type budget struct {
	Requests            int   `json:"requests"`
	Bytes               int64 `json:"bytes"`
	Reserved            int64 `json:"reserved"`
	LastRequestUnixNano int64 `json:"last_request_unix_nano"`
	active              int
}

type Gateway struct {
	mu                      sync.Mutex
	started                 time.Time
	audience                string
	keys                    map[string]ed25519.PublicKey
	policies                map[string]Policy
	budgets                 map[string]*budget
	global                  chan struct{}
	evidenceTransfers       chan struct{}
	statePath               string
	evidenceDirectory       string
	maxSpoolBytes           int64
	maxEvidenceFiles        int
	maxEvidenceArchiveBytes int64
	spoolArchiveBytes       int64
	testRoots               *x509.CertPool
	operationAuthorizer     OperationAuthorizer
	evidenceToken           string
	evidenceReleaseToken    string
	spoolBytes              int64
	spoolFiles              int
	spoolReservedBytes      int64
	spoolReservedFiles      int
	spoolReservationSeq     uint64
	spoolReservations       map[uint64]int64
	activeEvidenceByID      map[string]int
}

type spoolReservation struct {
	id    uint64
	bytes int64
}

type fetchRequest struct {
	URL string `json:"url"`
}

type Evidence struct {
	ContractVersion       string    `json:"contract_version"`
	FetchID               string    `json:"fetch_id"`
	TenantID              string    `json:"tenant_id"`
	RunID                 string    `json:"run_id"`
	AttemptID             string    `json:"attempt_id"`
	Generation            int64     `json:"generation"`
	SourceID              string    `json:"source_id"`
	SourceRevisionID      string    `json:"source_revision_id"`
	PolicyDigest          string    `json:"policy_digest"`
	ReleaseID             string    `json:"release_id"`
	BuildManifestDigest   string    `json:"build_manifest_digest"`
	RequestTargetDigest   string    `json:"request_target_digest"`
	EffectiveTargetDigest string    `json:"effective_target_digest"`
	RequestOrigin         string    `json:"request_origin"`
	EffectiveOrigin       string    `json:"effective_origin"`
	RequestPathDigest     string    `json:"request_path_digest"`
	EffectivePathDigest   string    `json:"effective_path_digest"`
	QueryParameterNames   []string  `json:"query_parameter_names"`
	RedirectCount         int       `json:"redirect_count"`
	Status                int       `json:"status"`
	AcquiredAt            time.Time `json:"acquired_at"`
	MediaType             string    `json:"media_type"`
	BodyRepresentation    string    `json:"body_representation"`
	BodySize              int64     `json:"body_size"`
	BodyDigest            string    `json:"body_digest"`
	BodyRef               string    `json:"body_ref"`
}

func New(options Options) (*Gateway, error) {
	if options.Audience == "" || len(options.Keys) == 0 || options.GlobalConcurrency < 1 || options.GlobalConcurrency > 128 || options.StatePath == "" || options.EvidenceDirectory == "" || options.MaxSpoolBytes < 1 || options.MaxEvidenceFiles < 1 {
		return nil, errors.New("gateway_configuration_invalid")
	}
	if options.EvidenceReleaseToken != "" && len(options.EvidenceReleaseToken) < 32 {
		return nil, errors.New("evidence_release_credential_invalid")
	}
	if options.Started.IsZero() {
		options.Started = time.Now().UTC()
	}
	state, err := openLedger(options.StatePath)
	if err != nil {
		return nil, err
	}
	archiveLimit := options.MaxEvidenceArchiveBytes
	if archiveLimit == 0 {
		archiveLimit = defaultEvidenceArchiveBytes
	}
	if archiveLimit < 1 {
		return nil, errors.New("evidence_archive_capacity_exhausted")
	}
	spoolBytes, spoolFiles, err := prepareSpoolWithArchive(options.EvidenceDirectory, options.MaxSpoolBytes, options.MaxEvidenceFiles, archiveLimit)
	if err != nil {
		return nil, err
	}
	archiveBytes, err := evidenceArchiveUsage(options.EvidenceDirectory)
	if err != nil {
		return nil, err
	}
	if archiveLimit < 1 || archiveBytes > archiveLimit {
		return nil, errors.New("evidence_archive_capacity_exhausted")
	}
	g := &Gateway{started: options.Started.UTC(), audience: options.Audience, keys: options.Keys, policies: map[string]Policy{}, budgets: map[string]*budget{}, global: make(chan struct{}, options.GlobalConcurrency), evidenceTransfers: make(chan struct{}, maxEvidenceTransfers), statePath: options.StatePath, evidenceDirectory: options.EvidenceDirectory, maxSpoolBytes: options.MaxSpoolBytes, maxEvidenceFiles: options.MaxEvidenceFiles, maxEvidenceArchiveBytes: archiveLimit, spoolArchiveBytes: archiveBytes, testRoots: options.TestRoots, operationAuthorizer: options.OperationAuthorizer, evidenceToken: options.EvidenceToken, evidenceReleaseToken: options.EvidenceReleaseToken, spoolBytes: spoolBytes, spoolFiles: spoolFiles, spoolReservations: map[uint64]int64{}, activeEvidenceByID: map[string]int{}}
	for key, saved := range state {
		g.budgets[key] = &saved
	}
	for _, policy := range options.Policies {
		if err := policy.Validate(); err != nil {
			return nil, err
		}
		if err := policy.VerifyDigest(); err != nil {
			return nil, err
		}
		if _, exists := g.policies[policy.PolicyDigest]; exists {
			return nil, errors.New("gateway_policy_ambiguous")
		}
		g.policies[policy.PolicyDigest] = policy
	}
	if len(g.policies) == 0 {
		return nil, errors.New("gateway_policy_missing")
	}
	if err := g.persistLocked(); err != nil {
		return nil, err
	}
	return g, nil
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ready"}`)
		return
	}
	if r.URL.Path == "/internal/v1/resources" {
		g.serveResourceSnapshot(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/internal/v1/evidence/") && strings.HasSuffix(r.URL.Path, "/metadata") && r.Method == http.MethodGet {
		g.serveEvidenceMetadata(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/internal/v1/evidence/") {
		if r.Method == http.MethodDelete {
			g.serveEvidenceRelease(w, r)
			return
		}
		g.serveEvidence(w, r)
		return
	}
	if r.URL.Path != "/v1/fetch" || r.Method != http.MethodPost {
		writeError(w, http.StatusNotFound, "route_not_found")
		return
	}
	if r.Header.Get("Transfer-Encoding") != "" || r.ContentLength < 0 || r.ContentLength > maxRequestBytes {
		writeError(w, http.StatusBadRequest, "request_size_invalid")
		return
	}
	mediaType, _, mediaTypeErr := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if mediaTypeErr != nil || mediaType != "application/json" {
		writeError(w, http.StatusUnsupportedMediaType, "content_type_invalid")
		return
	}
	capability, err := g.parseCapability(r.Header.Get("Authorization"))
	if err != nil {
		writeError(w, http.StatusUnauthorized, errorCode(err))
		return
	}
	policy, ok := g.policies[capability.PolicyDigest]
	if !ok || policy.PolicyID != capability.PolicyID || policy.PolicyVersion != capability.PolicyVersion || policy.SourceID != capability.SourceID || policy.SourceRevisionID != capability.SourceRevisionID {
		writeError(w, http.StatusForbidden, "policy_binding_denied")
		return
	}
	responseMode := r.Header.Get("X-Mozaic-Response-Mode")
	if responseMode != "" && responseMode != "evidence-reference" {
		writeError(w, http.StatusBadRequest, "response_mode_invalid")
		return
	}
	var input fetchRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxRequestBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "request_invalid")
		return
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		writeError(w, http.StatusBadRequest, "request_invalid")
		return
	}

	result, err := g.fetch(r.Context(), capability, policy, input.URL)
	if err != nil {
		writeError(w, errorStatus(err), errorCode(err))
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Mozaic-Fetch-ID", result.evidence.FetchID)
	w.Header().Set("X-Mozaic-Evidence-Ref", result.evidence.FetchID)
	w.Header().Set("X-Mozaic-Source-Status", strconv.Itoa(result.evidence.Status))
	w.Header().Set("X-Mozaic-Body-SHA256", result.evidence.BodyDigest)
	w.Header().Set("X-Mozaic-Acquired-At", result.evidence.AcquiredAt.Format(time.RFC3339Nano))
	w.Header().Set("X-Mozaic-Body-Representation", result.evidence.BodyRepresentation)
	w.Header().Set("X-Mozaic-Media-Type", result.evidence.MediaType)
	w.Header().Set("X-Mozaic-Body-Size", strconv.FormatInt(result.evidence.BodySize, 10))
	if responseMode == "evidence-reference" {
		w.Header().Set("X-Mozaic-Response-Mode", "evidence-reference")
		w.Header().Set("Content-Length", "0")
		w.WriteHeader(http.StatusOK)
		return
	}
	verified, body, openErr := openVerifiedEvidence(g.evidenceDirectory, result.evidence.FetchID)
	if openErr != nil || verified.BodyDigest != result.evidence.BodyDigest || verified.BodySize != result.evidence.BodySize {
		if body != nil {
			_ = body.Close()
		}
		writeError(w, http.StatusBadGateway, "evidence_integrity_failed")
		return
	}
	defer body.Close()
	w.Header().Set("Content-Length", strconv.FormatInt(result.evidence.BodySize, 10))
	w.WriteHeader(http.StatusOK)
	if _, err := io.CopyN(w, body, result.evidence.BodySize); err != nil {
		return
	}
}

func (g *Gateway) serveEvidenceMetadata(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" || g.evidenceToken == "" || !constantTimeBearer(r.Header.Get("Authorization"), g.evidenceToken) {
		writeError(w, http.StatusUnauthorized, "evidence_importer_authentication_required")
		return
	}
	path := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/internal/v1/evidence/"), "/metadata")
	if !validEvidenceID(path) {
		writeError(w, http.StatusBadRequest, "evidence_id_invalid")
		return
	}
	generation, err := strconv.ParseInt(r.Header.Get("X-DM-Generation"), 10, 64)
	if err != nil || generation < 1 {
		writeError(w, http.StatusBadRequest, "evidence_binding_invalid")
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	evidence, err := readEvidenceMetadata(g.evidenceDirectory, path)
	if err != nil || evidence.TenantID != r.Header.Get("X-DM-Tenant-ID") || evidence.RunID != r.Header.Get("X-DM-Run-ID") || evidence.AttemptID != r.Header.Get("X-DM-Attempt-ID") || evidence.Generation != generation {
		writeError(w, http.StatusNotFound, "evidence_metadata_unavailable")
		return
	}
	bodyState := "retained"
	if _, err := os.Lstat(filepath.Join(g.evidenceDirectory, evidence.BodyRef)); errors.Is(err, os.ErrNotExist) {
		bodyState = "released"
	} else if err != nil {
		writeError(w, http.StatusServiceUnavailable, "evidence_metadata_unavailable")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(map[string]any{"contract_version": evidence.ContractVersion, "fetch_id": evidence.FetchID, "tenant_id": evidence.TenantID, "run_id": evidence.RunID, "attempt_id": evidence.AttemptID, "generation": evidence.Generation, "source_id": evidence.SourceID, "source_revision_id": evidence.SourceRevisionID, "policy_digest": evidence.PolicyDigest, "release_id": evidence.ReleaseID, "build_manifest_digest": evidence.BuildManifestDigest, "request_target_digest": evidence.RequestTargetDigest, "effective_target_digest": evidence.EffectiveTargetDigest, "request_origin": evidence.RequestOrigin, "effective_origin": evidence.EffectiveOrigin, "request_path_digest": evidence.RequestPathDigest, "effective_path_digest": evidence.EffectivePathDigest, "query_parameter_names": evidence.QueryParameterNames, "redirect_count": evidence.RedirectCount, "status": evidence.Status, "acquired_at": evidence.AcquiredAt, "media_type": evidence.MediaType, "representation": evidence.BodyRepresentation, "body_size": evidence.BodySize, "body_digest": evidence.BodyDigest, "body_state": bodyState})
}

// serveResourceSnapshot exposes component-wide capacity only; it contains no
// tenant, run, fetch, URL, or artifact identifiers.
func (g *Gateway) serveResourceSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.RawQuery != "" || g.evidenceToken == "" {
		writeError(w, http.StatusNotFound, "route_not_found")
		return
	}
	if !constantTimeBearer(r.Header.Get("Authorization"), g.evidenceToken) {
		writeError(w, http.StatusUnauthorized, "resource_inspection_authentication_required")
		return
	}
	g.mu.Lock()
	archiveAvailable := g.maxEvidenceArchiveBytes - g.spoolArchiveBytes
	if archiveAvailable < 0 {
		archiveAvailable = 0
	}
	snapshot := map[string]int64{
		"spool_bytes_used":              g.spoolBytes,
		"spool_bytes_reserved":          g.spoolReservedBytes,
		"spool_entries":                 int64(g.spoolFiles / 2),
		"spool_entry_reservations":      int64(g.spoolReservedFiles / 2),
		"spool_max_bytes":               g.maxSpoolBytes,
		"spool_max_entries":             int64(g.maxEvidenceFiles),
		"spool_archive_bytes_used":      g.spoolArchiveBytes,
		"spool_archive_bytes_max":       g.maxEvidenceArchiveBytes,
		"spool_archive_bytes_available": archiveAvailable,
		"active_fetches":                int64(len(g.global)),
		"active_evidence_transfers":     int64(len(g.evidenceTransfers)),
	}
	archiveExhausted := archiveAvailable == 0
	g.mu.Unlock()
	snapshotResponse := make(map[string]any, len(snapshot)+1)
	for key, value := range snapshot {
		snapshotResponse[key] = value
	}
	snapshotResponse["spool_archive_exhausted"] = archiveExhausted
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	if err := json.NewEncoder(w).Encode(snapshotResponse); err != nil {
		return
	}
}

func (g *Gateway) serveEvidence(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.RawQuery != "" || g.evidenceToken == "" {
		writeError(w, http.StatusNotFound, "route_not_found")
		return
	}
	if !constantTimeBearer(r.Header.Get("Authorization"), g.evidenceToken) {
		writeError(w, http.StatusUnauthorized, "evidence_importer_authentication_required")
		return
	}
	fetchID := strings.TrimPrefix(r.URL.Path, "/internal/v1/evidence/")
	if !validEvidenceID(fetchID) {
		writeError(w, http.StatusBadRequest, "evidence_id_invalid")
		return
	}
	generation, err := strconv.ParseInt(r.Header.Get("X-DM-Generation"), 10, 64)
	if err != nil || generation < 1 {
		writeError(w, http.StatusBadRequest, "evidence_binding_invalid")
		return
	}
	g.mu.Lock()
	select {
	case g.evidenceTransfers <- struct{}{}:
		g.activeEvidenceByID[fetchID]++
		g.mu.Unlock()
		defer func() {
			g.mu.Lock()
			g.activeEvidenceByID[fetchID]--
			if g.activeEvidenceByID[fetchID] == 0 {
				delete(g.activeEvidenceByID, fetchID)
			}
			<-g.evidenceTransfers
			g.mu.Unlock()
		}()
	default:
		g.mu.Unlock()
		writeError(w, http.StatusTooManyRequests, "evidence_concurrency_exhausted")
		return
	}
	evidence, body, err := openVerifiedEvidence(g.evidenceDirectory, fetchID)
	if err != nil {
		writeError(w, http.StatusNotFound, "evidence_unavailable")
		return
	}
	defer body.Close()
	if evidence.TenantID != r.Header.Get("X-DM-Tenant-ID") || evidence.RunID != r.Header.Get("X-DM-Run-ID") || evidence.AttemptID != r.Header.Get("X-DM-Attempt-ID") || evidence.Generation != generation {
		writeError(w, http.StatusForbidden, "evidence_binding_denied")
		return
	}
	metadata, err := json.Marshal(evidence)
	if err != nil || len(metadata) > 8192 {
		writeError(w, http.StatusInternalServerError, "evidence_metadata_invalid")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Mozaic-Evidence", base64.RawURLEncoding.EncodeToString(metadata))
	w.Header().Set("Content-Length", strconv.FormatInt(evidence.BodySize, 10))
	w.Header().Set("Digest", evidence.BodyDigest)
	w.WriteHeader(http.StatusOK)
	if _, err := io.CopyN(w, body, evidence.BodySize); err != nil {
		return
	}
}

// serveEvidenceRelease removes only temporary body bytes. The release credential
// is held by the trusted lifecycle operator, never by an execution worker.
// Eligibility is established by that caller through BC04/BC05/BC06 public
// interfaces; this owner-local operation binds the request to immutable
// FetchEvidence identity and refuses active readers.
func (g *Gateway) serveEvidenceRelease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete || r.URL.RawQuery != "" || g.evidenceReleaseToken == "" {
		writeError(w, http.StatusNotFound, "route_not_found")
		return
	}
	if !constantTimeBearer(r.Header.Get("Authorization"), g.evidenceReleaseToken) {
		writeError(w, http.StatusUnauthorized, "evidence_release_authentication_required")
		return
	}
	fetchID := strings.TrimPrefix(r.URL.Path, "/internal/v1/evidence/")
	generation, err := strconv.ParseInt(r.Header.Get("X-DM-Generation"), 10, 64)
	if !validEvidenceID(fetchID) || generation < 1 || r.Header.Get("X-DM-Tenant-ID") == "" || r.Header.Get("X-DM-Run-ID") == "" || r.Header.Get("X-DM-Attempt-ID") == "" || r.Header.Get("X-DM-Body-SHA256") == "" {
		writeError(w, http.StatusBadRequest, "evidence_release_binding_invalid")
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.activeEvidenceByID[fetchID] != 0 {
		writeError(w, http.StatusConflict, "evidence_read_active")
		return
	}
	evidence, err := readEvidenceMetadata(g.evidenceDirectory, fetchID)
	if err != nil {
		writeError(w, http.StatusNotFound, "evidence_unavailable")
		return
	}
	if evidence.TenantID != r.Header.Get("X-DM-Tenant-ID") || evidence.RunID != r.Header.Get("X-DM-Run-ID") || evidence.AttemptID != r.Header.Get("X-DM-Attempt-ID") || evidence.Generation != generation || evidence.BodyDigest != r.Header.Get("X-DM-Body-SHA256") {
		writeError(w, http.StatusForbidden, "evidence_release_binding_denied")
		return
	}
	_, activeMetadataErr := os.Lstat(filepath.Join(g.evidenceDirectory, fetchID+".json"))
	hadActiveMetadata := activeMetadataErr == nil
	removed, archived, err := releaseEvidenceBodyBounded(g.evidenceDirectory, fetchID, g.maxEvidenceArchiveBytes)
	if removed {
		g.spoolBytes -= evidence.BodySize
		if g.spoolBytes < 0 {
			// Fail closed on accounting divergence. Restart reconstructs from disk.
			g.spoolBytes = 0
		}
	}
	// Derive both capacity dimensions from durable directory state after the
	// transition, including errors between fsync/unlink boundaries.
	if _, statErr := os.Lstat(filepath.Join(g.evidenceDirectory, fetchID+".json")); hadActiveMetadata && errors.Is(statErr, os.ErrNotExist) {
		g.spoolFiles -= 2
		if g.spoolFiles < 0 {
			g.spoolFiles = 0
		}
	}
	if usage, usageErr := evidenceArchiveUsage(g.evidenceDirectory); usageErr == nil {
		g.spoolArchiveBytes = usage
	} else if err == nil {
		err = usageErr
	}
	_ = archived
	if err != nil {
		if err.Error() == "evidence_archive_capacity_exhausted" {
			writeError(w, http.StatusInsufficientStorage, "evidence_archive_capacity_exhausted")
			return
		}
		writeError(w, http.StatusServiceUnavailable, "evidence_release_incomplete")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(map[string]string{"contract_version": "fetch-evidence/v1", "fetch_id": fetchID, "body_state": "released", "body_digest": evidence.BodyDigest})
}

func constantTimeBearer(header, token string) bool {
	const prefix = "Bearer "
	if len(header) != len(prefix)+len(token) || !strings.HasPrefix(header, prefix) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(strings.TrimPrefix(header, prefix)), []byte(token)) == 1
}

type fetchResult struct {
	evidence Evidence
	bodyPath string
}

func (g *Gateway) fetch(parent context.Context, cap Capability, policy Policy, requested string) (fetchResult, error) {
	deadline, _ := time.Parse(time.RFC3339Nano, cap.ExecutionDeadline)
	policyDeadline := time.Now().Add(time.Duration(policy.DeadlineSeconds) * time.Second)
	if deadline.After(policyDeadline) {
		deadline = policyDeadline
	}
	ctx, cancel := context.WithDeadline(parent, deadline)
	defer cancel()
	if err := g.acquireGlobal(ctx); err != nil {
		return fetchResult{}, errors.New("gateway_concurrency_exhausted")
	}
	defer func() { <-g.global }()

	current := requested
	initial := requested
	for redirects := 0; ; redirects++ {
		target, err := policy.AuthorizeURL(current)
		if err != nil {
			return fetchResult{}, err
		}
		if redirects > 0 && (policy.RedirectPolicy != "revalidate-every-hop" || redirects > maxRedirects) {
			return fetchResult{}, errors.New("redirect_denied")
		}
		if g.operationAuthorizer == nil {
			return fetchResult{}, errors.New("current_attempt_authorizer_unavailable")
		}
		targetID, identityErr := sanitizedTarget(target.String())
		if identityErr != nil {
			return fetchResult{}, errors.New("operation_target_invalid")
		}
		operationID, idErr := randomID()
		if idErr != nil {
			return fetchResult{}, errors.New("operation_identity_failed")
		}
		if authErr := g.operationAuthorizer.AuthorizeOperation(ctx, cap, operationID, targetID.targetDigest); authErr != nil {
			if errors.Is(authErr, ErrOperationDenied) {
				return fetchResult{}, ErrOperationDenied
			}
			return fetchResult{}, errors.New("current_attempt_authorization_unavailable")
		}
		// Hash a delimited tuple: identifiers may themselves contain separators.
		key := hashDigest([]byte(cap.TenantID + "\x00" + cap.RunID + "\x00" + cap.PolicyDigest))
		spoolLease, err := g.reserveSpool(policy.MaxResponseBytes)
		if err != nil {
			return fetchResult{}, err
		}
		if err := g.reserve(key, policy); err != nil {
			g.releaseSpoolReservation(spoolLease, 0, false)
			return fetchResult{}, err
		}
		response, bodyFile, bodySize, bodyDigest, complete, err := g.doRequest(ctx, target, policy)
		if err != nil {
			if response != nil && response.Body != nil {
				_ = response.Body.Close()
			}
			if bodyFile != nil {
				_ = bodyFile.Close()
				_ = os.Remove(bodyFile.Name())
			}
			g.releaseSpoolReservation(spoolLease, 0, false)
			if finishErr := g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, complete); finishErr != nil {
				return fetchResult{}, errors.New("quota_state_unavailable")
			}
			return fetchResult{}, err
		}
		if response.StatusCode >= 300 && response.StatusCode <= 399 {
			location := response.Header.Get("Location")
			response.Body.Close()
			if bodyFile != nil {
				_ = bodyFile.Close()
				_ = os.Remove(bodyFile.Name())
			}
			g.releaseSpoolReservation(spoolLease, 0, false)
			if err := g.finish(key, policy.MaxResponseBytes, bodySize, true); err != nil {
				return fetchResult{}, err
			}
			if location == "" {
				return fetchResult{}, errors.New("redirect_location_missing")
			}
			if policy.RedirectPolicy != "revalidate-every-hop" || redirects >= maxRedirects {
				return fetchResult{}, errors.New("redirect_denied")
			}
			ref, parseErr := url.Parse(location)
			if parseErr != nil {
				return fetchResult{}, errors.New("redirect_invalid")
			}
			current = target.ResolveReference(ref).String()
			continue
		}
		if err := validateResponse(response); err != nil {
			response.Body.Close()
			if bodyFile != nil {
				_ = bodyFile.Close()
				_ = os.Remove(bodyFile.Name())
			}
			g.releaseSpoolReservation(spoolLease, 0, false)
			_ = g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false)
			return fetchResult{}, err
		}
		response.Body.Close()
		id, err := randomID()
		if err != nil {
			_ = bodyFile.Close()
			_ = os.Remove(bodyFile.Name())
			g.releaseSpoolReservation(spoolLease, 0, false)
			_ = g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false)
			return fetchResult{}, errors.New("evidence_id_failed")
		}
		mediaType := response.Header.Get("Content-Type")
		requestIdentity, identityErr := sanitizedTarget(initial)
		if identityErr != nil {
			_ = bodyFile.Close()
			_ = os.Remove(bodyFile.Name())
			g.releaseSpoolReservation(spoolLease, 0, false)
			_ = g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false)
			return fetchResult{}, errors.New("evidence_identity_invalid")
		}
		effectiveIdentity, identityErr := sanitizedTarget(current)
		if identityErr != nil {
			_ = bodyFile.Close()
			_ = os.Remove(bodyFile.Name())
			g.releaseSpoolReservation(spoolLease, 0, false)
			_ = g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false)
			return fetchResult{}, errors.New("evidence_identity_invalid")
		}
		evidence := Evidence{ContractVersion: "fetch-evidence/v1", FetchID: id, TenantID: cap.TenantID, RunID: cap.RunID, AttemptID: cap.AttemptID, Generation: cap.Generation, SourceID: cap.SourceID, SourceRevisionID: cap.SourceRevisionID, PolicyDigest: cap.PolicyDigest, ReleaseID: cap.ReleaseID, BuildManifestDigest: cap.BuildManifestDigest, RequestTargetDigest: requestIdentity.targetDigest, EffectiveTargetDigest: effectiveIdentity.targetDigest, RequestOrigin: requestIdentity.origin, EffectiveOrigin: effectiveIdentity.origin, RequestPathDigest: requestIdentity.pathDigest, EffectivePathDigest: effectiveIdentity.pathDigest, QueryParameterNames: requestIdentity.queryNames, RedirectCount: redirects, Status: response.StatusCode, AcquiredAt: time.Now().UTC(), MediaType: mediaType, BodyRepresentation: "decoded-http-entity", BodySize: bodySize, BodyDigest: bodyDigest}
		evidence.BodyRef = evidence.FetchID + ".body"
		bodyPath := bodyFile.Name()
		if err := commitEvidence(g.evidenceDirectory, evidence, bodyPath); err != nil {
			_ = bodyFile.Close()
			_ = os.Remove(bodyPath)
			g.releaseSpoolReservation(spoolLease, 0, false)
			if finishErr := g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false); finishErr != nil {
				return fetchResult{}, errors.New("quota_state_unavailable")
			}
			return fetchResult{}, err
		}
		if err := bodyFile.Close(); err != nil {
			_ = os.Remove(filepath.Join(g.evidenceDirectory, evidence.BodyRef))
			_ = os.Remove(filepath.Join(g.evidenceDirectory, evidence.FetchID+".json"))
			g.releaseSpoolReservation(spoolLease, 0, false)
			if finishErr := g.finish(key, policy.MaxResponseBytes, policy.MaxResponseBytes, false); finishErr != nil {
				return fetchResult{}, errors.New("quota_state_unavailable")
			}
			return fetchResult{}, errors.New("evidence_body_write_failed")
		}
		g.releaseSpoolReservation(spoolLease, bodySize, true)
		if err := g.finish(key, policy.MaxResponseBytes, bodySize, true); err != nil {
			return fetchResult{}, err
		}
		return fetchResult{evidence: evidence, bodyPath: filepath.Join(g.evidenceDirectory, evidence.BodyRef)}, nil
	}
}

type targetIdentity struct {
	targetDigest string
	origin       string
	pathDigest   string
	queryNames   []string
}

func sanitizedTarget(raw string) (targetIdentity, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return targetIdentity{}, err
	}
	origin, _, err := parseOrigin(u.Scheme+"://"+u.Host, u.Scheme == "http")
	if err != nil {
		return targetIdentity{}, err
	}
	query, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return targetIdentity{}, err
	}
	names := make([]string, 0, len(query))
	for name := range query {
		names = append(names, name)
	}
	sort.Strings(names)
	return targetIdentity{targetDigest: hashDigest([]byte(u.String())), origin: origin, pathDigest: hashDigest([]byte(u.EscapedPath())), queryNames: names}, nil
}

func (g *Gateway) reserveSpool(bytes int64) (spoolReservation, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if bytes < 0 || bytes > g.maxSpoolBytes-g.spoolBytes-g.spoolReservedBytes || g.spoolFiles+g.spoolReservedFiles+2 > g.maxEvidenceFiles*2 {
		return spoolReservation{}, errors.New("evidence_spool_capacity_exhausted")
	}
	g.spoolReservationSeq++
	reservation := spoolReservation{id: g.spoolReservationSeq, bytes: bytes}
	g.spoolReservations[reservation.id] = bytes
	g.spoolReservedBytes += bytes
	g.spoolReservedFiles += 2
	return reservation, nil
}

func (g *Gateway) releaseSpoolReservation(reservation spoolReservation, actual int64, committed bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	reserved, ok := g.spoolReservations[reservation.id]
	if !ok || reserved != reservation.bytes {
		return
	}
	delete(g.spoolReservations, reservation.id)
	g.spoolReservedBytes -= reserved
	g.spoolReservedFiles -= 2
	if committed {
		g.spoolBytes += actual
		g.spoolFiles += 2
	}
}

func (g *Gateway) doRequest(ctx context.Context, target *url.URL, policy Policy) (*http.Response, *os.File, int64, string, bool, error) {
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if policy.DNSPolicy == "test-fixture-only" && g.testRoots != nil {
		tlsConfig.RootCAs = g.testRoots
	}
	transport := &http.Transport{Proxy: nil, TLSClientConfig: tlsConfig, MaxConnsPerHost: 1, MaxIdleConns: 0, ResponseHeaderTimeout: 10 * time.Second, TLSHandshakeTimeout: 10 * time.Second, MaxResponseHeaderBytes: 32 << 10}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		host, port, err := net.SplitHostPort(address)
		if err != nil {
			return nil, errors.New("destination_invalid")
		}
		ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
		if err != nil || len(ips) == 0 {
			return nil, errors.New("destination_resolution_failed")
		}
		for _, ip := range ips {
			if !policy.allowsResolvedAddress(ip) {
				return nil, errors.New("destination_address_denied")
			}
		}
		if len(ips) > 8 {
			return nil, errors.New("destination_resolution_too_large")
		}
		return (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: -1}).DialContext(ctx, network, net.JoinHostPort(ips[0].String(), port))
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return nil, nil, 0, "", false, errors.New("request_invalid")
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("User-Agent", "DataMozaic-AcquisitionGateway/0.1")
	response, err := client.Do(req)
	if err != nil {
		return nil, nil, 0, "", false, errors.New("upstream_request_failed")
	}
	encoding := strings.ToLower(strings.TrimSpace(response.Header.Get("Content-Encoding")))
	if encoding != "" && encoding != "identity" {
		return response, nil, 0, "", false, errors.New("content_encoding_denied")
	}
	if response.ContentLength > policy.MaxResponseBytes {
		return response, nil, 0, "", false, errors.New("response_too_large")
	}
	spool, err := os.CreateTemp(g.evidenceDirectory, ".fetch-*.part")
	if err != nil {
		return response, nil, 0, "", false, errors.New("evidence_spool_write_failed")
	}
	if err = spool.Chmod(0600); err != nil {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
		return response, nil, 0, "", false, errors.New("evidence_spool_write_failed")
	}
	hash := sha256.New()
	buffer := make([]byte, 64<<10)
	var count int64
	for {
		readCount, readErr := response.Body.Read(buffer)
		if readCount > 0 {
			if int64(readCount) > policy.MaxResponseBytes-count {
				_ = spool.Close()
				_ = os.Remove(spool.Name())
				return response, nil, count, "", false, errors.New("response_too_large")
			}
			written, writeErr := spool.Write(buffer[:readCount])
			if writeErr != nil || written != readCount {
				_ = spool.Close()
				_ = os.Remove(spool.Name())
				return response, nil, count, "", false, errors.New("evidence_spool_write_failed")
			}
			_, _ = hash.Write(buffer[:readCount])
			count += int64(readCount)
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = spool.Close()
			_ = os.Remove(spool.Name())
			return response, nil, count, "", false, errors.New("response_interrupted")
		}
	}
	if response.ContentLength >= 0 && count != response.ContentLength {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
		return response, nil, count, "", false, errors.New("response_interrupted")
	}
	if err = spool.Sync(); err != nil {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
		return response, nil, count, "", false, errors.New("evidence_spool_write_failed")
	}
	return response, spool, count, "sha256:" + hex.EncodeToString(hash.Sum(nil)), true, nil
}

func validateResponse(response *http.Response) error {
	if len(response.Header) > 64 {
		return errors.New("response_headers_too_large")
	}
	return nil
}

func (g *Gateway) reserve(key string, p Policy) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	b := g.budgets[key]
	if b == nil {
		b = &budget{}
		g.budgets[key] = b
	}
	if b.Requests >= p.MaxRequests {
		return errors.New("request_budget_exhausted")
	}
	now := time.Now().UTC()
	minInterval := time.Second / time.Duration(p.RequestsPerSecond)
	if b.LastRequestUnixNano != 0 && now.Sub(time.Unix(0, b.LastRequestUnixNano)) < minInterval {
		return errors.New("request_rate_limited")
	}
	if b.active >= p.MaxConcurrency {
		return errors.New("attempt_concurrency_exhausted")
	}
	if b.Bytes+b.Reserved+p.MaxResponseBytes > p.MaxTotalResponseBytes {
		return errors.New("aggregate_byte_budget_exhausted")
	}
	previousLastRequest := b.LastRequestUnixNano
	b.Requests++
	b.LastRequestUnixNano = now.UnixNano()
	b.active++
	b.Reserved += p.MaxResponseBytes
	if err := g.persistLocked(); err != nil {
		b.Requests--
		b.LastRequestUnixNano = previousLastRequest
		b.active--
		b.Reserved -= p.MaxResponseBytes
		return err
	}
	return nil
}

func (g *Gateway) finish(key string, reserved, charge int64, complete bool) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	b := g.budgets[key]
	if b == nil {
		return errors.New("quota_state_corrupt")
	}
	b.active--
	b.Reserved -= min(reserved, b.Reserved)
	if !complete {
		charge = reserved
	}
	b.Bytes += charge
	if err := g.persistLocked(); err != nil {
		return err
	}
	return nil
}

func (g *Gateway) persistLocked() error {
	state := make(ledgerSnapshot, len(g.budgets))
	for key, value := range g.budgets {
		saved := *value
		saved.active = 0
		state[key] = saved
	}
	return persistLedger(g.statePath, state)
}

func (g *Gateway) acquireGlobal(ctx context.Context) error {
	select {
	case g.global <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (g *Gateway) parseCapability(header string) (Capability, error) {
	if !strings.HasPrefix(header, "Bearer ") {
		return Capability{}, errors.New("capability_missing")
	}
	raw, err := base64RawURL(strings.TrimPrefix(header, "Bearer "))
	if err != nil || len(raw) > 4096 {
		return Capability{}, errors.New("capability_invalid")
	}
	if err = rejectDuplicateJSON(raw); err != nil {
		return Capability{}, errors.New("capability_invalid")
	}
	var cap Capability
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&cap) != nil {
		return Capability{}, errors.New("capability_invalid")
	}
	if err = VerifyCapability(cap, g.keys, g.audience, time.Now().UTC(), g.started); err != nil {
		return Capability{}, err
	}
	return cap, nil
}

func errorCode(err error) string {
	message := err.Error()
	if errors.Is(err, ErrOperationDenied) {
		return "current_attempt_denied"
	}
	known := map[string]bool{"capability_missing": true, "capability_invalid": true, "capability_key_unknown": true, "capability_signature_invalid": true, "capability_scope_invalid": true, "capability_expired_or_stale": true, "capability_time_invalid": true, "policy_binding_denied": true, "url_invalid": true, "origin_denied": true, "scheme_denied": true, "test_destination_denied": true, "path_invalid": true, "path_denied": true, "path_traversal_denied": true, "path_separator_denied": true, "path_ambiguous": true, "query_invalid": true, "query_denied": true, "redirect_denied": true, "redirect_invalid": true, "redirect_location_missing": true, "destination_address_denied": true, "destination_resolution_failed": true, "upstream_request_failed": true, "response_too_large": true, "content_encoding_denied": true, "evidence_spool_capacity_exhausted": true, "request_budget_exhausted": true, "aggregate_byte_budget_exhausted": true, "attempt_concurrency_exhausted": true, "gateway_concurrency_exhausted": true, "response_interrupted": true, "response_headers_too_large": true, "request_rate_limited": true, "current_attempt_authorizer_unavailable": true, "current_attempt_authorization_unavailable": true, "current_attempt_authorization_failed": true, "operation_target_invalid": true, "operation_identity_failed": true}
	if known[message] {
		return message
	}
	return "gateway_request_failed"
}

func errorStatus(err error) int {
	code := errorCode(err)
	if code == "current_attempt_denied" {
		return http.StatusForbidden
	}
	if code == "current_attempt_authorizer_unavailable" || code == "current_attempt_authorization_unavailable" {
		return http.StatusServiceUnavailable
	}
	if strings.HasPrefix(code, "capability_") {
		return http.StatusUnauthorized
	}
	if code == "origin_denied" || code == "path_denied" || code == "query_denied" || code == "test_destination_denied" || code == "destination_address_denied" {
		return http.StatusForbidden
	}
	if strings.Contains(code, "budget") || strings.Contains(code, "concurrency") || strings.Contains(code, "rate_limited") || code == "evidence_spool_capacity_exhausted" {
		return http.StatusTooManyRequests
	}
	return http.StatusBadGateway
}

func writeError(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": code})
}

func hashDigest(data []byte) string {
	digest := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(digest[:])
}

func randomID() (string, error) {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes[:]), nil
}

func allowedAddress(ip netip.Addr, testOnly bool) bool {
	ip = ip.Unmap()
	if testOnly {
		return ip.IsLoopback()
	}
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
		return false
	}
	blocked := []netip.Prefix{netip.MustParsePrefix("0.0.0.0/8"), netip.MustParsePrefix("100.64.0.0/10"), netip.MustParsePrefix("192.0.0.0/24"), netip.MustParsePrefix("192.0.2.0/24"), netip.MustParsePrefix("192.88.99.0/24"), netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("198.51.100.0/24"), netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("240.0.0.0/4"), netip.MustParsePrefix("64:ff9b::/96"), netip.MustParsePrefix("2001::/23"), netip.MustParsePrefix("2001:db8::/32"), netip.MustParsePrefix("2002::/16"), netip.MustParsePrefix("fc00::/7"), netip.MustParsePrefix("fe80::/10")}
	for _, prefix := range blocked {
		if prefix.Contains(ip) {
			return false
		}
	}
	return true
}

func (p Policy) allowsResolvedAddress(ip netip.Addr) bool {
	if p.DNSPolicy != "test-fixture-only" {
		return allowedAddress(ip, false)
	}
	if allowedAddress(ip, true) {
		return true
	}
	for _, raw := range p.TestDestinationCIDRs {
		prefix, err := netip.ParsePrefix(raw)
		if err == nil && prefix.Contains(ip.Unmap()) {
			return true
		}
	}
	return false
}

func base64RawURL(value string) ([]byte, error) { return base64.RawURLEncoding.DecodeString(value) }

func rejectDuplicateJSON(data []byte) error {
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	var walk func() error
	walk = func() error {
		token, err := decoder.Token()
		if err != nil {
			return err
		}
		delim, ok := token.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]bool{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return err
				}
				key, ok := keyToken.(string)
				if !ok || seen[key] {
					return errors.New("duplicate_json_key")
				}
				seen[key] = true
				if err = walk(); err != nil {
					return err
				}
			}
			_, err = decoder.Token()
			return err
		case '[':
			for decoder.More() {
				if err := walk(); err != nil {
					return err
				}
			}
			_, err = decoder.Token()
			return err
		default:
			return fmt.Errorf("unexpected JSON delimiter")
		}
	}
	if err := walk(); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return errors.New("trailing_json_data")
	}
	return nil
}
