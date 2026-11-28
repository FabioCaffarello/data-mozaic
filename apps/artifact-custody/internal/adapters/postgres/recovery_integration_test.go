package postgres

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/httpapi"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/adapters/s3store"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// This test deliberately uses real PostgreSQL and the configured S3-compatible
// provider. It writes uniquely named synthetic rows and objects and never
// removes or overwrites pre-existing data.
func TestRecoveryAfterObjectCommitBeforeInventoryConfirmation(t *testing.T) {
	if os.Getenv("BC06_REAL_STORE_TESTS") != "1" {
		t.Skip("set BC06_REAL_STORE_TESTS=1 against an isolated local BC06 environment")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	db, err := Open(ctx, os.Getenv("ARTIFACT_CUSTODY_DATABASE_URL"), 4)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	objects, err := s3store.New(ctx, s3store.Config{
		Region: os.Getenv("AWS_REGION"), Endpoint: os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT"),
		Bucket: os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET"), TempDir: os.Getenv("ARTIFACT_CUSTODY_TEMP_DIR"),
		MaxBytes: domain.DefaultMaxBytes, PathStyle: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte("synthetic BC06 recovery evidence")
	digest := sha256.Sum256(payload)
	sha := "sha256:" + hex.EncodeToString(digest[:])
	suffix := randomSuffix(t)
	intent := domain.WriteIntent{
		ContractVersion: domain.ContractVersion, OperationID: "recovery-op-" + suffix,
		TenantID: "recovery-tenant-" + suffix, ArtifactID: "recovery-artifact-" + suffix,
		Source:         domain.SourceRef{SourceID: "synthetic", Revision: "recovery-v1"},
		Artifact:       domain.ArtifactRef{ArtifactID: "recovery-artifact-" + suffix, Digest: sha, SizeBytes: int64(len(payload)), MediaType: "application/octet-stream", MediaTypePresent: true},
		Representation: domain.RepresentationDownloadedFile, Classification: domain.ClassificationInternal,
		RetentionPolicyRef: "local-test-v1",
	}
	if err = intent.Validate(domain.DefaultMaxBytes); err != nil {
		t.Fatal(err)
	}
	identity, err := intent.Identity()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ReserveIntent(ctx, intent, identity.IntentDigest); err != nil {
		t.Fatal(err)
	}
	oldClaim, err := db.ClaimUpload(ctx, intent.TenantID, intent.OperationID, identity.IntentDigest, "synthetic-first", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	location, err := objects.PutVerified(ctx, oldClaim.ObjectKey, int64(len(payload)), sha, intent.Artifact.MediaType, bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	changed := []byte("replacement bytes must not overwrite")
	changedDigest := sha256.Sum256(changed)
	changedSHA := "sha256:" + hex.EncodeToString(changedDigest[:])
	if _, overwriteErr := objects.PutVerified(ctx, oldClaim.ObjectKey, int64(len(changed)), changedSHA, intent.Artifact.MediaType, bytes.NewReader(changed)); overwriteErr == nil {
		t.Fatal("conditional object write unexpectedly replaced an existing immutable upload key")
	}
	stored, err := objects.Head(ctx, location)
	if err != nil || stored.Digest != sha || stored.Size != int64(len(payload)) {
		t.Fatalf("conditional write changed original object: metadata=%+v err=%v", stored, err)
	}
	if _, err = db.db.ExecContext(ctx, `UPDATE artifact_custody.artifact_inventory SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=$1 AND operation_id=$2`, intent.TenantID, intent.OperationID); err != nil {
		t.Fatal(err)
	}
	service, err := application.NewService(db, objects, integrationPolicy{}, domain.DefaultMaxBytes)
	if err != nil {
		t.Fatal(err)
	}
	completed, err := service.ReconcileExpiredUpload(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	if completed != 1 {
		t.Fatalf("confirmed=%d, want one recovered object", completed)
	}
	receipt, err := db.VerifyArtifact(ctx, intent.TenantID, intent.ArtifactID, sha, int64(len(payload)), intent.Source)
	if err != nil || receipt.ReceiptID == "" {
		t.Fatalf("recovered receipt: receipt=%+v err=%v", receipt, err)
	}
	pendingEvents, err := db.ListPendingEvents(ctx, intent.TenantID, 100)
	if err != nil {
		t.Fatalf("list tenant-scoped pending events: %v", err)
	}
	foundCustodyEvent := false
	for _, event := range pendingEvents {
		if event.EventID == "custody-confirmed-"+receipt.ReceiptID &&
			event.TenantID == intent.TenantID && event.AggregateID == intent.ArtifactID {
			foundCustodyEvent = true
		}
	}
	if !foundCustodyEvent {
		t.Fatalf("durable custody event missing for operation %s", intent.OperationID)
	}
	api, err := httpapi.NewServer(service, "test", "integration-token-at-least-32-bytes-long", domain.DefaultMaxBytes, 2)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/draft/v1/artifacts?limit=10", nil)
	request.Header.Set("Authorization", "Bearer integration-token-at-least-32-bytes-long")
	request.Header.Set("X-DM-Tenant-ID", intent.TenantID)
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	var inventory map[string]any
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &inventory) != nil {
		t.Fatalf("tenant inventory response status=%d body=%s", response.Code, response.Body.String())
	}
	items, ok := inventory["artifacts"].([]any)
	if !ok || len(items) != 1 {
		t.Fatalf("tenant inventory count unexpected: %#v", inventory)
	}
	item, ok := items[0].(map[string]any)
	if !ok || item["operation_id"] != intent.OperationID || item["state"] != "available" {
		t.Fatalf("inventory did not expose owner state: %#v", item)
	}
	if _, exposesPhysical := item["location"]; exposesPhysical {
		t.Fatal("inventory exposed private physical storage location")
	}
	request = httptest.NewRequest(http.MethodGet, "/draft/v1/artifacts?limit=10", nil)
	request.Header.Set("Authorization", "Bearer integration-token-at-least-32-bytes-long")
	request.Header.Set("X-DM-Tenant-ID", "other-tenant-"+suffix)
	response = httptest.NewRecorder()
	api.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"artifacts":[]`) {
		t.Fatalf("tenant inventory crossed scope: status=%d body=%s", response.Code, response.Body.String())
	}
	otherTenantEvents, err := db.ListPendingEvents(ctx, "other-"+suffix, 100)
	if err != nil {
		t.Fatalf("list other tenant pending events: %v", err)
	}
	if len(otherTenantEvents) != 0 {
		t.Fatalf("pending event query crossed tenant boundary: got %d events", len(otherTenantEvents))
	}
	var recoveredState string
	var recoveredAt *time.Time
	if err = db.db.QueryRowContext(ctx, `SELECT reconciliation_state,last_reconciled_at FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2`, intent.TenantID, intent.OperationID).Scan(&recoveredState, &recoveredAt); err != nil {
		t.Fatal(err)
	}
	if recoveredState != string(domain.ReconciliationConsistent) || recoveredAt == nil {
		t.Fatalf("recovered object classification state=%s at=%v", recoveredState, recoveredAt)
	}
	replayed, err := db.ClaimUpload(ctx, intent.TenantID, intent.OperationID, identity.IntentDigest, "synthetic-replay", time.Minute)
	if err != nil || replayed.Receipt == nil || replayed.Receipt.ReceiptID != receipt.ReceiptID {
		t.Fatalf("durable replay: claim=%+v err=%v", replayed, err)
	}
	if _, err = db.CompleteUpload(ctx, oldClaim, location); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("stale claim completion error=%v, want conflict", err)
	}
	backupAndRestoreObject(ctx, t, db, objects, service, intent, receipt, location, suffix)
	read, err := objects.Get(ctx, application.ObjectLocation{Provider: location.Provider, Bucket: location.Bucket, Key: location.Key, VersionID: location.VersionID})
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	got, err := io.ReadAll(read)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(payload) {
		t.Fatalf("restored object bytes differ: got %q", got)
	}

	missing := intent
	missing.OperationID = "recovery-missing-op-" + suffix
	missing.ArtifactID = "recovery-missing-artifact-" + suffix
	missing.Artifact.ArtifactID = missing.ArtifactID
	missingDigest := sha256.Sum256([]byte("missing"))
	missing.Artifact.Digest = "sha256:" + hex.EncodeToString(missingDigest[:])
	missing.Artifact.SizeBytes = int64(len("missing"))
	missingID, err := missing.Identity()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ReserveIntent(ctx, missing, missingID.IntentDigest); err != nil {
		t.Fatal(err)
	}
	if _, err = db.ClaimUpload(ctx, missing.TenantID, missing.OperationID, missingID.IntentDigest, "synthetic-missing", time.Minute); err != nil {
		t.Fatal(err)
	}
	if _, err = db.db.ExecContext(ctx, `UPDATE artifact_custody.artifact_inventory SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=$1 AND operation_id=$2`, missing.TenantID, missing.OperationID); err != nil {
		t.Fatal(err)
	}
	completed, err = service.ReconcileExpiredUpload(ctx, 10)
	if err != nil || completed != 0 {
		t.Fatalf("missing-object reconciliation completed=%d err=%v", completed, err)
	}
	var state, reconciliationState string
	var reconciledAt *time.Time
	if err = db.db.QueryRowContext(ctx, `SELECT state,reconciliation_state,last_reconciled_at FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2`, missing.TenantID, missing.OperationID).Scan(&state, &reconciliationState, &reconciledAt); err != nil {
		t.Fatal(err)
	}
	if state != string(domain.StatePending) || reconciliationState != string(domain.ReconciliationPending) || reconciledAt == nil {
		t.Fatalf("missing object classification state=%s reconciliation=%s at=%v", state, reconciliationState, reconciledAt)
	}

	divergent := intent
	divergent.OperationID = "recovery-divergent-op-" + suffix
	divergent.ArtifactID = "recovery-divergent-artifact-" + suffix
	divergent.Artifact.ArtifactID = divergent.ArtifactID
	divergentID, err := divergent.Identity()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ReserveIntent(ctx, divergent, divergentID.IntentDigest); err != nil {
		t.Fatal(err)
	}
	divergentClaim, err := db.ClaimUpload(ctx, divergent.TenantID, divergent.OperationID, divergentID.IntentDigest, "synthetic-divergent", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	badBytes := bytes.Repeat([]byte("x"), len(payload))
	badDigest := sha256.Sum256(badBytes)
	clientConfig, err := config.LoadDefaultConfig(ctx, config.WithRegion(os.Getenv("AWS_REGION")))
	if err != nil {
		t.Fatal(err)
	}
	client := s3.NewFromConfig(clientConfig, func(options *s3.Options) {
		options.UsePathStyle = true
		options.BaseEndpoint = aws.String(os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT"))
	})
	_, err = client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET")), Key: aws.String("outside-bc06/" + suffix), Body: bytes.NewReader([]byte("denied")), ContentLength: aws.Int64(int64(len("denied")))})
	if err == nil {
		t.Fatal("application S3 credentials unexpectedly wrote outside the BC06 prefix")
	}
	_, err = client.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET")), Key: aws.String(divergentClaim.ObjectKey), Body: bytes.NewReader(badBytes), ContentLength: aws.Int64(int64(len(badBytes))), Metadata: map[string]string{"sha256": "sha256:" + hex.EncodeToString(badDigest[:]), "size-bytes": fmt.Sprint(len(badBytes))}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.db.ExecContext(ctx, `UPDATE artifact_custody.artifact_inventory SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=$1 AND operation_id=$2`, divergent.TenantID, divergent.OperationID); err != nil {
		t.Fatal(err)
	}
	completed, err = service.ReconcileExpiredUpload(ctx, 10)
	if err != nil || completed != 0 {
		t.Fatalf("divergent-object reconciliation completed=%d err=%v", completed, err)
	}
	if _, err = db.VerifyArtifact(ctx, divergent.TenantID, divergent.ArtifactID, divergent.Artifact.Digest, divergent.Artifact.SizeBytes, divergent.Source); !errors.Is(err, domain.ErrInconsistent) {
		t.Fatalf("divergent artifact verification error=%v, want inconsistent", err)
	}

	concurrent := intent
	concurrent.OperationID = "recovery-race-op-" + suffix
	concurrent.ArtifactID = "recovery-race-artifact-" + suffix
	concurrent.Artifact.ArtifactID = concurrent.ArtifactID
	concurrentID, err := concurrent.Identity()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ReserveIntent(ctx, concurrent, concurrentID.IntentDigest); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(ctx, os.Getenv("ARTIFACT_CUSTODY_DATABASE_URL"), 4)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	service2, err := application.NewService(db2, objects, integrationPolicy{}, domain.DefaultMaxBytes)
	if err != nil {
		t.Fatal(err)
	}
	services := []*application.Service{service, service2}
	results := make([]domain.CustodyReceipt, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range services {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for retry := 0; retry < 100; retry++ {
				results[i], errs[i] = services[i].Upload(ctx, application.Principal{TenantID: concurrent.TenantID}, concurrent.TenantID, concurrent.OperationID, bytes.NewReader(payload), fmt.Sprintf("reconcile-spoof-%d-%d-%s", i, retry, suffix))
				if !errors.Is(errs[i], domain.ErrInProgress) {
					return
				}
				time.Sleep(20 * time.Millisecond)
			}
			errs[i] = fmt.Errorf("concurrent upload did not resolve within 2 seconds: %w", errs[i])
		}(i)
	}
	wg.Wait()
	for i, uploadErr := range errs {
		if uploadErr != nil {
			t.Fatalf("independent writer %d failed: %v", i, uploadErr)
		}
	}
	if results[0].ReceiptID == "" || results[0].ReceiptID != results[1].ReceiptID {
		t.Fatalf("concurrent operation receipt mismatch: %#v", results)
	}
	var ordinaryState string
	var ordinaryReconciledAt *time.Time
	if err = db.db.QueryRowContext(ctx, `SELECT reconciliation_state,last_reconciled_at FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2`, concurrent.TenantID, concurrent.OperationID).Scan(&ordinaryState, &ordinaryReconciledAt); err != nil {
		t.Fatal(err)
	}
	if ordinaryState != string(domain.ReconciliationUnchecked) || ordinaryReconciledAt != nil {
		t.Fatalf("caller-selected attempt label changed reconciliation state=%s at=%v", ordinaryState, ordinaryReconciledAt)
	}
}

type integrationPolicy struct{}

func (integrationPolicy) Authorize(context.Context, application.Principal, string, domain.WriteIntent) error {
	return nil
}

func backupAndRestoreObject(ctx context.Context, t *testing.T, db *Store, objects *s3store.Store, service *application.Service, sourceIntent domain.WriteIntent, sourceReceipt domain.CustodyReceipt, sourceLocation application.ObjectLocation, suffix string) {
	t.Helper()
	awsConfig, err := config.LoadDefaultConfig(ctx, config.WithRegion(os.Getenv("AWS_REGION")))
	if err != nil {
		t.Fatal(err)
	}
	client := s3.NewFromConfig(awsConfig, func(options *s3.Options) {
		options.UsePathStyle = true
		options.BaseEndpoint = aws.String(os.Getenv("ARTIFACT_CUSTODY_S3_ENDPOINT"))
	})
	bucket := os.Getenv("ARTIFACT_CUSTODY_S3_BUCKET")
	backupKey := "bc06/test-backup/" + suffix
	restoredKey := "bc06/test-restore/" + suffix
	for _, target := range []string{backupKey, restoredKey} {
		_, err = client.CopyObject(ctx, &s3.CopyObjectInput{Bucket: aws.String(bucket), Key: aws.String(target), CopySource: aws.String(url.PathEscape(bucket + "/" + sourceLocation.Key))})
		if err != nil {
			t.Fatalf("copy object into isolated backup/restore key %q: %v", target, err)
		}
	}
	restoredLocation := application.ObjectLocation{Provider: "s3", Bucket: bucket, Key: restoredKey}
	metadata, err := objects.Head(ctx, restoredLocation)
	if err != nil || metadata.Size != sourceIntent.Artifact.SizeBytes || metadata.Digest != sourceIntent.Artifact.Digest {
		t.Fatalf("restored object metadata=%+v err=%v", metadata, err)
	}
	restoredIntent := sourceIntent
	restoredIntent.TenantID = "restore-tenant-" + suffix
	restoredIntent.OperationID = "restore-operation-" + suffix
	restoredIntent.ArtifactID = "restore-artifact-" + suffix
	restoredIntent.Artifact.ArtifactID = restoredIntent.ArtifactID
	if err = restoredIntent.Validate(domain.DefaultMaxBytes); err != nil {
		t.Fatal(err)
	}
	restoredReceipt := sourceReceipt
	restoredReceipt.TenantID = restoredIntent.TenantID
	restoredReceipt.Artifact = restoredIntent.Artifact
	restoredReceipt.Source = restoredIntent.Source
	restoredReceipt.ReceiptID = application.ReceiptID(restoredIntent.TenantID, restoredIntent.OperationID, claimIntentDigest(restoredIntent))
	restoredReceiptRaw, err := json.Marshal(restoredReceipt)
	if err != nil {
		t.Fatal(err)
	}
	intentRaw, err := json.Marshal(restoredIntent)
	if err != nil {
		t.Fatal(err)
	}
	intentDigest := claimIntentDigest(restoredIntent)
	_, err = db.db.ExecContext(ctx, `INSERT INTO artifact_custody.artifact_inventory
	(tenant_id,artifact_id,operation_id,intent_digest,intent,state,object_provider,object_bucket,object_key,receipt,confirmed_at)
	VALUES($1,$2,$3,$4,$5,'available','s3',$6,$7,$8,clock_timestamp())`, restoredIntent.TenantID, restoredIntent.ArtifactID, restoredIntent.OperationID, intentDigest, intentRaw, bucket, restoredKey, restoredReceiptRaw)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.db.ExecContext(ctx, `INSERT INTO artifact_custody.custody_operations(tenant_id,operation_id,artifact_id,intent_digest,receipt) VALUES($1,$2,$3,$4,$5)`, restoredIntent.TenantID, restoredIntent.OperationID, restoredIntent.ArtifactID, intentDigest, restoredReceiptRaw)
	if err != nil {
		t.Fatal(err)
	}
	_, body, err := service.Download(ctx, application.Principal{TenantID: restoredIntent.TenantID}, restoredIntent.TenantID, restoredIntent.ArtifactID)
	if err != nil {
		t.Fatalf("read restored bytes through BC06 application: %v", err)
	}
	defer body.Close()
	actual, err := io.ReadAll(body)
	if err != nil {
		t.Fatal(err)
	}
	actualDigest := sha256.Sum256(actual)
	if int64(len(actual)) != restoredIntent.Artifact.SizeBytes || "sha256:"+hex.EncodeToString(actualDigest[:]) != restoredIntent.Artifact.Digest {
		t.Fatal("restored BC06 read does not match the custody digest and size")
	}
}

func randomSuffix(t *testing.T) string {
	t.Helper()
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(raw[:])
}
