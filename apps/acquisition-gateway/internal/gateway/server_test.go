package gateway

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type allowOperationAuthorizer struct{}

func (allowOperationAuthorizer) AuthorizeOperation(_ context.Context, _ Capability, _ string, _ string) error {
	return nil
}

type denyOperationAuthorizer struct{}

func (denyOperationAuthorizer) AuthorizeOperation(_ context.Context, _ Capability, _ string, _ string) error {
	return ErrOperationDenied
}

type blockingEvidenceWriter struct {
	header   http.Header
	started  chan struct{}
	release  chan struct{}
	startOne sync.Once
	status   int
}

type failingEvidenceWriter struct {
	header http.Header
	status int
}

func (w *failingEvidenceWriter) Header() http.Header { return w.header }

func (w *failingEvidenceWriter) WriteHeader(status int) { w.status = status }

func (w *failingEvidenceWriter) Write([]byte) (int, error) {
	return 0, errors.New("injected evidence consumer disconnect")
}

func (w *blockingEvidenceWriter) Header() http.Header { return w.header }

func (w *blockingEvidenceWriter) WriteHeader(status int) { w.status = status }

func (w *blockingEvidenceWriter) Write(body []byte) (int, error) {
	w.startOne.Do(func() { close(w.started) })
	<-w.release
	return len(body), nil
}

