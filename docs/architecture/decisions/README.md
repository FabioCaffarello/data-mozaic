# Registro de decisões

Todos os registros estão **Proposed**; não há decisões Accepted, Rejected ou Superseded. Merge de implementação, CI verde ou evidência técnica local não aprovam ADR. A aprovação humana e os critérios de validação são pré-requisitos para consolidar escolhas, não para manter documentação.

| ADR | Escopo | Estado |
|---|---|---|
| [001](001-boundaries-monorepo.md) | planos, contextos, monorepo, linguagens e distribuição | Proposed |
| [002](002-authoring-runtime.md) | playbook, IR, interpretação, SDK e releases | Proposed |
| [003](003-distributed-capture.md) | intake/frontier, persistência e mensageria | Proposed |
| [004](004-contracts-custody-security.md) | contratos, raw, segurança e agentes | Proposed |
| [005](005-engineering-validation.md) | toolchains, CI e testes arquiteturais | Proposed |
| [006](006-scout-module-contracts.md) | localização interna do BC14 e contratos draft | Proposed |
| [007](007-scout-durable-recovery.md) | recuperação durável de comandos Scout | Proposed |
| [008](008-scout-command-receipts.md) | persistência, receipts e outbox Scout | Proposed |
| [009](009-scout-local-postgres-profile.md) | perfil local PostgreSQL | Proposed |
| [010](010-shared-library-architecture.md) | organização de libraries compartilhadas | Proposed |
| [011](011-scout-outbox-relay-protocol.md) | lease, fencing e retry do relay Scout | Proposed |
| [012](012-scout-controlled-discovery-tools.md) | ferramentas HTTP de descoberta com política local | Proposed |
| [013](013-capture-playbook-draft-contract.md) | artefato draft de CapturePlaybook com ownership BC15 | Proposed |
| [014](014-scout-egress-sandbox.md) | isolamento de egress para o tool worker Scout | Proposed |

Use o [template](template.md) para alterações que imponham restrições novas. Decisões menores e reversíveis podem permanecer nos documentos de arquitetura até existir evidência suficiente para ADR.
7. [Recuperação durável de comandos Scout](007-scout-durable-recovery.md)
8. [Persistência durável e protocolo de receipts/outbox Scout](008-scout-command-receipts.md)
9. [Perfil local PostgreSQL do Scout](009-scout-local-postgres-profile.md)
10. [Organização e promoção de bibliotecas compartilhadas](010-shared-library-architecture.md)
