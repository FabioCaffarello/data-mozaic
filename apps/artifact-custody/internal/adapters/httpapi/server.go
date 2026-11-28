package httpapi

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
)

type LocalPolicy struct{}

func (LocalPolicy) Authorize(_ context.Context, p application.Principal, action string, intent domain.WriteIntent) error {
	if p.TenantID == "" || intent.TenantID != "" && p.TenantID != intent.TenantID {
		return errors.New("tenant authorization denied")
	}
	if action != "artifact:read" && action != "artifact:write" && action != "artifact:verify" {
		return errors.New("scope denied")
	}
	return nil
}

type Server struct {
	service  *application.Service
	token    string
	maxBytes int64
	slots    chan struct{}
}

func NewServer(service *application.Service, mode, token string, maxBytes int64, maxConcurrent int) (http.Handler, error) {
	if service == nil || mode != "local" && mode != "test" || len(token) < 32 || maxBytes < 1 || maxConcurrent < 1 || maxConcurrent > 64 {
		return nil, errors.New("BC06 API requires explicit local/test mode, token and bounded limits")
	}
	s := &Server{service: service, token: token, maxBytes: maxBytes, slots: make(chan struct{}, maxConcurrent)}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	})
	mux.HandleFunc("POST /draft/v1/artifact-intents", s.createIntent)
	mux.HandleFunc("PUT /draft/v1/artifact-intents/{operation_id}/content", s.upload)
	mux.HandleFunc("GET /draft/v1/artifacts/{artifact_id}/verification", s.verify)
	mux.HandleFunc("GET /draft/v1/artifacts", s.inventory)
	mux.HandleFunc("GET /draft/v1/artifacts/{artifact_id}", s.metadata)
	mux.HandleFunc("GET /draft/v1/artifacts/{artifact_id}/content", s.download)
	mux.HandleFunc("GET /draft/v1/outbox/pending", s.pendingEvents)
	return s.auth(mux), nil
}

type inventoryCursor struct {
	TenantID   string `json:"tenant_id"`
	CreatedAt  string `json:"created_at"`
	ArtifactID string `json:"artifact_id"`
}

func (s *Server) inventory(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	for key := range query {
		if key != "limit" && key != "cursor" {
			writeError(w, http.StatusBadRequest, "invalid_request", "unsupported inventory query")
			return
		}
	}
	limit := 50
	if raw := query.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_request", "limit must be an integer")
			return
		}
		limit = n
	}
	afterArtifact := ""
	var afterCreated time.Time
	if cursor := query.Get("cursor"); cursor != "" {
		raw, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil || len(raw) > 1024 {
			writeError(w, http.StatusBadRequest, "invalid_request", "inventory cursor is invalid")
			return
		}
		var value inventoryCursor
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&value) != nil || value.TenantID != r.Context().Value(tenantKey{}).(string) || value.ArtifactID == "" {
			writeError(w, http.StatusBadRequest, "invalid_request", "inventory cursor scope is invalid")
			return
		}
		afterCreated, err = time.Parse(time.RFC3339Nano, value.CreatedAt)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_request", "inventory cursor time is invalid")
			return
		}
		afterArtifact = value.ArtifactID
	}
	page, err := s.service.Inventory(r.Context(), s.principal(r), r.Context().Value(tenantKey{}).(string), afterArtifact, afterCreated, limit)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "inventory request is unavailable or outside its bounds")
		return
	}
	if page.HasMore && len(page.Items) > 0 {
		last := page.Items[len(page.Items)-1]
		raw, _ := json.Marshal(inventoryCursor{TenantID: r.Context().Value(tenantKey{}).(string), CreatedAt: last.CreatedAt, ArtifactID: last.Artifact.ArtifactID})
		page.NextCursor = base64.RawURLEncoding.EncodeToString(raw)
	}
	writeJSON(w, http.StatusOK, map[string]any{"contract_version": domain.ContractVersion, "artifacts": page.Items, "next_cursor": page.NextCursor, "scope": "tenant", "disposition": "unknown_without_published_reference"})
}

