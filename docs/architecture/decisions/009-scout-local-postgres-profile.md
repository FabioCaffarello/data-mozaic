# ADR 009 — Perfil local PostgreSQL do Scout

- Status: Proposed.
- Contexto: S1.2 requer repetição local de testes PostgreSQL sem tornar Docker requisito para checks unitários ou demo offline.
- Decisão proposta: um Compose canônico em `deployments/compose/postgres.compose.yaml`, imagem oficial `postgres:18.4`, porta configurável publicada somente em loopback (padrão 55432), volume nomeado em `/var/lib/postgresql`, readiness via `pg_isready`, e `stop_grace_period` de 30 segundos. Credenciais `.env` locais ignoradas pelo Git, derivadas do placeholder `.env.example`; sem credenciais de produção. O Scout continua rodando no host por `uv`.
- Evidência local: daemon `linux/arm64` inspecionou RepoDigest `postgres@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636`; a configuração permanece na tag patch `18.4` para permitir o manifesto multi-arquitetura do host/CI.
- Migrations são SQL explícito numerado e aplicadas por runner psycopg com checksum SHA-256 e advisory transaction lock. Compose não executa migrations no entrypoint. `db-down` não remove volumes.
- Alternativas: SQLite para testes; containerizar todo o app; imagem PostgreSQL `latest`; executar migrations no entrypoint. SQLite não prova o CAS PostgreSQL; containerizar o app não ajuda o caso de uso atual; tags flutuantes reduzem reprodutibilidade; entrypoint só executa em diretório vazio e confunde bootstrap com evolução de schema.
- Consequências: Docker é requisito apenas para perfil local de integração; `make check` e CLI offline permanecem executáveis sem daemon. CI usa o mesmo major/patch e o runner de migration. Integration harness cria banco aleatório próprio e só o remove ao terminar.
- Riscos / gate: Compose e persistência de volume foram exercitados no daemon local; CI precisa confirmar disponibilidade da tag 18.4. O perfil não é produção e não define backup, TLS, rotação, retenção ou exposição externa. ADR requer aprovação humana antes de uso além de desenvolvimento.
