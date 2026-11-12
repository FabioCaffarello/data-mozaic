# Data Mozaic

Data Mozaic é uma fundação orientada por contratos para aquisição e engenharia de dados. O repositório contém o workflow offline Scout S1, contratos draft, infraestrutura PostgreSQL de recuperação e checks de desenvolvimento. Não oferece navegação real, serviço de captura produtivo ou runtime de agente.

Comece pelo [índice de arquitetura](docs/architecture/README.md), [roadmap atual](docs/roadmap/implementation.md) e [baseline do repositório](docs/architecture/assessments/repository-baseline.md). O baseline e os assessments S0–S1 são snapshots históricos; use o roadmap para o estado atual. As decisões de arquitetura permanecem Proposed, salvo indicação explícita em cada registro.

As dependências Python usam o workspace uv. Execute `make sync` e depois `make check` para os checks offline do repositório. `make ci` é a entrada do CI. O profile PostgreSQL local é opcional e requer Docker e um `.env` local ignorado pelo Git; veja [operações de recovery do Scout](docs/operations/scout-recovery.md).

A demonstração do Scout é offline e usa fixtures:

```sh
PYTHONPATH=apps/scout uv run --locked python -m scout
```

Ela exercita o workflow BC14 e a projeção da proposta. Bytes de fixture são uma entrada de demonstração, não uma interface pública de authoring via rede. Os schemas Scout v1 permanecem draft.
