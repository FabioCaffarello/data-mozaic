UPDATE artifact_custody.custody_outbox
SET event = jsonb_set(
    jsonb_set(event - 'state', '{contract_version}', to_jsonb('artifact-custody.v1'::text)),
    '{receipt_id}',
    to_jsonb(substr(event_id, length('custody-confirmed-') + 1))
)
WHERE event_type = 'data-mozaic.artifact.custody-confirmed.v1'
  AND NOT (event ? 'contract_version');
