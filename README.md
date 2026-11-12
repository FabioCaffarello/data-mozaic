# Data Mozaic

Fundação arquitetural proposta para uma plataforma modular de aquisição e engenharia de dados. Este repositório contém documentação, contratos draft e verificações da fundação; não há serviços de captura implementados.

Comece pelo [índice de arquitetura](docs/architecture/README.md), pela [auditoria inicial](docs/architecture/assessments/repository-baseline.md) e pelo [plano incremental](docs/roadmap/implementation.md). Todas as decisões técnicas estão propostas e dependem de revisão.

O desenvolvimento Python usa uv e um workspace único. Com uv e Make instalados, execute `make sync` e `make hooks-install`; use `make check` para validar localmente e `make ci` para reproduzir a entrada do CI. O hook pre-commit chama `make check`. O fluxo de contribuição usa branches e pull requests; detalhes em [padrões de engenharia](docs/development/standards.md).

O verificador de documentação cobre links Markdown locais e blocos de código delimitados, não a semântica dos diagramas ou a correção da arquitetura.

O [Scout S1](docs/architecture/scout.md) possui demonstração offline com fixtures e fakes: `PYTHONPATH=apps/scout uv run --locked python -m scout`. Não há navegação nem agente.

O [S1.1](docs/architecture/assessments/scout-s1-1.md) fortalece idempotência e CAS somente no processo local; não oferece recuperação durável ou consistência distribuída.

O profile local e os gates de recuperação S1.2b estão descritos em [avaliação S1.2b](docs/architecture/assessments/scout-s1-2b-recovery-gate.md). O status atual permanece NO-GO para PR enquanto a recuperação durável BC06 e replay integral da aplicação não forem homologados.

A organização proposta de bibliotecas reutilizáveis e a política de promoção estão em [Library Architecture](docs/architecture/libraries.md); nenhuma library compartilhada ou workspace de linguagem foi criado nesta etapa.

### Banco local do Scout (desenvolvimento)

O perfil local usa PostgreSQL 18.4 no Docker Compose, limitado a `127.0.0.1` e volume nomeado persistente. Copie `.env.example` para `.env` (credenciais exclusivas de desenvolvimento), depois execute `make db-up`, `make db-status`, `make db-migrate`, `make test-integration` e `make db-down`. `db-down` preserva o volume. `make check` permanece offline; `make ci` usa PostgreSQL configurado pelo CI. O DSN host local usa `127.0.0.1:55432`; dentro da rede Compose, o hostname é `postgres:5432`. Migrations são aplicadas pelo runner versionado, não pelo entrypoint da imagem.
