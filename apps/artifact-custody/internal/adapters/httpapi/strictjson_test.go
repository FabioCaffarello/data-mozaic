package httpapi

import (
	"strings"
	"testing"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
)

func TestStrictDecodeWireBoundary(t *testing.T) {
	base := `{"contract_version":"artifact-custody.v1","operation_id":"op-1","tenant_id":"tenant-1","artifact_id":"artifact-1","source":{"source_id":"source-1","revision":"rev-1"},"artifact":{"artifact_id":"artifact-1","digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size_bytes":4,"media_type":"application/octet-stream"},"representation":"downloaded_file_bytes","classification":"internal","retention_policy_ref":"default-v1"}`
	var valid domain.WriteIntent
	if err := decodeStrict([]byte(base), &valid); err != nil {
		t.Fatalf("valid contract rejected: %v", err)
	}
	for name, raw := range map[string]string{
		"duplicate":           strings.Replace(base, `"tenant_id":"tenant-1"`, `"tenant_id":"tenant-1","tenant_id":"tenant-2"`, 1),
		"null":                strings.Replace(base, `"source":{"source_id":"source-1"`, `"source":null,"source":{"source_id":"source-1"`, 1),
		"extra":               strings.TrimSuffix(base, "}") + `,"database_uri":"x"}`,
		"lone high surrogate": strings.Replace(base, "source-1", `source-\uD800`, 1),
		"lone low surrogate":  strings.Replace(base, "source-1", `source-\uDC00`, 1),
		"trailing value":      base + ` {}`,
	} {
		t.Run(name, func(t *testing.T) {
			var value domain.WriteIntent
			if err := decodeStrict([]byte(raw), &value); err == nil {
				t.Fatal("invalid wire JSON accepted")
			}
		})
	}
	rawEmptyMedia := strings.Replace(base, `"media_type":"application/octet-stream"`, `"media_type":""`, 1)
	var emptyMedia domain.WriteIntent
	if err := decodeStrict([]byte(rawEmptyMedia), &emptyMedia); err != nil {
		t.Fatal(err)
	}
	if err := emptyMedia.Validate(domain.DefaultMaxBytes); err == nil {
		t.Fatal("explicit empty media_type was treated as absent")
	}
}
