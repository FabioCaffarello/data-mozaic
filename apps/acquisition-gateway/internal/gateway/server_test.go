package gateway

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
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
	upstream := httptest.NewServer(http.NotFoundHandler())
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
