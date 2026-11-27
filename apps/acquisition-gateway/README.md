# Acquisition Gateway — controlled foundation

This Go deployable owns the source-facing HTTP request. Workers submit one
bounded JSON `POST /v1/fetch` containing a source URL and a BC04-signed bearer
capability. The gateway accepts only GET source requests and returns the
bounded decoded entity body plus fetch identity headers. It does not implement
CONNECT, arbitrary TCP forwarding, client-selected credentials, or a worker
selected policy.

The gateway verifies Ed25519 execution capabilities, binds the policy digest
and source revision, revalidates every redirect, resolves and checks all
addresses, then dials the validated address while retaining the URL host for
TLS verification. Ordinary policy permits only HTTPS public destinations. The
controlled topology uses a test-only private CA and an exact `/32`
destination allowance. Test roots are only applied to `test-fixture-only`
policies; ordinary policies retain the system trust store and public-address
checks.

Request count, response-byte reservations, concurrency and rate are shared by
tenant/run/policy. Redirect hops consume request and byte budget and require a
new BC04 operation admission. Retries are disabled. Incomplete requests
conservatively consume the reserved response size. Quota state is fsync-backed
and survives restart; in-flight reservations are charged on recovery. A
gateway restart invalidates all old capabilities and requires the attempt to
be recovered and re-authorized.

Upstream bodies are read in fixed 64 KiB chunks into private `.part` files while
size and SHA-256 are calculated. The body is fsynced and renamed before bounded
evidence metadata is made visible. The gateway reserves the complete
policy-authorized response allowance against its configured spool capacity and
entry bound before dialing; a unique reservation identity prevents duplicate
release from reducing another operation's reservation. Capacity is local to a
single gateway process and rebuilt from completed files at startup. Interrupted
and unpaired temporary files are removed after safe-file checks. Completed
evidence is retained; no TTL is applied while recovery dependencies remain
unknown. Authenticated `GET /internal/v1/resources` exposes only component-wide
used/reserved bytes, entry counts, configured bounds, active source fetches and
active evidence transfers. Evidence retrieval admits at most four concurrent
transfers and rejects excess work with `429`; it does not queue requests. It
exposes no tenant or evidence identifiers.

The raw-only worker may request an evidence-reference response. The gateway
still acquires and verifies the full authorized response and stores authoritative
FetchEvidence; only the bounded metadata/reference is returned to that worker.
Structured profiles still receive the complete bounded body for extraction.
The trusted importer independently retrieves the exact attempt-bound evidence.
See the [M7 resource evidence](../../docs/architecture/assessments/acquisition-resource-maturity-2026-11-27.md)
for limits and controlled measurements.

For ordinary policies the gateway requires `BC04HTTPAuthorizer`. It sends the
signed capability, a unique operation ID, and a digest of the normalized
target to BC04's narrow authorization endpoint before the upstream dial. BC04
verifies the capability and records the admission in PostgreSQL under locks
shared with attempt cancellation and lease transitions. That commit is the
linearization point: later operations are denied after cancellation or stale
generation; a request already admitted at that point may finish, and a network
request already in flight is not retroactively canceled. Endpoint failure is
fail-closed. The bearer credential is mounted only in the trusted gateway and
BC04 coordinator. The static authorizer switch is limited to the isolated
controlled transport smoke and accepts only `test-fixture-only` policies; it
does not prove durable BC04 freshness.

## Controlled container smoke

Run `make acquisition-live-gateway-container-smoke`. It builds the Go gateway,
deterministic controlled source, and separate Scrapy runtime image; starts
separate worker/gateway, gateway/source, and gateway-egress networks; compiles
and executes the JSON example as a live BotBuild through Scrapy and the gateway;
and rejects direct source IP/name access, persistence-service resolution and
host-port access, and unauthorized-origin access from the worker container.
The capability, signing key and test TLS key are generated ephemerally by the
harness; no test private key is committed and no external source is contacted.
The named quota/evidence volume is preserved
when the smoke stops its containers.

The Compose profile is not a production deployment recipe. Its gateway policy
and verification key are operator-mounted files. The container smoke above is
a lower-level isolated transport test with a test-only authorizer. The separate
`make authorized-live-json-acquisition` gate creates a test-scoped signed
authority snapshot and BotRelease, admits a durable BC04 run, issues a current
attempt capability, and uses BC04 HTTP operation admission for each gateway
fetch. It then validates gateway-owned evidence, transfers the decoded entity
through BC06, stages and publishes two BC05 outputs, completes BC04, and
independently reads the outputs and verifies the retrieved artifact digest.
Worker-declared evidence remains untrusted. The trusted importer reads evidence
from the bounded gateway spool using an importer-only token and checks exact
attempt binding and body digest before custody.

`make authorized-live-html-acquisition` runs the same durable flow with the
controlled HTML endpoint, HTML selector and independent expected record. The
live backend also supports the existing typed `links.discover` plus
`pagination.bounded` graph for bounded HTML next-URL traversal. The compiler
requires an explicit stop condition and page/request budgets. Each discovered
URL is checked against playbook source scope and is independently reauthorized
by BC04 and the gateway policy before its request. The importer checks the
trusted body sequence, fetch membership, record provenance and termination.
`make authorized-live-pagination-acquisition` runs that path against two
controlled TLS pages and publishes both response artifacts through one
OutputSet. `make authorized-live-pagination-planned-acquisition` exercises a
separate authorized bounded window: the trusted playbook permits exactly two
pages, both responses and provenance are verified, and the final continuation
is intentionally left for a later window. This is reported as bounded-window
completion, not as exhaustion of the source. A natural-end plan that reaches
its page limit fails instead of being promoted to successful completion.

The controlled live response-failure harness uses the same compiled worker,
BC04 admission, gateway, evidence validation and durable run lifecycle:

```sh
make authorized-live-scenario CASE=oversized
make authorized-live-scenario CASE=slow
make authorized-live-scenario CASE=malformed
make authorized-live-scenario CASE=invalid-output
make authorized-live-scenario CASE=interrupted-body
make authorized-live-scenario CASE=pagination-incomplete
make authorized-live-scenario CASE=gateway-restart
```

Each expected failure must leave BC04 failed with no publication receipt.
The restart case restarts the actual gateway container during paginated
execution and then retries with the old capability, which must be rejected.
Gateway unit tests separately verify that consumed aggregate request budgets
survive process restart and in-flight reservations are charged conservatively.
`make authorized-live-redirect-acquisition`,
`make authorized-live-redirect-denial`, and
`make authorized-live-redirect-budget-exhaustion` cover controlled redirect
success and denial. `make authorized-live-publication-ack-loss` commits to
BC05, drops the acknowledgment, reconciles the same durable receipt, and
replays the result without changing its OutputSet or manifest identity.

`make authorized-live-custody-interruption` and
`make authorized-live-partial-stage-interruption` inject deterministic
exceptions at the actual BC06/BC05 importer boundary. They are not process
crash tests: they verify retained custody and that partial OutputSets remain
invisible until governed publication. `make acquisition-gateway-go-vuln`
runs the pinned `govulncheck` tool against the gateway module.

The end-to-end command is controlled-test-only; it is not a production
approval workflow, and its generated test authority does not authorize a real
source. Controlled JSON, single-response HTML and bounded next-URL pagination
are covered end to end. Offset pagination and arbitrary multi-link frontiers
are not supported by this first live profile.
Neither smoke nor integration evidence implies external-source authorization
or production readiness.
