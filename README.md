# Data Mozaic

Data Mozaic é uma fundação orientada por contratos para aquisição e engenharia de dados. O repositório contém o workflow offline Scout S1, contratos draft, infraestrutura PostgreSQL de recuperação e checks de desenvolvimento. Há um adapter HTTP de authoring experimental e uma topologia Docker opcional de egress isolado; sem MCP/tool host, source aprovada ou adapters BC06/BC11, navegação não está operacional. Não há sandbox para o processo de um agente, serviço de captura produtivo ou runtime de agente.

Comece pelo [índice de arquitetura](docs/architecture/README.md), [roadmap atual](docs/roadmap/implementation.md) e [baseline do repositório](docs/architecture/assessments/repository-baseline.md). O baseline e os assessments S0–S1 são snapshots históricos; use o roadmap para o estado atual. As decisões de arquitetura permanecem Proposed, salvo indicação explícita em cada registro.

As dependências Python usam o workspace uv. Execute `make sync` e depois `make check` para os checks offline do repositório. `make ci` é a entrada do CI. O profile PostgreSQL local é opcional e requer Docker e um `.env` local ignorado pelo Git; veja [operações de recovery do Scout](docs/operations/scout-recovery.md). Os limites do adapter HTTP estão em [ferramentas de authoring](docs/operations/scout-authoring-tools.md); a prova opcional de egress isolado requer Docker (`make scout-sandbox-check`).

A demonstração do Scout e a validação/replay de CapturePlaybook draft são offline e usam fixtures. Veja [operações de drafts BC15](docs/operations/capture-playbook-drafts.md). Nenhum desses comandos navega pela rede.

Demo Scout:

```sh
PYTHONPATH=apps/scout uv run --locked python -m scout
```

Ela exercita o workflow BC14 e a projeção da proposta. Bytes de fixture são uma entrada de demonstração, não uma interface pública de authoring via rede. Os schemas Scout v1 permanecem draft.