func TestDelegatedGETReturnsBoundEvidenceAndRejectsSpoolDivergence(t *testing.T) {
	var seen atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.Add(1)
		if r.Method != http.MethodGet {
			t.Errorf("method = %s", r.Method)
		}
		if r.Header.Get("Cookie") != "" || r.Header.Get("X-Worker-Secret") != "" {
			t.Error("worker headers reached source")
		}
		if r.Header.Get("Accept-Encoding") != "identity" {
			t.Errorf("encoding = %q", r.Header.Get("Accept-Encoding"))
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"records":[{"id":7}]}`))
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, func(p *Policy) { p.MaxRequests = 3 })
	w := fetch(g, cap, `{"url":"`+upstream.URL+`/api/v1/records?limit=7"}`, map[string]string{"Cookie": "exfiltrate", "X-Worker-Secret": "ignored"})
	if w.Code != http.StatusOK || w.Body.String() != `{"records":[{"id":7}]}` {
		t.Fatalf("gateway response: status=%d body=%q", w.Code, w.Body.String())
	}
	if w.Header().Get("X-Mozaic-Fetch-ID") == "" || w.Header().Get("X-Mozaic-Body-Representation") != "decoded-http-entity" {
		t.Fatalf("missing evidence metadata: %#v", w.Header())
	}
	if w.Header().Get("X-Mozaic-Body-SHA256") != hashDigest([]byte(w.Body.String())) {
		t.Fatal("evidence digest mismatch")
	}
	evidence, evidenceBody, err := readEvidence(g.evidenceDirectory, w.Header().Get("X-Mozaic-Fetch-ID"))
	if err != nil || evidence.BodyDigest != hashDigest(evidenceBody) || string(evidenceBody) != w.Body.String() {
		t.Fatalf("trusted spool verification failed: evidence=%#v err=%v", evidence, err)
	}
	importerRequest := httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
	importerRequest.Header.Set("Authorization", "Bearer "+g.evidenceToken)
	importerRequest.Header.Set("X-DM-Tenant-ID", evidence.TenantID)
	importerRequest.Header.Set("X-DM-Run-ID", evidence.RunID)
	importerRequest.Header.Set("X-DM-Attempt-ID", evidence.AttemptID)
	importerRequest.Header.Set("X-DM-Generation", strconv.FormatInt(evidence.Generation, 10))
	importerResponse := httptest.NewRecorder()
	g.ServeHTTP(importerResponse, importerRequest)
	if importerResponse.Code != http.StatusOK || importerResponse.Body.String() != string(evidenceBody) || importerResponse.Header().Get("X-Mozaic-Evidence") == "" {
		t.Fatalf("trusted importer evidence response status=%d body=%q", importerResponse.Code, importerResponse.Body.String())
	}
	unauthorizedImporter := httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
	unauthorizedImporter.Header = importerRequest.Header.Clone()
	unauthorizedImporter.Header.Set("Authorization", "Bearer wrong")
	unauthorizedImporterResponse := httptest.NewRecorder()
	g.ServeHTTP(unauthorizedImporterResponse, unauthorizedImporter)
	if unauthorizedImporterResponse.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated evidence response status=%d", unauthorizedImporterResponse.Code)
	}

	wrongAttempt := httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
	wrongAttempt.Header = importerRequest.Header.Clone()
	wrongAttempt.Header.Set("X-DM-Attempt-ID", "other-attempt")
	wrongAttemptResponse := httptest.NewRecorder()
	g.ServeHTTP(wrongAttemptResponse, wrongAttempt)
	if wrongAttemptResponse.Code != http.StatusForbidden {
		t.Fatalf("cross-attempt evidence status=%d", wrongAttemptResponse.Code)
	}
	if err := os.WriteFile(
		filepath.Join(g.evidenceDirectory, evidence.BodyRef),
		bytes.Repeat([]byte("x"), len(evidenceBody)),
		0600,
	); err != nil {
		t.Fatal(err)
	}
	divergentEvidence := httptest.NewRecorder()
	g.ServeHTTP(divergentEvidence, importerRequest)
	if divergentEvidence.Code != http.StatusNotFound ||
		!strings.Contains(divergentEvidence.Body.String(), "evidence_unavailable") {
		t.Fatalf("divergent authoritative evidence status=%d body=%q", divergentEvidence.Code, divergentEvidence.Body.String())
	}
	if seen.Load() != 1 {
		t.Fatalf("upstream requests = %d", seen.Load())
	}
}

func TestLargeChunkedEvidenceUsesVerifiedSpoolAndReferenceResponse(t *testing.T) {
	want := bytes.Repeat([]byte("bounded-transfer"), 32_768)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/pdf")
		for offset := 0; offset < len(want); offset += 32 << 10 {
			end := min(offset+(32<<10), len(want))
			_, _ = w.Write(want[offset:end])
			w.(http.Flusher).Flush()
		}
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, func(p *Policy) {
		p.MaxResponseBytes = int64(len(want))
		p.MaxTotalResponseBytes = int64(len(want))
	})
	body, _ := json.Marshal(fetchRequest{URL: upstream.URL + "/api/v1/records?limit=7"})
	response := fetch(g, cap, string(body), map[string]string{"X-Mozaic-Response-Mode": "evidence-reference"})
	if response.Code != http.StatusOK || response.Body.Len() != 0 || response.Header().Get("X-Mozaic-Response-Mode") != "evidence-reference" {
		t.Fatalf("reference response code=%d body=%d headers=%v", response.Code, response.Body.Len(), response.Header())
	}
	evidence, file, err := openVerifiedEvidence(g.evidenceDirectory, response.Header().Get("X-Mozaic-Fetch-ID"))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	actual, err := io.ReadAll(file)
	if err != nil || !bytes.Equal(actual, want) || evidence.BodySize != int64(len(want)) || evidence.BodyDigest != hashDigest(want) {
		t.Fatalf("stored chunked body diverged: size=%d digest=%q err=%v", evidence.BodySize, evidence.BodyDigest, err)
	}
}

func TestEvidenceTransferCapacityRejectsAndReleasesSlot(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"records":[{"id":7}]}`))
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, nil)
	fetchResponse := fetch(g, cap, `{"url":"`+upstream.URL+`/api/v1/records?limit=7"}`, nil)
	if fetchResponse.Code != http.StatusOK {
		t.Fatalf("fetch status=%d body=%s", fetchResponse.Code, fetchResponse.Body.String())
	}
	evidence, _, err := readEvidence(g.evidenceDirectory, fetchResponse.Header().Get("X-Mozaic-Fetch-ID"))
	if err != nil {
		t.Fatal(err)
	}
	// Shrink the production limit to one for a deterministic rejection test.
	g.evidenceTransfers = make(chan struct{}, 1)
	request := func() *http.Request {
		r := httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
		r.Header.Set("Authorization", "Bearer "+g.evidenceToken)
		r.Header.Set("X-DM-Tenant-ID", evidence.TenantID)
		r.Header.Set("X-DM-Run-ID", evidence.RunID)
		r.Header.Set("X-DM-Attempt-ID", evidence.AttemptID)
		r.Header.Set("X-DM-Generation", strconv.FormatInt(evidence.Generation, 10))
		return r
	}
	blocked := &blockingEvidenceWriter{
		header: make(http.Header), started: make(chan struct{}), release: make(chan struct{}),
	}
	firstDone := make(chan struct{})
	go func() {
		g.ServeHTTP(blocked, request())
		close(firstDone)
	}()
	select {
	case <-blocked.started:
	case <-time.After(2 * time.Second):
		t.Fatal("first evidence stream did not begin")
	}
	resources := httptest.NewRequest(http.MethodGet, "/internal/v1/resources", nil)
	resources.Header.Set("Authorization", "Bearer "+g.evidenceToken)
	resourceResponse := httptest.NewRecorder()
	g.ServeHTTP(resourceResponse, resources)
	var snapshot map[string]int64
	if err := json.Unmarshal(resourceResponse.Body.Bytes(), &snapshot); err != nil || snapshot["active_evidence_transfers"] != 1 {
		t.Fatalf("active evidence transfer snapshot=%v err=%v", snapshot, err)
	}
	second := httptest.NewRecorder()
	g.ServeHTTP(second, request())
	if second.Code != http.StatusTooManyRequests || !strings.Contains(second.Body.String(), "evidence_concurrency_exhausted") {
		t.Fatalf("excess transfer status=%d body=%s", second.Code, second.Body.String())
	}
	close(blocked.release)
	select {
	case <-firstDone:
	case <-time.After(2 * time.Second):
		t.Fatal("first evidence stream did not finish")
	}
	if blocked.status != http.StatusOK || len(g.evidenceTransfers) != 0 {
		t.Fatalf("first status=%d remaining slots=%d", blocked.status, len(g.evidenceTransfers))
	}
	third := httptest.NewRecorder()
	g.ServeHTTP(third, request())
	if third.Code != http.StatusOK || third.Body.String() != fetchResponse.Body.String() {
		t.Fatalf("released slot transfer status=%d body=%q", third.Code, third.Body.String())
	}
}