func (s *Server) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
			next.ServeHTTP(w, r)
			return
		}
		header := r.Header.Get("Authorization")
		if !strings.HasPrefix(header, "Bearer ") || subtle.ConstantTimeCompare([]byte(strings.TrimPrefix(header, "Bearer ")), []byte(s.token)) != 1 {
			writeError(w, http.StatusUnauthorized, "unauthorized", "local test authorization failed")
			return
		}
		tenant := r.Header.Get("X-DM-Tenant-ID")
		if tenant == "" || len(tenant) > 128 {
			writeError(w, http.StatusBadRequest, "invalid_request", "tenant header is required")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), tenantKey{}, tenant)))
	})
}

type tenantKey struct{}

func (s *Server) principal(r *http.Request) application.Principal {
	return application.Principal{TenantID: r.Context().Value(tenantKey{}).(string), Scopes: []string{"artifact:read", "artifact:write"}}
}

func (s *Server) createIntent(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	var intent domain.WriteIntent
	if err := decodeRequest(r, &intent); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "request contract is invalid")
		return
	}
	reservation, err := s.service.CreateIntent(r.Context(), s.principal(r), intent)
	if err != nil {
		s.fail(w, err)
		return
	}
	status := http.StatusCreated
	if reservation.Receipt != nil || reservation.State != domain.StatePending {
		status = http.StatusOK
	}
	response := map[string]any{"contract_version": domain.ContractVersion, "operation_id": intent.OperationID, "artifact_id": intent.ArtifactID, "state": reservation.State}
	if reservation.Receipt != nil {
		response["receipt"] = reservation.Receipt
	}
	writeJSON(w, status, response)
}
func (s *Server) upload(w http.ResponseWriter, r *http.Request) {
	if r.ContentLength > s.maxBytes {
		writeError(w, http.StatusRequestEntityTooLarge, "size_limit_exceeded", "upload exceeds configured limit")
		return
	}
	if !s.acquire(w) {
		return
	}
	defer s.release()
	attempt := r.Header.Get("X-DM-Upload-Attempt")
	if attempt == "" {
		attempt = "http-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	}
	reader := http.MaxBytesReader(w, r.Body, s.maxBytes+1)
	receipt, err := s.service.Upload(r.Context(), s.principal(r), r.Context().Value(tenantKey{}).(string), r.PathValue("operation_id"), reader, attempt)
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, receipt)
}
func (s *Server) verify(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	size, err := strconv.ParseInt(query.Get("size_bytes"), 10, 64)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "size_bytes must be an integer")
		return
	}
	receipt, err := s.service.Verify(r.Context(), s.principal(r), r.Context().Value(tenantKey{}).(string), r.PathValue("artifact_id"), query.Get("digest"), size, domain.SourceRef{SourceID: query.Get("source_id"), Revision: query.Get("source_revision")})
	if errors.Is(err, domain.ErrNotAvailable) {
		writeJSON(w, http.StatusOK, map[string]any{"contract_version": domain.ContractVersion, "status": "unavailable"})
		return
	}
	if errors.Is(err, domain.ErrInconsistent) {
		writeJSON(w, http.StatusOK, map[string]any{"contract_version": domain.ContractVersion, "status": "inconsistent"})
		return
	}
	if err != nil {
		s.fail(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"contract_version": domain.ContractVersion, "status": "confirmed", "receipt": receipt})
}
func (s *Server) metadata(w http.ResponseWriter, r *http.Request) {
	value, err := s.service.Metadata(r.Context(), s.principal(r), r.Context().Value(tenantKey{}).(string), r.PathValue("artifact_id"))
	if err != nil {
		s.fail(w, err)
		return
	}
	response := map[string]any{"contract_version": domain.ContractVersion, "artifact": value.Artifact, "source": value.Source, "representation": value.Representation, "classification": value.Classification, "retention_policy_ref": value.RetentionPolicyRef, "state": value.State, "created_at": value.CreatedAt}
	if value.ConfirmedAt != "" {
		response["confirmed_at"] = value.ConfirmedAt
	}
	writeJSON(w, http.StatusOK, response)
}
func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	if !s.acquire(w) {
		return
	}
	defer s.release()
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()
	metadata, body, err := s.service.Download(ctx, s.principal(r), r.Context().Value(tenantKey{}).(string), r.PathValue("artifact_id"))
	if err != nil {
		s.fail(w, err)
		return
	}
	defer body.Close()
	w.Header().Set("Content-Type", metadata.Artifact.MediaType)
	w.Header().Set("Content-Length", strconv.FormatInt(metadata.Artifact.SizeBytes, 10))
	w.Header().Set("Digest", metadata.Artifact.Digest)
	if _, err = io.CopyN(w, body, metadata.Artifact.SizeBytes); err != nil {
		return
	}
}

