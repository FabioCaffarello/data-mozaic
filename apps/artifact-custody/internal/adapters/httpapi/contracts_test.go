package httpapi

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
)

func TestPublishedWriteIntentFixtures(t *testing.T) {
	_, source, _, _ := runtime.Caller(0)
	root := filepath.Clean(filepath.Join(filepath.Dir(source), "../../../../../contracts/artifact-custody/draft/v1/fixtures"))
	valid, err := os.ReadFile(filepath.Join(root, "valid/write-intent.json"))
	if err != nil {
		t.Fatal(err)
	}
	var intent domain.WriteIntent
	if err = decodeStrict(valid, &intent); err != nil {
		t.Fatalf("valid public fixture rejected: %v", err)
	}
	if err = intent.Validate(domain.DefaultMaxBytes); err != nil {
		t.Fatalf("valid public fixture violates domain rules: %v", err)
	}
	for _, name := range []string{"null-is-not-absence.json", "physical-uri-is-not-a-logical-artifact-ref.json", "mismatched-artifact-identity.json"} {
		raw, readErr := os.ReadFile(filepath.Join(root, "invalid", name))
		if readErr != nil {
			t.Fatal(readErr)
		}
		t.Run(name, func(t *testing.T) {
			var candidate domain.WriteIntent
			if decodeErr := decodeStrict(raw, &candidate); decodeErr == nil && candidate.Validate(domain.DefaultMaxBytes) == nil {
				t.Fatal("invalid fixture passed wire and domain validation")
			}
		})
	}
}
