package main

import (
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/acquisition-gateway/internal/gateway"
)

type controlledSmokeAuthorizer struct{}

func (controlledSmokeAuthorizer) AuthorizeOperation(context.Context, gateway.Capability, string, string) error {
	return nil
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "-healthcheck" {
		response, err := http.Get(os.Args[2])
		if err != nil {
			os.Exit(1)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		return
	}
	listen := env("GATEWAY_LISTEN", ":8090")
	audience := os.Getenv("GATEWAY_AUDIENCE")
	keyPath := os.Getenv("GATEWAY_PUBLIC_KEYS_FILE")
	policyPath := os.Getenv("GATEWAY_POLICY_FILE")
	statePath := os.Getenv("GATEWAY_STATE_PATH")
	evidenceDirectory := os.Getenv("GATEWAY_EVIDENCE_DIRECTORY")
	if audience == "" || keyPath == "" || policyPath == "" || statePath == "" || evidenceDirectory == "" {
		log.Fatal("gateway audience, trusted policy/key files, durable quota state, and evidence directory are required")
	}
	keys, err := loadKeys(keyPath)
	if err != nil {
		log.Fatalf("trusted key configuration invalid: %v", err)
	}
	policies, err := loadPolicies(policyPath)
	if err != nil {
		log.Fatalf("trusted policy configuration invalid: %v", err)
	}
	var operationAuthorizer gateway.OperationAuthorizer
	if os.Getenv("GATEWAY_TEST_STATIC_AUTHORIZATION") == "1" {
		for _, policy := range policies {
			if policy.DNSPolicy != "test-fixture-only" {
				log.Fatal("static capability authorization is restricted to controlled test policies")
			}
		}
		operationAuthorizer = controlledSmokeAuthorizer{}
	} else if authorizationURL, authorizationToken := os.Getenv("GATEWAY_BC04_AUTHORIZATION_URL"), os.Getenv("GATEWAY_BC04_AUTHORIZATION_TOKEN"); authorizationURL != "" || authorizationToken != "" {
		operationAuthorizer, err = gateway.NewBC04HTTPAuthorizer(authorizationURL, authorizationToken)
		if err != nil {
			log.Fatalf("BC04 operation authorization configuration invalid: %v", err)
		}
	} else {
		for _, policy := range policies {
			if policy.DNSPolicy != "test-fixture-only" {
				log.Fatal("BC04 current-attempt authorization is required for non-test policies")
			}
		}
	}
	concurrency, err := strconv.Atoi(env("GATEWAY_GLOBAL_CONCURRENCY", "16"))
	if err != nil {
		log.Fatal("gateway concurrency configuration invalid")
	}
	spoolBytes, err := strconv.ParseInt(env("GATEWAY_MAX_SPOOL_BYTES", "104857600"), 10, 64)
	if err != nil {
		log.Fatal("gateway spool byte budget invalid")
	}
	spoolFiles, err := strconv.Atoi(env("GATEWAY_MAX_EVIDENCE_FILES", "1000"))
	if err != nil {
		log.Fatal("gateway spool file budget invalid")
	}
	archiveBytes, err := strconv.ParseInt(env("GATEWAY_MAX_EVIDENCE_ARCHIVE_BYTES", "16777216"), 10, 64)
	if err != nil || archiveBytes < 1 {
		log.Fatal("gateway evidence archive budget invalid")
	}
	var testRoots *x509.CertPool
	if rootsPath := os.Getenv("GATEWAY_TEST_ROOTS_FILE"); rootsPath != "" {
		pem, err := trustedFile(rootsPath, 1<<20)
		if err != nil {
			log.Fatal("gateway test roots unavailable")
		}
		testRoots = x509.NewCertPool()
		if !testRoots.AppendCertsFromPEM(pem) {
			log.Fatal("gateway test roots invalid")
		}
	}
	app, err := gateway.New(gateway.Options{Audience: audience, Keys: keys, Policies: policies, Started: time.Now().UTC(), GlobalConcurrency: concurrency, StatePath: statePath, EvidenceDirectory: evidenceDirectory, MaxSpoolBytes: spoolBytes, MaxEvidenceFiles: spoolFiles, MaxEvidenceArchiveBytes: archiveBytes, TestRoots: testRoots, OperationAuthorizer: operationAuthorizer, EvidenceToken: os.Getenv("GATEWAY_EVIDENCE_IMPORT_TOKEN"), EvidenceReleaseToken: os.Getenv("GATEWAY_EVIDENCE_RELEASE_TOKEN")})
	if err != nil {
		log.Fatalf("gateway startup denied: %v", err)
	}
	server := &http.Server{Addr: listen, Handler: app, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 310 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	stopped := make(chan os.Signal, 1)
	signal.Notify(stopped, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stopped
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	}()
	log.Printf("acquisition gateway listening on %s", listen)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("gateway stopped: %v", err)
	}
}

func loadKeys(path string) (map[string]ed25519.PublicKey, error) {
	data, err := trustedFile(path, 64<<10)
	if err != nil {
		return nil, err
	}
	var encoded map[string]string
	if err := json.Unmarshal(data, &encoded); err != nil || len(encoded) == 0 {
		return nil, errors.New("key ring is malformed")
	}
	keys := make(map[string]ed25519.PublicKey, len(encoded))
	for id, value := range encoded {
		decoded, err := base64.RawURLEncoding.DecodeString(value)
		if err != nil || len(decoded) != ed25519.PublicKeySize {
			return nil, errors.New("key ring contains invalid public key")
		}
		keys[id] = ed25519.PublicKey(decoded)
	}
	return keys, nil
}

func loadPolicies(path string) ([]gateway.Policy, error) {
	data, err := trustedFile(path, 2<<20)
	if err != nil {
		return nil, err
	}
	var set gateway.PolicySet
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&set); err != nil || len(set.Policies) == 0 {
		return nil, errors.New("policy snapshot is malformed")
	}
	return set.Policies, nil
}

func trustedFile(path string, max int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 || info.Size() < 1 || info.Size() > max {
		return nil, fmt.Errorf("%s must be a bounded regular file not writable by group/other", path)
	}
	return os.ReadFile(path)
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
