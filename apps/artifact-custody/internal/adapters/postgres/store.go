package postgres

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/application"
	"github.com/FabioCaffarello/data-mozaic/apps/artifact-custody/internal/domain"
	_ "github.com/jackc/pgx/v5/stdlib"
)

// Store is the BC06-owned PostgreSQL inventory adapter. It never holds a SQL
// transaction open while object bytes are transferred.
type Store struct{ db *sql.DB }

func Open(ctx context.Context, dsn string, maxOpen int) (*Store, error) {
	if dsn == "" || maxOpen < 1 || maxOpen > 64 {
		return nil, errors.New("invalid BC06 PostgreSQL configuration")
	}
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, fmt.Errorf("open BC06 PostgreSQL: %w", err)
	}
	db.SetMaxOpenConns(maxOpen)
	db.SetMaxIdleConns(maxOpen)
	db.SetConnMaxLifetime(30 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("connect BC06 PostgreSQL: %w", err)
	}
	return &Store{db: db}, nil
}
func (s *Store) Close() error { return s.db.Close() }

func (s *Store) ReserveIntent(ctx context.Context, intent domain.WriteIntent, digest string) (application.Reservation, error) {
	wire, err := json.Marshal(intent)
	if err != nil {
		return application.Reservation{}, err
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return application.Reservation{}, err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `INSERT INTO artifact_custody.artifact_inventory
	(tenant_id,artifact_id,operation_id,intent_digest,intent,state) VALUES($1,$2,$3,$4,$5,'pending_upload')
	ON CONFLICT DO NOTHING`, intent.TenantID, intent.ArtifactID, intent.OperationID, digest, wire)
	if err != nil {
		return application.Reservation{}, fmt.Errorf("reserve custody intent: %w", err)
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO artifact_custody.custody_operations
	(tenant_id,operation_id,artifact_id,intent_digest) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, intent.TenantID, intent.OperationID, intent.ArtifactID, digest)
	if err != nil {
		return application.Reservation{}, fmt.Errorf("record custody operation: %w", err)
	}
	if err = tx.Commit(); err != nil {
		return application.Reservation{}, err
	}
	reservation, err := s.GetReservation(ctx, intent.TenantID, intent.OperationID)
	if err != nil {
		return application.Reservation{}, err
	}
	storedDigest, err := intentDigest(ctx, s.db, intent.TenantID, intent.OperationID)
	if err != nil {
		return application.Reservation{}, err
	}
	if storedDigest != digest {
		return application.Reservation{}, domain.ErrConflict
	}
	return reservation, nil
}

func intentDigest(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, tenant, operation string) (string, error) {
	var digest string
	err := q.QueryRowContext(ctx, `SELECT intent_digest FROM artifact_custody.custody_operations WHERE tenant_id=$1 AND operation_id=$2`, tenant, operation).Scan(&digest)
	if err != nil {
		return "", fmt.Errorf("read operation intent: %w", err)
	}
	return digest, nil
}

func (s *Store) GetReservation(ctx context.Context, tenant, operation string) (application.Reservation, error) {
	var raw, receiptRaw []byte
	var state domain.ArtifactState
	err := s.db.QueryRowContext(ctx, `SELECT intent,state,receipt FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2`, tenant, operation).Scan(&raw, &state, &receiptRaw)
	if err != nil {
		return application.Reservation{}, mapNotFound(err)
	}
	var intent domain.WriteIntent
	if err = json.Unmarshal(raw, &intent); err != nil {
		return application.Reservation{}, fmt.Errorf("decode stored intent: %w", err)
	}
	result := application.Reservation{Intent: intent, State: state}
	if len(receiptRaw) > 0 {
		var receipt domain.CustodyReceipt
		if err = json.Unmarshal(receiptRaw, &receipt); err != nil {
			return application.Reservation{}, fmt.Errorf("decode stored receipt: %w", err)
		}
		result.Receipt = &receipt
	}
	return result, nil
}

