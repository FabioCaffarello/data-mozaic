package domain

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func validIntent() WriteIntent {
	return WriteIntent{
		ContractVersion: ContractVersion,
		OperationID:     "operation-1", TenantID: "tenant-1", ArtifactID: "artifact-1",
		Source:         SourceRef{SourceID: "source-1", Revision: "revision-1"},
		Artifact:       ArtifactRef{ArtifactID: "artifact-1", Digest: "sha256:" + strings.Repeat("a", 64), SizeBytes: 4, MediaType: "application/octet-stream"},
		Representation: RepresentationDownloadedFile, Classification: ClassificationInternal,
		RetentionPolicyRef: "retention-default-v1", Metadata: map[string]string{"label": "dados—teste"},
	}
}

func TestWriteIntentInvariantsAndOpaqueIdentity(t *testing.T) {
	intent := validIntent()
	if err := intent.Validate(DefaultMaxBytes); err != nil {
		t.Fatal(err)
	}
	digest1, err := intent.IntentDigest()
	if err != nil {
		t.Fatal(err)
	}
	intent.Metadata = map[string]string{"label": "dados—teste"}
	digest2, err := intent.IntentDigest()
	if err != nil || digest1 != digest2 {
		t.Fatalf("intent digest not stable: %v", err)
	}
	intent.ArtifactID = "other"
	if err := intent.Validate(DefaultMaxBytes); !errors.Is(err, ErrInvalidIntent) {
		t.Fatalf("expected identity mismatch, got %v", err)
	}
}

func TestWriteIntentRejectsDigestSizeAndRepresentation(t *testing.T) {
	for name, mutate := range map[string]func(*WriteIntent){
		"digest":         func(value *WriteIntent) { value.Artifact.Digest = "sha256:xyz" },
		"size":           func(value *WriteIntent) { value.Artifact.SizeBytes = DefaultMaxBytes + 1 },
		"representation": func(value *WriteIntent) { value.Representation = "parsed_html" },
		"classification": func(value *WriteIntent) { value.Classification = "top_secret" },
		"path-like id":   func(value *WriteIntent) { value.ArtifactID = "../key" },
	} {
		t.Run(name, func(t *testing.T) {
			intent := validIntent()
			mutate(&intent)
			if err := intent.Validate(DefaultMaxBytes); !errors.Is(err, ErrInvalidIntent) {
				t.Fatalf("expected invalid intent, got %v", err)
			}
		})
	}
}

func TestIntentDigestStableAcrossMetadataMapOrder(t *testing.T) {
	first := validIntent()
	first.Metadata = map[string]string{"z": "last", "a": "first", "ñ": "unicode"}
	second := validIntent()
	second.Metadata = map[string]string{"ñ": "unicode", "a": "first", "z": "last"}
	d1, err := first.IntentDigest()
	if err != nil {
		t.Fatal(err)
	}
	d2, err := second.IntentDigest()
	if err != nil {
		t.Fatal(err)
	}
	if d1 != d2 {
		t.Fatalf("map insertion order changed intent identity: %s != %s", d1, d2)
	}
}

func TestIntentDigestDistinguishesAbsentAndEmptyMetadata(t *testing.T) {
	absent := validIntent()
	absent.Metadata = nil
	empty := validIntent()
	empty.Metadata = map[string]string{}
	a, err := absent.IntentDigest()
	if err != nil {
		t.Fatal(err)
	}
	b, err := empty.IntentDigest()
	if err != nil {
		t.Fatal(err)
	}
	if a == b {
		t.Fatal("absent metadata and an empty metadata object must have different intent identities")
	}
	encoded, err := json.Marshal(empty)
	if err != nil {
		t.Fatal(err)
	}
	var roundTrip WriteIntent
	if err = json.Unmarshal(encoded, &roundTrip); err != nil {
		t.Fatal(err)
	}
	if roundTrip.Metadata == nil || len(roundTrip.Metadata) != 0 {
		t.Fatal("empty metadata did not survive persistence round trip")
	}
	roundTripDigest, err := roundTrip.IntentDigest()
	if err != nil || roundTripDigest != b {
		t.Fatalf("intent digest changed after round trip: %v", err)
	}
}

func TestIntentRejectsMetadataOverBoundsAndUnsafeUnicode(t *testing.T) {
	t.Run("property count", func(t *testing.T) {
		intent := validIntent()
		intent.Metadata = map[string]string{}
		for i := 0; i < 33; i++ {
			intent.Metadata["k"+strings.Repeat("a", i)] = "x"
		}
		if err := intent.Validate(DefaultMaxBytes); err == nil {
			t.Fatal("metadata with 33 fields accepted")
		}
	})
	t.Run("invalid utf8", func(t *testing.T) {
		intent := validIntent()
		intent.Metadata = map[string]string{"name": string([]byte{0xff})}
		if err := intent.Validate(DefaultMaxBytes); err == nil {
			t.Fatal("invalid UTF-8 accepted")
		}
	})
	t.Run("large interoperable integer", func(t *testing.T) {
		intent := validIntent()
		intent.Artifact.SizeBytes = 1 << 54
		if err := intent.Artifact.Validate(); err == nil {
			t.Fatal("non-interoperable size accepted")
		}
	})
	t.Run("metadata bytes", func(t *testing.T) {
		intent := validIntent()
		intent.Metadata = map[string]string{"body": strings.Repeat("x", MaxMetadataBytes)}
		if err := intent.Validate(DefaultMaxBytes); err == nil {
			t.Fatal("oversized metadata accepted")
		}
	})
}