func (s *Server) pendingEvents(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_request", "limit must be an integer")
			return
		}
		limit = n
	}
	events, err := s.service.PendingEvents(r.Context(), r.Context().Value(tenantKey{}).(string), limit)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "limit must be between 1 and 100")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": events, "delivery_guarantee": "none"})
}
func (s *Server) acquire(w http.ResponseWriter) bool {
	select {
	case s.slots <- struct{}{}:
		return true
	default:
		writeError(w, http.StatusTooManyRequests, "temporarily_unavailable", "artifact transfer capacity reached")
		return false
	}
}
func (s *Server) release() { <-s.slots }
func (s *Server) fail(w http.ResponseWriter, err error) {
	if !errors.Is(err, domain.ErrConflict) && !errors.Is(err, domain.ErrDigestMismatch) && !errors.Is(err, domain.ErrInvalidIntent) && !errors.Is(err, domain.ErrNotAvailable) {
		slog.Error("BC06 operation failed", "error_type", fmt.Sprintf("%T", err), "cause_type", fmt.Sprintf("%T", errors.Unwrap(err)))
	}
	var maxErr *http.MaxBytesError
	switch {
	case errors.As(err, &maxErr):
		writeError(w, http.StatusRequestEntityTooLarge, "size_limit_exceeded", "upload exceeds configured limit")
	case errors.Is(err, domain.ErrInProgress):
		writeError(w, http.StatusConflict, "operation_in_progress", "custody operation is already in progress")
	case errors.Is(err, domain.ErrConflict):
		writeError(w, http.StatusConflict, "operation_conflict", "operation identity conflicts with an existing request")
	case errors.Is(err, domain.ErrDigestMismatch):
		writeError(w, http.StatusUnprocessableEntity, "digest_mismatch", "uploaded bytes do not match the declared artifact")
	case errors.Is(err, domain.ErrInvalidIntent):
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", "artifact intent is invalid")
	case errors.Is(err, domain.ErrNotAvailable):
		writeError(w, http.StatusNotFound, "not_found", "artifact is not available to this tenant")
	case strings.Contains(err.Error(), "tenant scope"):
		writeError(w, http.StatusForbidden, "forbidden", err.Error())
	default:
		writeError(w, http.StatusServiceUnavailable, "temporarily_unavailable", "artifact custody operation could not be completed")
	}
}
func decodeRequest(r *http.Request, target any) error {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return err
	}
	return decodeStrict(raw, target)
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func writeError(w http.ResponseWriter, status int, code, message string) {
	var requestID [12]byte
	if _, err := rand.Read(requestID[:]); err != nil {
		requestID = [12]byte{0}
	}
	writeJSON(w, status, map[string]string{"contract_version": domain.ContractVersion, "code": code, "message": message, "request_id": hex.EncodeToString(requestID[:])})
}

func NewHTTPServer(handler http.Handler, address string) *http.Server {
	return &http.Server{Addr: address, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 11 * time.Minute, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
}
func ValidatePort(port string) error {
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return fmt.Errorf("invalid listen port")
	}
	return nil
}