func (s *Store) ClaimUpload(ctx context.Context, tenant, operation, digest, attempt string, lease time.Duration) (application.UploadClaim, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return application.UploadClaim{}, err
	}
	defer tx.Rollback()
	var raw, receiptRaw []byte
	var storedDigest string
	var state domain.ArtifactState
	var generation int64
	var leaseUntil sql.NullTime
	err = tx.QueryRowContext(ctx, `SELECT intent,intent_digest,state,generation,lease_until,receipt FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2 FOR UPDATE`, tenant, operation).Scan(&raw, &storedDigest, &state, &generation, &leaseUntil, &receiptRaw)
	if err != nil {
		return application.UploadClaim{}, mapNotFound(err)
	}
	if storedDigest != digest {
		return application.UploadClaim{}, domain.ErrConflict
	}
	var intent domain.WriteIntent
	if err = json.Unmarshal(raw, &intent); err != nil {
		return application.UploadClaim{}, err
	}
	claim := application.UploadClaim{Intent: intent, Generation: generation, AttemptID: attempt}
	if len(receiptRaw) > 0 {
		var receipt domain.CustodyReceipt
		if err = json.Unmarshal(receiptRaw, &receipt); err != nil {
			return application.UploadClaim{}, err
		}
		claim.Receipt = &receipt
		return claim, tx.Commit()
	}
	if state == domain.StateUploading && leaseUntil.Valid {
		var active bool
		if err = tx.QueryRowContext(ctx, `SELECT lease_until > clock_timestamp() FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2`, tenant, operation).Scan(&active); err != nil {
			return application.UploadClaim{}, err
		}
		if active {
			return application.UploadClaim{}, domain.ErrInProgress
		}
	}
	if state != domain.StatePending && state != domain.StateUnavailable && state != domain.StateUploading {
		return application.UploadClaim{}, domain.ErrNotAvailable
	}
	claim.Generation = generation + 1
	claim.LeaseToken = randomToken()
	claim.ObjectKey = fmt.Sprintf("bc06/%s/%s/%d/%s", tenant, intent.ArtifactID, claim.Generation, claim.LeaseToken)
	err = tx.QueryRowContext(ctx, `UPDATE artifact_custody.artifact_inventory SET state='uploading',generation=$3,attempt_id=$4,lease_token=$5,lease_until=clock_timestamp()+($6 * interval '1 microsecond'),object_key=$7,updated_at=clock_timestamp() WHERE tenant_id=$1 AND operation_id=$2 RETURNING lease_until`, tenant, operation, claim.Generation, attempt, claim.LeaseToken, lease.Microseconds(), claim.ObjectKey).Scan(&claim.LeaseUntil)
	if err != nil {
		return application.UploadClaim{}, err
	}
	if err = tx.Commit(); err != nil {
		return application.UploadClaim{}, err
	}
	return claim, nil
}

