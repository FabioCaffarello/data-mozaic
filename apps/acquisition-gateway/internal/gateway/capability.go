package gateway

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
)

const issuer = "urn:data-mozaic:bc04:execution-coordination"
const recipient = "urn:data-mozaic:acquisition-gateway"
const maxCapabilityLifetime = 2 * time.Minute

var safeClaimValue = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/_+\-]{0,255}$`)

type Capability struct {
	ContractVersion             string `json:"contract_version"`
	Issuer                      string `json:"issuer"`
	Recipient                   string `json:"recipient"`
	Algorithm                   string `json:"algorithm"`
	KeyID                       string `json:"key_id"`
	Audience                    string `json:"audience"`
	TenantID                    string `json:"tenant_id"`
	RunID                       string `json:"run_id"`
	AttemptID                   string `json:"attempt_id"`
	Generation                  int64  `json:"generation"`
	SourceID                    string `json:"source_id"`
	SourceRevisionID            string `json:"source_revision_id"`
	PolicyID                    string `json:"policy_id"`
	PolicyVersion               string `json:"policy_version"`
	PolicyDigest                string `json:"policy_digest"`
	ReleaseID                   string `json:"release_id"`
	BuildID                     string `json:"build_id"`
	BuildManifestDigest         string `json:"build_manifest_digest"`
	RuntimeProfileID            string `json:"runtime_profile_id"`
	InputParametersDigest       string `json:"input_parameters_digest,omitempty"`
	ExpectedInitialTargetDigest string `json:"expected_initial_target_digest,omitempty"`
	IssuedAt                    string `json:"issued_at"`
	ExpiresAt                   string `json:"expires_at"`
	ExecutionDeadline           string `json:"execution_deadline"`
	CapabilityID                string `json:"capability_id"`
	Signature                   string `json:"signature"`
}

func VerifyCapability(c Capability, keys map[string]ed25519.PublicKey, audience string, now, processStarted time.Time) error {
	stringsToCheck := []string{c.KeyID, c.Audience, c.TenantID, c.RunID, c.AttemptID, c.SourceID, c.SourceRevisionID, c.PolicyID, c.PolicyVersion, c.PolicyDigest, c.ReleaseID, c.BuildID, c.BuildManifestDigest, c.RuntimeProfileID, c.CapabilityID}
	for _, value := range stringsToCheck {
		if !safeClaimValue.MatchString(value) {
			return errors.New("capability_scope_invalid")
		}
	}
	if c.ContractVersion != "execution-capability/v1" || c.Issuer != issuer || c.Recipient != recipient || c.Algorithm != "Ed25519" || c.Audience != audience || c.Generation < 1 || c.RuntimeProfileID != "scrapy-live-http-v1" {
		return errors.New("capability_scope_invalid")
	}
	key := keys[c.KeyID]
	if len(key) != ed25519.PublicKeySize {
		return errors.New("capability_key_unknown")
	}
	if !strings.HasPrefix(c.Signature, "ed25519:") {
		return errors.New("capability_signature_invalid")
	}
	sig, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(c.Signature, "ed25519:"))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return errors.New("capability_signature_invalid")
	}
	body, err := json.Marshal(c)
	if err != nil {
		return errors.New("capability_signature_invalid")
	}
	var fields map[string]any
	if err = json.Unmarshal(body, &fields); err != nil {
		return errors.New("capability_signature_invalid")
	}
	delete(fields, "signature")
	canonical, err := json.Marshal(fields)
	if err != nil || !ed25519.Verify(key, canonical, sig) {
		return errors.New("capability_signature_invalid")
	}
	issued, err := time.Parse(time.RFC3339Nano, c.IssuedAt)
	if err != nil {
		return errors.New("capability_time_invalid")
	}
	expires, err := time.Parse(time.RFC3339Nano, c.ExpiresAt)
	if err != nil {
		return errors.New("capability_time_invalid")
	}
	deadline, err := time.Parse(time.RFC3339Nano, c.ExecutionDeadline)
	if err != nil {
		return errors.New("capability_time_invalid")
	}
	if issued.After(now.Add(time.Second)) || issued.Before(processStarted) || !expires.After(now) || expires.After(deadline) || expires.Sub(issued) > maxCapabilityLifetime {
		return errors.New("capability_expired_or_stale")
	}
	if !digestValue(c.PolicyDigest) || !digestValue(c.BuildManifestDigest) {
		return errors.New("capability_scope_invalid")
	}
	if c.InputParametersDigest != "" && !digestValue(c.InputParametersDigest) {
		return errors.New("capability_scope_invalid")
	}
	if c.ExpectedInitialTargetDigest != "" && !digestValue(c.ExpectedInitialTargetDigest) {
		return errors.New("capability_scope_invalid")
	}
	return nil
}

func digestValue(value string) bool {
	if !strings.HasPrefix(value, "sha256:") || len(value) != 71 {
		return false
	}
	decoded, err := hex.DecodeString(strings.TrimPrefix(value, "sha256:"))
	return err == nil && len(decoded) == sha256.Size
}
