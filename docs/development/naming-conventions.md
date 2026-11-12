# Naming conventions

These conventions apply to new paths and identifiers. Existing public contract filenames, schema `$id` values, imports, and established paths remain unchanged unless a reviewed migration explicitly covers all consumers.

| Kind | Convention | Example |
|---|---|---|
| Directories and Markdown | `kebab-case`, descriptive and unambiguous | `scout-s1-2-durable-recovery.md` |
| Python modules, functions, variables | `snake_case` | `command_receipts.py` |
| Python types | `PascalCase` | `ScoutSession` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_ATTEMPTS` |
| Apps/services/workers | kebab-case filesystem path; valid Python package inside | `apps/scout/`, import `scout` |
| Public JSON contracts | preserve published versioned names; new names follow the owning contract family | `contracts/scout/v1/` |
| ADRs | next available zero-padded number plus kebab-case slug | `010-shared-library-architecture.md` |
| SQL migrations | monotonic fixed-width sequence plus slug, never renumbered | `001-durable-state.sql` |
| Branches | `feat/`, `fix/`, or `docs/` + domain and increment | `feat/scout-s1` |
| Commits | conventional type and explicit scope | `feat(scout): persist command receipts` |

The CI checks Markdown links, Python formatting/lint, migration sequence uniqueness, and presence of descriptive migration slugs. It deliberately does not enforce broad filename regexes that can reject valid multilingual or contract-owned names. A path rename requires a migration map covering imports, documentation, CI, schema IDs, fixtures and consumers; published v1 contract paths are not renamed for style.