func TestEvidenceTransferReleasesSlotAfterConsumerWriteFailure(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"records":[{"id":9}]}`))
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, nil)
	fetched := fetch(g, cap, `{"url":"`+upstream.URL+`/api/v1/records?limit=7"}`, nil)
	if fetched.Code != http.StatusOK {
		t.Fatalf("fetch status=%d body=%s", fetched.Code, fetched.Body.String())
	}
	evidence, _, err := readEvidence(g.evidenceDirectory, fetched.Header().Get("X-Mozaic-Fetch-ID"))
	if err != nil {
		t.Fatal(err)
	}
	g.evidenceTransfers = make(chan struct{}, 1)
	request := httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
	request.Header.Set("Authorization", "Bearer "+g.evidenceToken)
	request.Header.Set("X-DM-Tenant-ID", evidence.TenantID)
	request.Header.Set("X-DM-Run-ID", evidence.RunID)
	request.Header.Set("X-DM-Attempt-ID", evidence.AttemptID)
	request.Header.Set("X-DM-Generation", strconv.FormatInt(evidence.Generation, 10))
	failing := &failingEvidenceWriter{header: make(http.Header)}
	g.ServeHTTP(failing, request)
	if len(g.evidenceTransfers) != 0 {
		t.Fatalf("consumer write failure leaked evidence transfer slot: %d", len(g.evidenceTransfers))
	}
	completed := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodGet, "/internal/v1/evidence/"+evidence.FetchID, nil)
	request.Header.Set("Authorization", "Bearer "+g.evidenceToken)
	request.Header.Set("X-DM-Tenant-ID", evidence.TenantID)
	request.Header.Set("X-DM-Run-ID", evidence.RunID)
	request.Header.Set("X-DM-Attempt-ID", evidence.AttemptID)
	request.Header.Set("X-DM-Generation", strconv.FormatInt(evidence.Generation, 10))
	g.ServeHTTP(completed, request)
	if completed.Code != http.StatusOK {
		t.Fatalf("evidence transfer after consumer failure status=%d", completed.Code)
	}
}

func TestSpoolCapacityReservationsAreBoundedAndReleasedOnce(t *testing.T) {
	g, _, _ := testGateway(t, "http://127.0.0.1:1", nil)
	g.maxSpoolBytes = 100
	reservation, err := g.reserveSpool(80)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := g.reserveSpool(21); err == nil || err.Error() != "evidence_spool_capacity_exhausted" {
		t.Fatalf("overlapping spool reservation = %v", err)
	}
	g.releaseSpoolReservation(reservation, 0, false)
	other, err := g.reserveSpool(70)
	if err != nil {
		t.Fatalf("capacity remained reserved after release: %v", err)
	}
	g.releaseSpoolReservation(reservation, 0, false)
	if g.spoolReservedBytes != other.bytes {
		t.Fatalf("duplicate release consumed another lease: %d", g.spoolReservedBytes)
	}
	g.releaseSpoolReservation(other, 0, false)
	if g.spoolReservedBytes != 0 || g.spoolReservedFiles != 0 {
		t.Fatalf("reservation release diverged: bytes=%d files=%d", g.spoolReservedBytes, g.spoolReservedFiles)
	}
	if _, err := g.reserveSpool(100); err != nil {
		t.Fatalf("capacity remained reserved after release: %v", err)
	}
}

func TestSpoolEntryReservationsAreBounded(t *testing.T) {
	g, _, _ := testGateway(t, "http://127.0.0.1:1", nil)
	g.maxEvidenceFiles = 1
	first, err := g.reserveSpool(1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := g.reserveSpool(0); err == nil || err.Error() != "evidence_spool_capacity_exhausted" {
		t.Fatalf("entry over-reservation = %v", err)
	}
	g.releaseSpoolReservation(first, 0, false)
	if _, err := g.reserveSpool(1); err != nil {
		t.Fatalf("entry capacity remained reserved: %v", err)
	}
}

func TestResourceSnapshotRequiresAuthenticationAndContainsOnlyAggregateState(t *testing.T) {
	g, _, _ := testGateway(t, "http://127.0.0.1:1", nil)
	unauthenticated := httptest.NewRecorder()
	g.ServeHTTP(unauthenticated, httptest.NewRequest(http.MethodGet, "/internal/v1/resources", nil))
	if unauthenticated.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated resource snapshot status=%d", unauthenticated.Code)
	}
	request := httptest.NewRequest(http.MethodGet, "/internal/v1/resources", nil)
	request.Header.Set("Authorization", "Bearer "+g.evidenceToken)
	response := httptest.NewRecorder()
	g.ServeHTTP(response, request)
	var snapshot map[string]int64
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &snapshot) != nil {
		t.Fatalf("resource snapshot response status=%d body=%q", response.Code, response.Body.String())
	}
	if _, hasTenant := snapshot["tenant_id"]; hasTenant || snapshot["spool_max_bytes"] != g.maxSpoolBytes {
		t.Fatalf("resource snapshot contains tenant detail or wrong bounds: %#v", snapshot)
	}
}

func TestSpoolStartupRemovesOnlyIncompleteAndUnpairedEvidenceFiles(t *testing.T) {
	root := t.TempDir()
	id := strings.Repeat("a", 32)
	for name, body := range map[string][]byte{
		".fetch-interrupted.part":    []byte("partial"),
		".evidence-meta-interrupted": []byte("partial"),
		"orphan.body":                []byte("uncommitted"),
		"orphan-metadata.json":       []byte("uncommitted"),
		id + ".body":                 []byte("kept"),
		id + ".json":                 []byte("metadata"),
	} {
		if err := os.WriteFile(filepath.Join(root, name), body, 0600); err != nil {
			t.Fatal(err)
		}
	}
	used, files, err := prepareSpool(root, 100, 10)
	if err != nil || used != 4 || files != 2 {
		t.Fatalf("startup spool recovery used=%d files=%d err=%v", used, files, err)
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 2 {
		t.Fatalf("startup recovery left unexpected spool entries: %#v err=%v", entries, err)
	}
}

func TestOverlappingGatewayTransfersRespectReservedSpoolCapacity(t *testing.T) {
	payload := bytes.Repeat([]byte("concurrent"), 48_000)
	var arrived atomic.Int32
	var maxConcurrent atomic.Int32
	release := make(chan struct{})
	var releaseOnce sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		active := arrived.Add(1)
		for {
			previous := maxConcurrent.Load()
			if active <= previous || maxConcurrent.CompareAndSwap(previous, active) {
				break
			}
		}
		if active == 2 {
			releaseOnce.Do(func() { close(release) })
		}
		select {
		case <-release:
		case <-time.After(2 * time.Second):
			http.Error(w, "concurrent transfer did not overlap", http.StatusGatewayTimeout)
			return
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
		_, _ = w.Write(payload)
	}))
	defer upstream.Close()
	g, cap, private := testGateway(t, upstream.URL, func(p *Policy) {
		p.MaxResponseBytes = int64(len(payload))
		p.MaxTotalResponseBytes = int64(len(payload) * 2)
		p.MaxConcurrency = 2
		p.RequestsPerSecond = 10
	})
	g.maxSpoolBytes = int64(len(payload) * 2)
	body := `{"url":"` + upstream.URL + `/api/v1/records?limit=7"}`
	responses := make(chan *httptest.ResponseRecorder, 2)
	second := cap
	second.RunID = "run-test-second"
	second.CapabilityID = "capability_test_second"
	second = resignCapability(t, private, second)
	for _, capability := range []Capability{cap, second} {
		capability := capability
		go func() { responses <- fetch(g, capability, body, nil) }()
	}
	for range 2 {
		response := <-responses
		if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), payload) {
			t.Fatalf("concurrent transfer status=%d bytes=%d", response.Code, response.Body.Len())
		}
	}
	if maxConcurrent.Load() != 2 || g.spoolBytes != int64(len(payload)*2) || g.spoolReservedBytes != 0 {
		t.Fatalf("concurrent resource accounting diverged: overlap=%d spool=%d reserved=%d", maxConcurrent.Load(), g.spoolBytes, g.spoolReservedBytes)
	}
}

func TestCurrentAttemptAuthorizationPrecedesEveryUpstreamRequest(t *testing.T) {
	var seen atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		seen.Add(1)
		_, _ = w.Write([]byte("unexpected"))
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, nil)
	g.operationAuthorizer = denyOperationAuthorizer{}
	response := fetch(g, cap, `{"url":"`+upstream.URL+`/api/v1/records?limit=7"}`, nil)
	if response.Code != http.StatusForbidden || !strings.Contains(response.Body.String(), "current_attempt_denied") {
		t.Fatalf("denied current attempt status=%d body=%s", response.Code, response.Body.String())
	}
	if seen.Load() != 0 {
		t.Fatal("gateway dialed the source before BC04 operation admission")
	}
}

func TestPolicyDeniesOriginPathQueryAndDuplicateQuery(t *testing.T) {
	var seen atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.Add(1)
		http.NotFound(w, r)
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, nil)
	for _, target := range []string{
		"http://127.0.0.1:1/api/v1/records?limit=7",
		upstream.URL + "/admin?limit=7",
		upstream.URL + "/api/v1/records?limit=8",
		upstream.URL + "/api/v1/records?limit=7&limit=7",
		upstream.URL + "/api/v1/records/%252e%252e/admin?limit=7",
		upstream.URL + "/api/v1/records%2fadmin?limit=7",
	} {
		body, _ := json.Marshal(fetchRequest{URL: target})
		response := fetch(g, cap, string(body), nil)
		if response.Code < 400 {
			t.Errorf("unauthorized URL passed: %s", target)
		}
	}
	if seen.Load() != 0 {
		t.Fatalf("gateway contacted the source for a denied target: requests=%d", seen.Load())
	}
}

func TestRedirectIsRevalidatedBeforeSecondDial(t *testing.T) {
	var denied atomic.Int32
	victim := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { denied.Add(1) }))
	defer victim.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, victim.URL+"/api/v1/records?limit=7", http.StatusFound)
	}))
	defer redirect.Close()
	g, cap, _ := testGateway(t, redirect.URL, func(p *Policy) { p.RedirectPolicy = "revalidate-every-hop" })
	response := fetch(g, cap, `{"url":"`+redirect.URL+`/api/v1/records?limit=7"}`, nil)
	if response.Code != http.StatusForbidden {
		t.Fatalf("redirect status=%d body=%s", response.Code, response.Body.String())
	}
	if denied.Load() != 0 {
		t.Fatal("gateway fetched unauthorized redirect target")
	}
}

func TestRequestAndAggregateBudgetsAreSharedAcrossCapabilities(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, func(p *Policy) { p.MaxRequests = 1 })
	body := `{"url":"` + upstream.URL + `/api/v1/records?limit=7"}`
	if response := fetch(g, cap, body, nil); response.Code != http.StatusOK {
		t.Fatalf("first status=%d", response.Code)
	}
	time.Sleep(110 * time.Millisecond)
	if response := fetch(g, cap, body, nil); response.Code != http.StatusTooManyRequests {
		t.Fatalf("second status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestRequestRateLimitAppliesBeforeUpstreamFetch(t *testing.T) {
	var seen atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { seen.Add(1); _, _ = w.Write([]byte("ok")) }))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, func(p *Policy) { p.MaxRequests = 3; p.RequestsPerSecond = 1 })
	body := `{"url":"` + upstream.URL + `/api/v1/records?limit=7"}`
	if response := fetch(g, cap, body, nil); response.Code != http.StatusOK {
		t.Fatalf("first status=%d", response.Code)
	}
	if response := fetch(g, cap, body, nil); response.Code != http.StatusTooManyRequests || !strings.Contains(response.Body.String(), "request_rate_limited") {
		t.Fatalf("rate status=%d body=%s", response.Code, response.Body.String())
	}
	if seen.Load() != 1 {
		t.Fatalf("rate-limited request reached source; count=%d", seen.Load())
	}
}

func TestConcurrentRequestsSharePolicyConcurrencyBudget(t *testing.T) {
	started := make(chan struct{}, 1)
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		started <- struct{}{}
		<-release
		_, _ = w.Write([]byte("ok"))
	}))
	defer upstream.Close()
	g, cap, _ := testGateway(t, upstream.URL, func(policy *Policy) {
		policy.MaxConcurrency = 1
		policy.RequestsPerSecond = 10
		policy.MaxRequests = 3
	})
	body := `{"url":"` + upstream.URL + `/api/v1/records?limit=7"}`
	first := make(chan *httptest.ResponseRecorder, 1)
	go func() { first <- fetch(g, cap, body, nil) }()
	<-started
	time.Sleep(110 * time.Millisecond) // allow the request-rate interval to elapse
	second := fetch(g, cap, body, nil)
	if second.Code != http.StatusTooManyRequests || !strings.Contains(second.Body.String(), "attempt_concurrency_exhausted") {
		t.Fatalf("concurrent request status=%d body=%s", second.Code, second.Body.String())
	}
	close(release)
	if response := <-first; response.Code != http.StatusOK {
		t.Fatalf("first request status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestRestartInvalidatesOldCapabilityAndPreservesRunQuota(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }))
	defer upstream.Close()
	gateway1, oldCapability, private := testGateway(t, upstream.URL, func(p *Policy) { p.MaxRequests = 2; p.MaxTotalResponseBytes = 1024 })
	body := `{"url":"` + upstream.URL + `/api/v1/records?limit=7"}`
	if response := fetch(gateway1, oldCapability, body, nil); response.Code != http.StatusOK {
		t.Fatalf("first status=%d body=%s", response.Code, response.Body.String())
	}
	time.Sleep(110 * time.Millisecond)
	policy := gateway1.policies[oldCapability.PolicyDigest]
	started2 := time.Now().UTC()
	gateway2, err := New(Options{Audience: gateway1.audience, Keys: gateway1.keys, Policies: []Policy{policy}, Started: started2, GlobalConcurrency: 4, StatePath: gateway1.statePath, EvidenceDirectory: gateway1.evidenceDirectory, MaxSpoolBytes: gateway1.maxSpoolBytes, MaxEvidenceFiles: gateway1.maxEvidenceFiles, OperationAuthorizer: allowOperationAuthorizer{}, EvidenceToken: gateway1.evidenceToken})
	if err != nil {
		t.Fatal(err)
	}
	if response := fetch(gateway2, oldCapability, body, nil); response.Code != http.StatusUnauthorized {
		t.Fatalf("pre-restart capability status=%d", response.Code)
	}
	newCapability := capabilityFor(t, private, upstream.URL, time.Now().UTC())
	newCapability.PolicyID = policy.PolicyID
	newCapability.PolicyVersion = policy.PolicyVersion
	newCapability.PolicyDigest = policy.PolicyDigest
	newCapability.SourceID = policy.SourceID
	newCapability.SourceRevisionID = policy.SourceRevisionID
	newCapability = resignCapability(t, private, newCapability)
	if response := fetch(gateway2, newCapability, body, nil); response.Code != http.StatusTooManyRequests {
		t.Fatalf("restarted quota status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestStaleCapabilitiesAndProhibitedAddressesFailClosed(t *testing.T) {
	started := time.Now().UTC()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cap := capabilityFor(t, private, "http://127.0.0.1:1", started.Add(-time.Second))
	err = VerifyCapability(cap, map[string]ed25519.PublicKey{"test-key": public}, "gateway/test", started, started)
	if err == nil || err.Error() != "capability_expired_or_stale" {
		t.Fatalf("stale cap err=%v", err)
	}
	for raw, want := range map[string]bool{"8.8.8.8": true, "10.0.0.1": false, "127.0.0.1": false, "169.254.169.254": false, "100.64.0.1": false, "192.0.2.1": false, "192.88.99.1": false, "240.0.0.1": false, "::1": false, "fc00::1": false, "fe80::1": false, "2001:4860:4860::8888": true, "2001:20::1": false, "2002::1": false, "64:ff9b::a00:1": false} {
		ip := netip.MustParseAddr(raw)
		if got := allowedAddress(ip, false); got != want {
			t.Errorf("allowedAddress(%s)=%v want %v", raw, got, want)
		}
	}
	policy := Policy{DNSPolicy: "test-fixture-only", TestDestinationCIDRs: []string{"172.30.249.10/32"}}
	if !policy.allowsResolvedAddress(netip.MustParseAddr("172.30.249.10")) || policy.allowsResolvedAddress(netip.MustParseAddr("172.30.249.11")) {
		t.Fatal("test fixture CIDR did not pin the controlled source destination")
	}
}

func TestExecutionCapabilityCrossLanguageConformanceVector(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "contracts", "execution-capability", "draft", "v1", "examples", "conformance-vector.json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		PublicKey  string     `json:"public_key"`
		Capability Capability `json:"capability"`
	}
	if err := json.Unmarshal(data, &vector); err != nil {
		t.Fatal(err)
	}
	key, err := base64.RawURLEncoding.DecodeString(vector.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	err = VerifyCapability(vector.Capability, map[string]ed25519.PublicKey{"vector-key-1": key}, "gateway/test", time.Date(2026, 11, 25, 16, 0, 30, 0, time.UTC), time.Date(2026, 11, 25, 15, 59, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("Python RFC 8785/Ed25519 vector rejected: %v", err)
	}
}

func TestExecutionCapabilityAcceptsBoundInputParameterDigest(t *testing.T) {
	g, cap, private := testGateway(t, "https://example.test", nil)
	cap.InputParametersDigest = "sha256:" + strings.Repeat("e", 64)
	cap = resignCapability(t, private, cap)
	if err := VerifyCapability(cap, g.keys, g.audience, time.Now().UTC(), g.started); err != nil {
		t.Fatalf("valid input parameter digest rejected: %v", err)
	}
	cap.InputParametersDigest = "sha256:invalid"
	cap = resignCapability(t, private, cap)
	if err := VerifyCapability(cap, g.keys, g.audience, time.Now().UTC(), g.started); err == nil {
		t.Fatal("invalid input parameter digest was accepted")
	}
}

func testGateway(t *testing.T, origin string, change func(*Policy)) (*Gateway, Capability, ed25519.PrivateKey) {
	t.Helper()
	started := time.Now().UTC().Add(-time.Second)
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	policy := Policy{ContractVersion: "acquisition-policy/v1", PolicyID: "controlled-get", PolicyVersion: "1.0.0", SourceID: "controlled-http", SourceRevisionID: "controlled-v1", Status: "eligible", Origins: []string{origin}, Methods: []string{"GET"}, PathPrefixes: []string{"/api/v1/records"}, QueryParameterValues: map[string][]string{"limit": {"7"}}, DNSPolicy: "test-fixture-only", RedirectPolicy: "deny", MaxRequests: 5, MaxConcurrency: 2, RequestsPerSecond: 10, MaxResponseBytes: 1024, MaxTotalResponseBytes: 4096, DeadlineSeconds: 30, RequireTLS: false, CredentialRefs: []string{}, EvidenceRetentionRef: "controlled-test", PolicyDigest: ""}
	if change != nil {
		change(&policy)
	}
	policy.PolicyDigest = policyDigest(t, policy)
	stateRoot := t.TempDir()
	g, err := New(Options{Audience: "gateway/test", Keys: map[string]ed25519.PublicKey{"test-key": public}, Policies: []Policy{policy}, Started: started, GlobalConcurrency: 8, StatePath: filepath.Join(stateRoot, "quota.json"), EvidenceDirectory: filepath.Join(stateRoot, "evidence"), MaxSpoolBytes: 1 << 20, MaxEvidenceFiles: 100, OperationAuthorizer: allowOperationAuthorizer{}, EvidenceToken: strings.Repeat("i", 40)})
	if err != nil {
		t.Fatal(err)
	}
	cap := capabilityFor(t, private, policy.Origins[0], started.Add(time.Second))
	cap.PolicyID = policy.PolicyID
	cap.PolicyVersion = policy.PolicyVersion
	cap.PolicyDigest = policy.PolicyDigest
	cap.SourceID = policy.SourceID
	cap.SourceRevisionID = policy.SourceRevisionID
	cap = resignCapability(t, private, cap)
	return g, cap, private
}

func capabilityFor(t *testing.T, private ed25519.PrivateKey, _ string, issued time.Time) Capability {
	t.Helper()
	digest := strings.Repeat("a", 64)
	c := Capability{ContractVersion: "execution-capability/v1", Issuer: issuer, Recipient: recipient, Algorithm: "Ed25519", KeyID: "test-key", Audience: "gateway/test", TenantID: "tenant-test", RunID: "run-test", AttemptID: "attempt-test", Generation: 1, SourceID: "controlled-http", SourceRevisionID: "controlled-v1", PolicyID: "controlled-get", PolicyVersion: "1.0.0", PolicyDigest: "sha256:" + digest, ReleaseID: "release-test", BuildID: "build-test", BuildManifestDigest: "sha256:" + strings.Repeat("b", 64), RuntimeProfileID: "scrapy-live-http-v1", IssuedAt: issued.UTC().Format(time.RFC3339Nano), ExpiresAt: issued.Add(time.Minute).UTC().Format(time.RFC3339Nano), ExecutionDeadline: issued.Add(90 * time.Second).UTC().Format(time.RFC3339Nano), CapabilityID: "capability_test_1"}
	return resignCapability(t, private, c)
}

func resignCapability(t *testing.T, private ed25519.PrivateKey, c Capability) Capability {
	t.Helper()
	c.Signature = ""
	b, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	delete(m, "signature")
	canonical, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	c.Signature = "ed25519:" + base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, canonical))
	return c
}

func policyDigest(t *testing.T, p Policy) string {
	t.Helper()
	p.PolicyDigest = ""
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err = json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	delete(m, "policy_digest")
	canonical, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return hashDigest(canonical)
}

func fetch(g *Gateway, cap Capability, body string, headers map[string]string) *httptest.ResponseRecorder {
	encoded, _ := json.Marshal(cap)
	r := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader([]byte(body)))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Authorization", "Bearer "+base64.RawURLEncoding.EncodeToString(encoded))
	for key, value := range headers {
		r.Header.Set(key, value)
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	return w
}
