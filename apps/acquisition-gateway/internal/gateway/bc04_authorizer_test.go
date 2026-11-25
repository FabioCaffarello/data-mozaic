package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBC04AuthorizerSendsBoundOperationAndHandlesDenial(t *testing.T) {
	var observed bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/internal/v1/acquisition-operations/admit" || r.Header.Get("Authorization") != "Bearer "+strings.Repeat("x", 32) {
			t.Errorf("invalid BC04 request method=%s path=%s", r.Method, r.URL.Path)
		}
		var request struct {
			Capability   Capability `json:"capability"`
			OperationID  string     `json:"operation_id"`
			TargetDigest string     `json:"target_digest"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
			return
		}
		observed = request.Capability.CapabilityID == "capability_test_1" && request.OperationID == "op-1" && request.TargetDigest == "sha256:"+strings.Repeat("a", 64)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"error":"current_attempt_denied"}`))
	}))
	defer server.Close()
	authorizer, err := NewBC04HTTPAuthorizer(server.URL+"/internal/v1/acquisition-operations/admit", strings.Repeat("x", 32))
	if err != nil {
		t.Fatal(err)
	}
	err = authorizer.AuthorizeOperation(context.Background(), Capability{CapabilityID: "capability_test_1"}, "op-1", "sha256:"+strings.Repeat("a", 64))
	if err != ErrOperationDenied || !observed {
		t.Fatalf("authorization result=%v request observed=%v", err, observed)
	}
}

func TestBC04AuthorizerRejectsEndpointRedirects(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://127.0.0.1/other", http.StatusFound)
	}))
	defer server.Close()
	authorizer, err := NewBC04HTTPAuthorizer(server.URL+"/internal/v1/acquisition-operations/admit", strings.Repeat("x", 32))
	if err != nil {
		t.Fatal(err)
	}
	err = authorizer.AuthorizeOperation(context.Background(), Capability{}, "op-1", "sha256:"+strings.Repeat("a", 64))
	if err == nil || err == ErrOperationDenied {
		t.Fatalf("redirect response was treated as admission: %v", err)
	}
}
