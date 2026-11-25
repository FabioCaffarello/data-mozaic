# Repository consolidation — 2026-11-24

## Scope and verified baseline

This assessment reconciles the registered Data Mozaic worktrees, recent branches, squash-merged PRs and the previously separate fixture-only Execution Coordination worktree. It preserves the earlier implementation assessments as historical snapshots.

`git fetch origin` completed without changing a working tree. The verified remote baseline is `origin/main` at `637f84532a9323a496d4992404e81696939f4d44` (`feat(artifact-custody): add BC06 custody foundation (#18)`). The local branch named `main` is clean at `138e90bdcaf7bf7c2972a1efccc9fff824de9494`, one commit behind; it was not moved. The active durable BC04 worktree is based exactly on `origin/main` and contains the uncommitted BC04 increment. No credentials were inspected.

## Squash-merge evidence

Ahead/behind counts are not used as an integration verdict. The repository squashes PRs, so their source commits do not become ancestors of the resulting main commit. For PRs listed below, the PR source head tree and the reported squash-merge commit tree were byte-for-byte identical under `git diff --quiet <head> <merge>`:

| PR | Source head | Squash merge on main | Result |
|---|---|---|---|
| [#8](https://github.com/FabioCaffarello/data-mozaic/pull/8) | `9ebf57f49a91502b776ce73d1ec063a2cc0ccb29` | `5c3e2b14f758b1956e6ef9acb5d937b12109f234` | Integrated |
| [#9](https://github.com/FabioCaffarello/data-mozaic/pull/9) | `189f6ea810dafdb515f2e42d08c2dec7aaed2a0a` | `b483ad680b98b4fa22c1522f6a7d4a9d08243b76` | Integrated |
| [#10](https://github.com/FabioCaffarello/data-mozaic/pull/10) | `448ac520e2cd35368e128c2977fc6ca041ab4dcf` | `a11d5142231e8b8ad5c9403732d9cb4aa209e29d` | Integrated |
| [#12](https://github.com/FabioCaffarello/data-mozaic/pull/12) | `eb565bae130ea6fbaed3bec33dbdd7c70d6b0463` | `ca2a1a589daaec789972a8cb9fc4f08752394b5e` | Integrated |
| [#13](https://github.com/FabioCaffarello/data-mozaic/pull/13) | `5af15bd7f215c1694d59cd32ab9c4699541d187f` | `a07e393354f49d43eb95135c607f2077f93c1f94` | Integrated |
| [#15](https://github.com/FabioCaffarello/data-mozaic/pull/15) | `ded4640610670c139e66cf47daaf10db6e200e72` | `427a027b5b3894cc7ef926f155be671ca73a7d0a` | Integrated |
| [#16](https://github.com/FabioCaffarello/data-mozaic/pull/16) | `ade602d0f17d3245042a98d956212557b7925818` | `c61f268ac968cb6f2822db1eec793718e22778b6` | Integrated |
| [#17](https://github.com/FabioCaffarello/data-mozaic/pull/17) | `5def724dd7066535f1875ed25b6ba69aa6ece16d` | `138e90bdcaf7bf7c2972a1efccc9fff824de9494` | Integrated |
| [#18](https://github.com/FabioCaffarello/data-mozaic/pull/18) | `66be6cf3c26dfb16c2ed9e617f025a3001ee6915` | `637f84532a9323a496d4992404e81696939f4d44` | Integrated |

The closed PR inventory has no PR #14. Recent PR #16–#18 each report successful Quality Gate and Integration Security Gate checks; #17 additionally reports BC05 MongoDB integration, and #18 additionally reports BC05 MongoDB integration, BC06 Go unit/race/vulnerability, and BC06 PostgreSQL/RustFS interoperability. These are GitHub check results for the exact PR heads, not local re-execution claims.

## Worktree and branch classification

| Branch / registered worktree | HEAD and upstream relationship | State / PR | Classification and evidence | Disposition |
|---|---|---|---|---|
| `feat/acquisition-output-foundation` — `/Volumes/OWC Express 1M2/Develop/data-mozaic` | `ade602d`; equal to `origin/feat/acquisition-output-foundation`; local `main` is separately one commit behind | Clean; PR #16 | Integrated; source tree exactly matches squash `c61f268` | Preserve existing worktree; do not use it as the current baseline |
| `feat/acquisition-output-mongodb` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-mongodb` | `5def724`; equal to its remote branch | Clean; PR #17 | Integrated; source tree exactly matches squash `138e90b` | Preserve branch, worktree and Mongo volume |
| `feat/artifact-custody-foundation` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-artifact-custody` | `66be6cf`; equal to its remote branch | Clean; PR #18 | Integrated; source tree exactly matches squash `637f845` | Preserve branch, worktree and volumes |
| `feat/execution-coordination-fixture-v0` — `/private/tmp/data-mozaic-execution-coordination` | `ded4640`; graph reports 14 ahead / 4 behind `origin/main` | Clean; PR #15 | Integrated. Its HEAD is exactly PR #15's head and its entire tree equals squash `427a027`; all 14 ahead commits are the source history of that PR, not 14 missing changes | Preserve. No cherry-pick or merge is needed |
| `feat/execution-coordination-durable` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-execution-coordination-durable` | `637f845`; equal to `origin/main` | Dirty by design; no PR | Work in progress. Tracked and untracked files are the authorized BC04 implementation plus its review documentation | Preserve all contents; continue only after this inventory |
| `feat/collector-playbook-v2` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-pr10` | `448ac52`; equal to remote branch | Clean; PR #10 | Integrated; tree equals squash `a11d514` | Preserve |
| `feat/collector-scrapy-fixture-backend` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-scrapy` | `eb565ba`; equal to remote branch | Clean; PR #12 | Integrated; tree equals squash `ca2a1a5` | Preserve |
| `feat/scrapy-collector-multibot` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-scrapy-multibot` | `5af15bd`; equal to remote branch | Clean; PR #13 | Integrated; tree equals squash `a07e393` | Preserve |
| `feat/scout-codex-playbook-authoring` — detached `/Volumes/OWC Express 1M2/.data-mozaic-pr9-generalization-72058db` | Worktree HEAD `72058db`; detached; clean. The remote branch head `189f6ea` has no separate worktree | PR #9 integrated; local-only commit `72058db` follows `81527e9` | Partially integrated / superseded. Transcript-v2 schemas, fixtures, observation identity fields and core validation files touched by `72058db` are byte-identical to current main. The remaining compiler, MCP host, tests and guidance changes evolved in merged PR #10 and subsequent PRs. Its `scout-codex-functional-remediation.md` is historical session evidence, not an unintegrated runtime dependency | Preserve the detached worktree; do not merge this old snapshot over later implementations. Record the evidence-only assessment as not ported |
| `feat/scout-authorized-http-exploration` — registered `/private/tmp/data-mozaic-pr8-review` | Metadata HEAD `9284455`; directory and gitdir target are missing | PR #8 integrated at `5c3e2b1`; published head `9ebf57f` | Stale metadata. `9284455` is an earlier pre-final version of the same PR; published head tree equals the squash commit. The missing worktree cannot be inspected, but its referenced Git object remains readable | Document; do not prune or recreate the worktree |

Other local/remote branches without registered worktrees are historical source branches for merged PRs #1–#7, #8–#10, #12–#13, and #15–#18, or the bootstrap commit. Their current remote heads do not identify an open implementation PR. PR #11 and #14 are absent from the GitHub PR inventory. No unpublished branch with an unexplained recent worktree was found.

## BC06 evidence review

PR #18's merged implementation and exact-head checks cover real PostgreSQL/RustFS integration, conditional overwrite denial, immutable generation-bound object keys, concurrent independent writers, stale completion rejection, replayed receipts, missing and divergent object classification, post-object-write recovery, and object-copy/read-back alongside isolated PostgreSQL schema restore. The CI also exercised the Python BC05 client against MongoDB and BC06.

The implementation enforces a 100 MiB per-object cap, concurrency semaphore and aggregate spool budget, uses a bounded temporary file and conditional single-part S3 write, and bounds downloads with a deadline. Multipart is absent. The backup evidence uses isolated resources but is not one coordinated cross-store snapshot. The benchmark evidence is explicitly small-sample and does not attribute Go heap or temporary-disk peak. An interrupted process during an incomplete streaming request and a coordinated PostgreSQL/object-volume point-in-time restore were not demonstrated by the merged evidence. These gaps do not block BC04's synthetic local finalization work, which consumes durable receipts through BC06's public HTTP contract; they remain operational limitations, not claims of acceptance.

## Gate A disposition

All recent registered worktrees are accounted for. The only divergent fixture branch is semantically identical to its merged PR; the detached PR #9 follow-up has no remaining runtime behavior missing from current main; and the stale PR #8 entry points to an inaccessible, superseded worktree. No independent residual implementation warrants a reconciliation PR. Uncommitted BC04 work and all existing branches, worktrees, credentials and persistent volumes remain preserved. The working baseline for the next gate is `origin/main` / `637f84532a9323a496d4992404e81696939f4d44`.

## ADR state

ADR 022, ADR 023 and ADR 024 remain Proposed. The current main contains ADR 023 from PR #18; ADR 024 remains part of the uncommitted BC04 increment. This assessment does not accept or revise ADR status.

## Post-PR #19 and Gate C worktree amendment — 2026-11-25

`git fetch origin` confirmed `origin/main` at `1db31b3e32390202496befff49bef7c05bd10769`, the squash merge of PR #19. PR #19 head `35ed140dba564e8cf073807f865536a12213ec41` and merge tree are byte-for-byte equal. The PR is merged, targeting `main`, and its Quality Gate, Integration Security Gate, BC05 MongoDB Integration Gate, BC06 Go Unit/Race/Vulnerability Gates and BC06 PostgreSQL/RustFS and BC05/MongoDB interoperability checks all report success. No review approval was required by the repository ruleset; no protection was bypassed.

The ahead/behind values below are Git graph counts against current `origin/main`. Because the repository squash-merges, these values are topology evidence only; the PR/source-tree comparisons above remain the integration evidence.

| Branch / worktree | HEAD | Ahead / behind `origin/main` | Working tree | PR / classification / disposition |
|---|---|---:|---|---|
| `main` — no associated worktree | `138e90b` | 0 / 2 | No worktree | Local branch ref is intentionally untouched and behind current origin; do not use as Gate C base |
| `feat/acquisition-output-foundation` — `/Volumes/OWC Express 1M2/Develop/data-mozaic` | `ade602d` | 2 / 4 | Clean | PR #16 integrated by exact tree match; preserve worktree |
| `feat/acquisition-output-mongodb` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-mongodb` | `5def724` | 2 / 3 | Clean | PR #17 integrated by exact tree match; preserve worktree and volume |
| `feat/artifact-custody-foundation` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-artifact-custody` | `66be6cf` | 3 / 2 | Clean | PR #18 integrated by exact tree match; preserve worktree and volumes |
| `feat/execution-coordination-fixture-v0` — `/private/tmp/data-mozaic-execution-coordination` | `ded4640` | 14 / 5 | Clean | PR #15 integrated; all 14 commits are represented by its squash tree; preserve and do not merge |
| `feat/execution-coordination-durable` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-execution-coordination-durable` | `35ed140` | 1 / 1 | Clean | PR #19 integrated; source head tree matches squash; preserve branch/worktree |
| `feat/acquisition-fixture-integration` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-acquisition-fixture-integration` | `1db31b3` | 0 / 0 | Intentional Gate C modifications | Work in progress, no PR; based exactly on verified main; generated fixture assembly/results are retained under a narrowly ignored `.local/` path |
| `feat/collector-playbook-v2` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-pr10` | `448ac52` | 5 / 8 | Clean | PR #10 integrated by exact tree match; preserve |
| `feat/collector-scrapy-fixture-backend` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-scrapy` | `eb565ba` | 13 / 7 | Clean | PR #12 integrated by exact tree match; preserve |
| `feat/scrapy-collector-multibot` — `/Volumes/OWC Express 1M2/Develop/data-mozaic-scrapy-multibot` | `5af15bd` | 5 / 6 | Clean | PR #13 integrated by exact tree match; preserve |
| `feat/scout-authorized-http-exploration` — stale `/private/tmp/data-mozaic-pr8-review` metadata | `9284455` (inaccessible worktree); published branch `9ebf57f` | 5 / 10 for published branch | Unavailable | PR #8 integrated; stale metadata remains untouched because the registered directory is missing |
| `feat/scout-codex-playbook-authoring` — detached `/Volumes/OWC Express 1M2/.data-mozaic-pr9-generalization-72058db` | `72058db` (remote branch `189f6ea`) | 21 / 9 for remote branch | Detached and clean | PR #9 integrated; local follow-up is superseded by PR #10+ and adds no missing runtime behavior; preserve |

PR #19 provides the merge gate for BC04. The Gate C branch is the only active dirty implementation worktree. Its local tests and real local infrastructure flow are documented in [the fixture acquisition assessment](fixture-acquisition-integration-2026-11-25.md). No Gate C PR, commit or push has been made. Existing credentials, worktrees and volumes remain preserved; the stale PR #8 metadata has not been pruned.
