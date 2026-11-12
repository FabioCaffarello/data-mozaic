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
| Branches | conventional work prefix + concise domain/task slug | `chore/repository-governance` |
| Commits and PR titles | Conventional Commit type, explicit lowercase scope, concise description | `feat(scout): persist command receipts` |

The repository checks Python module filenames under first-party source roots for `snake_case` and reject generic `common`, `utils`, `helpers`, and `shared` modules. They also validate architecture imports and migration sequence/slug. Public contract paths, schema IDs, and fixture names are not normalized by a generic filename rule. A path rename requires a migration map covering imports, documentation, CI, schema IDs, fixtures and consumers; published v1 contract paths are not renamed for style.