func (s *Store) CompleteUpload(ctx context.Context, claim application.UploadClaim, location application.ObjectLocation) (domain.CustodyReceipt, error) {
	if location.Provider == "" || location.Bucket == "" || location.Key == "" || location.Key != claim.ObjectKey {
		return domain.CustodyReceipt{}, errors.New("object location does not match immutable upload claim")
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	defer tx.Rollback()
	var raw []byte
	err = tx.QueryRowContext(ctx, `SELECT intent FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND operation_id=$2 AND state='uploading' AND generation=$3 AND attempt_id=$4 AND lease_token=$5 AND lease_until>clock_timestamp() FOR UPDATE`, claim.Intent.TenantID, claim.Intent.OperationID, claim.Generation, claim.AttemptID, claim.LeaseToken).Scan(&raw)
	if err != nil {
		return domain.CustodyReceipt{}, domain.ErrConflict
	}
	var intent domain.WriteIntent
	if err = json.Unmarshal(raw, &intent); err != nil {
		return domain.CustodyReceipt{}, err
	}
	var confirmedAt time.Time
	if err = tx.QueryRowContext(ctx, `SELECT clock_timestamp()`).Scan(&confirmedAt); err != nil {
		return domain.CustodyReceipt{}, err
	}
	confirmed := confirmedAt.UTC().Truncate(time.Microsecond).Format(time.RFC3339Nano)
	receipt := domain.CustodyReceipt{ContractVersion: domain.ContractVersion, ReceiptID: application.ReceiptID(intent.TenantID, intent.OperationID, claimIntentDigest(intent)), TenantID: intent.TenantID, Artifact: intent.Artifact, Source: intent.Source, Representation: intent.Representation, Classification: intent.Classification, RetentionPolicyRef: intent.RetentionPolicyRef, ConfirmedAt: confirmed}
	receiptJSON, _ := json.Marshal(receipt)
	result, err := tx.ExecContext(ctx, `UPDATE artifact_custody.artifact_inventory SET state='available',object_provider=$6,object_bucket=$7,object_key=$8,object_version_id=$9,receipt=$10,confirmed_at=$11,lease_token=NULL,lease_until=NULL,reconciliation_state=CASE WHEN $12 THEN 'consistent' ELSE reconciliation_state END,last_reconciled_at=CASE WHEN $12 THEN clock_timestamp() ELSE last_reconciled_at END,updated_at=clock_timestamp() WHERE tenant_id=$1 AND operation_id=$2 AND generation=$3 AND attempt_id=$4 AND lease_token=$5 AND state='uploading'`, intent.TenantID, intent.OperationID, claim.Generation, claim.AttemptID, claim.LeaseToken, location.Provider, location.Bucket, location.Key, location.VersionID, receiptJSON, confirmed, claim.Reconciliation)
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	n, _ := result.RowsAffected()
	if n != 1 {
		return domain.CustodyReceipt{}, domain.ErrConflict
	}
	_, err = tx.ExecContext(ctx, `UPDATE artifact_custody.custody_operations SET receipt=$3,updated_at=clock_timestamp() WHERE tenant_id=$1 AND operation_id=$2`, intent.TenantID, intent.OperationID, receiptJSON)
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	event := application.OutboxEvent{ContractVersion: domain.ContractVersion, EventID: "custody-confirmed-" + receipt.ReceiptID, EventSource: "urn:data-mozaic:bc06:artifact-custody", EventType: "data-mozaic.artifact.custody-confirmed.v1", AggregateType: "RawArtifact", AggregateID: intent.ArtifactID, TenantID: intent.TenantID, OccurredAt: confirmed, CorrelationID: intent.OperationID, CausationID: intent.OperationID, ReceiptID: receipt.ReceiptID, Artifact: intent.Artifact}
	eventJSON, _ := json.Marshal(event)
	_, err = tx.ExecContext(ctx, `INSERT INTO artifact_custody.custody_outbox(event_id,tenant_id,artifact_id,event_type,event,occurred_at) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(event_id) DO NOTHING`, event.EventID, intent.TenantID, intent.ArtifactID, event.EventType, eventJSON, confirmed)
	if err != nil {
		return domain.CustodyReceipt{}, err
	}
	if err = tx.Commit(); err != nil {
		return domain.CustodyReceipt{}, err
	}
	return receipt, nil
}

func claimIntentDigest(intent domain.WriteIntent) string { d, _ := intent.IntentDigest(); return d }
func (s *Store) ReleaseUpload(ctx context.Context, c application.UploadClaim, state domain.ArtifactState) error {
	if state != domain.StatePending && state != domain.StateUnavailable && state != domain.StateInconsistent {
		return domain.ErrInvalidIntent
	}
	_, err := s.db.ExecContext(ctx, `UPDATE artifact_custody.artifact_inventory SET state=$6,lease_token=NULL,lease_until=NULL,reconciliation_state=CASE WHEN $7 AND $6='inconsistent' THEN 'inconsistent' WHEN $7 THEN 'pending' ELSE reconciliation_state END,last_reconciled_at=CASE WHEN $7 THEN clock_timestamp() ELSE last_reconciled_at END,attempt_id=NULL,updated_at=clock_timestamp() WHERE tenant_id=$1 AND operation_id=$2 AND generation=$3 AND attempt_id=$4 AND lease_token=$5 AND state='uploading' AND lease_until>clock_timestamp()`, c.Intent.TenantID, c.Intent.OperationID, c.Generation, c.AttemptID, c.LeaseToken, state, c.Reconciliation)
	return err
}

func (s *Store) VerifyArtifact(ctx context.Context, tenant, artifact, digest string, size int64, source domain.SourceRef) (domain.CustodyReceipt, error) {
	var raw []byte
	var state domain.ArtifactState
	err := s.db.QueryRowContext(ctx, `SELECT state,receipt FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND artifact_id=$2`, tenant, artifact).Scan(&state, &raw)
	if err != nil {
		return domain.CustodyReceipt{}, mapNotFound(err)
	}
	if state == domain.StateInconsistent {
		return domain.CustodyReceipt{}, domain.ErrInconsistent
	}
	if state != domain.StateAvailable || len(raw) == 0 {
		return domain.CustodyReceipt{}, domain.ErrNotAvailable
	}
	var receipt domain.CustodyReceipt
	if err = json.Unmarshal(raw, &receipt); err != nil {
		return receipt, err
	}
	if receipt.Artifact.Digest != digest || receipt.Artifact.SizeBytes != size || receipt.Source != source {
		return domain.CustodyReceipt{}, domain.ErrConflict
	}
	return receipt, nil
}
func (s *Store) ReadArtifact(ctx context.Context, tenant, artifact string) (domain.ArtifactMetadata, error) {
	var raw []byte
	var state domain.ArtifactState
	var loc application.ObjectLocation
	var provider, bucket, key, version sql.NullString
	var created time.Time
	var confirmedAt sql.NullTime
	err := s.db.QueryRowContext(ctx, `SELECT intent,state,object_provider,object_bucket,object_key,object_version_id,created_at,confirmed_at FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND artifact_id=$2`, tenant, artifact).Scan(&raw, &state, &provider, &bucket, &key, &version, &created, &confirmedAt)
	if err != nil {
		return domain.ArtifactMetadata{}, mapNotFound(err)
	}
	var intent domain.WriteIntent
	if err = json.Unmarshal(raw, &intent); err != nil {
		return domain.ArtifactMetadata{}, err
	}
	loc.Provider = provider.String
	loc.Bucket = bucket.String
	loc.Key = key.String
	loc.VersionID = version.String
	confirmedText := ""
	if confirmedAt.Valid {
		confirmedText = confirmedAt.Time.UTC().Format(time.RFC3339Nano)
	}
	return domain.ArtifactMetadata{Artifact: intent.Artifact, Source: intent.Source, Representation: intent.Representation, Classification: intent.Classification, RetentionPolicyRef: intent.RetentionPolicyRef, Location: domain.ArtifactLocation{Provider: loc.Provider, Bucket: loc.Bucket, Key: loc.Key, VersionID: loc.VersionID}, State: state, CreatedAt: created.UTC().Format(time.RFC3339Nano), ConfirmedAt: confirmedText}, nil
}

func (s *Store) ListInventory(ctx context.Context, tenant, afterArtifact string, afterCreated time.Time, limit int) ([]application.InventoryItem, error) {
	if tenant == "" || limit < 1 || limit > 101 {
		return nil, errors.New("inventory bounds invalid")
	}
	query := `SELECT operation_id,intent,state,created_at,updated_at,confirmed_at,reconciliation_state
	          FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 ORDER BY created_at,artifact_id LIMIT $2`
	args := []any{tenant, limit}
	if afterArtifact != "" {
		query = `SELECT operation_id,intent,state,created_at,updated_at,confirmed_at,reconciliation_state
		         FROM artifact_custody.artifact_inventory WHERE tenant_id=$1 AND (created_at,artifact_id)>($2,$3)
		         ORDER BY created_at,artifact_id LIMIT $4`
		args = []any{tenant, afterCreated, afterArtifact, limit}
	}
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]application.InventoryItem, 0, limit)
	for rows.Next() {
		var operation string
		var raw []byte
		var state domain.ArtifactState
		var created, updated time.Time
		var confirmed sql.NullTime
		var reconciliation domain.ReconciliationState
		if err := rows.Scan(&operation, &raw, &state, &created, &updated, &confirmed, &reconciliation); err != nil {
			return nil, err
		}
		var intent domain.WriteIntent
		if err := json.Unmarshal(raw, &intent); err != nil {
			return nil, err
		}
		item := application.InventoryItem{OperationID: operation, Artifact: intent.Artifact, Source: intent.Source, State: state, CreatedAt: created.UTC().Format(time.RFC3339Nano), UpdatedAt: updated.UTC().Format(time.RFC3339Nano), ReconciliationState: reconciliation}
		if confirmed.Valid {
			item.ConfirmedAt = confirmed.Time.UTC().Format(time.RFC3339Nano)
		}
		items = append(items, item)
	}
	return items, rows.Err()
}
func (s *Store) ClaimReconciliation(ctx context.Context, limit int, lease time.Duration) ([]application.UploadClaim, error) {
	if limit < 1 || limit > 100 || lease < time.Second || lease > time.Hour {
		return nil, errors.New("reconciliation bounds invalid")
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT tenant_id,operation_id,intent,intent_digest,generation,object_key FROM artifact_custody.artifact_inventory WHERE state='uploading' AND lease_until<=clock_timestamp() ORDER BY lease_until,tenant_id,operation_id FOR UPDATE SKIP LOCKED LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	type expired struct {
		tenant, operation, key, digest string
		raw                            []byte
		generation                     int64
	}
	items := []expired{}
	for rows.Next() {
		var item expired
		if err = rows.Scan(&item.tenant, &item.operation, &item.raw, &item.digest, &item.generation, &item.key); err != nil {
			rows.Close()
			return nil, err
		}
		items = append(items, item)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	claims := make([]application.UploadClaim, 0, len(items))
	for _, item := range items {
		var intent domain.WriteIntent
		if err = json.Unmarshal(item.raw, &intent); err != nil {
			return nil, err
		}
		token := randomToken()
		attempt := "reconcile-" + token
		generation := item.generation + 1
		var until time.Time
		err = tx.QueryRowContext(ctx, `UPDATE artifact_custody.artifact_inventory SET generation=$3,attempt_id=$4,lease_token=$5,lease_until=clock_timestamp()+($6 * interval '1 microsecond'),reconciliation_state='pending',updated_at=clock_timestamp() WHERE tenant_id=$1 AND operation_id=$2 RETURNING lease_until`, item.tenant, item.operation, generation, attempt, token, lease.Microseconds()).Scan(&until)
		if err != nil {
			return nil, err
		}
		claims = append(claims, application.UploadClaim{Intent: intent, Generation: generation, AttemptID: attempt, LeaseToken: token, ObjectKey: item.key, LeaseUntil: until, Reconciliation: true})
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return claims, nil
}
func (s *Store) ListPendingEvents(ctx context.Context, tenantID string, limit int) ([]application.OutboxEvent, error) {
	if tenantID == "" {
		return nil, errors.New("pending event tenant is required")
	}
	if limit < 1 || limit > 100 {
		return nil, errors.New("pending event limit must be 1..100")
	}
	rows, err := s.db.QueryContext(ctx, `SELECT event FROM artifact_custody.custody_outbox WHERE tenant_id=$1 AND state='pending' ORDER BY occurred_at,event_id LIMIT $2`, tenantID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	events := []application.OutboxEvent{}
	for rows.Next() {
		var b []byte
		var e application.OutboxEvent
		if err = rows.Scan(&b); err != nil {
			return nil, err
		}
		if err = json.Unmarshal(b, &e); err != nil {
			return nil, err
		}
		events = append(events, e)
	}
	return events, rows.Err()
}

func randomToken() string {
	var b [24]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic("crypto/rand unavailable: " + err.Error())
	}
	return hex.EncodeToString(b[:])
}
func mapNotFound(err error) error {
	if errors.Is(err, sql.ErrNoRows) {
		return domain.ErrNotAvailable
	}
	return err
}
