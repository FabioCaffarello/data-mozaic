# Data Mozaic

Data Mozaic é uma fundação orientada por contratos para aquisição e engenharia de dados. O repositório contém o workflow Scout de autoria local, contratos draft, infraestrutura PostgreSQL de recuperação e checks de desenvolvimento. O host MCP STDIO experimental suporta autoria incremental sobre fixtures e exploração HTTPS autorizada via Squid, exportação de transcript BC14 e replay offline de draft BC15. Source/policy e custody são adapters locais de referência, não integrações BC01/BC06/BC11 produtivas. Não há sandbox para o processo de um agente, serviço de captura produtivo ou runtime de agente.

Comece pelo [índice de arquitetura](docs/architecture/README.md), [roadmap atual](docs/roadmap/implementation.md) e [baseline do repositório](docs/architecture/assessments/repository-baseline.md). O baseline e os assessments S0–S1 são snapshots históricos; use o roadmap para o estado atual. As decisões de arquitetura permanecem Proposed, salvo indicação explícita em cada registro.

As dependências Python usam o workspace uv. Execute `make sync` e depois `make check` para os checks offline do repositório. `make ci` é a entrada do CI. O profile PostgreSQL local é opcional e requer Docker e um `.env` local ignorado pelo Git; veja [operações de recovery do Scout](docs/operations/scout-recovery.md). Os limites do adapter HTTP estão em [ferramentas de authoring](docs/operations/scout-authoring-tools.md); a prova opcional de egress isolado requer Docker (`make scout-sandbox-check`).

A demonstração base do Scout e o replay BC15 são offline. A exploração HTTPS é um fluxo opcional de desenvolvimento, explicitamente autorizado por source/revision e policy local, e passa pelo MCP e Squid; ela não é navegação pública genérica. Veja [operações de authoring](docs/operations/scout-authoring-tools.md) e [drafts BC15](docs/operations/capture-playbook-drafts.md). O teste determinístico de plataforma não representa uma execução ao vivo do Codex.

Demo Scout:

```sh
PYTHONPATH=apps/scout uv run --locked python -m scout
```

Ela exercita o workflow BC14 e a projeção da proposta. Bytes de fixture são uma entrada de demonstração, não uma interface pública de authoring via rede. Os schemas Scout v1 permanecem draft.
