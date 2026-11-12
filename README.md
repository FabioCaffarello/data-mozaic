# Data Mozaic

Fundação arquitetural proposta para uma plataforma modular de aquisição e engenharia de dados. Este repositório contém somente documentação e verificações da fundação; não há serviços de captura implementados.

Comece pelo [índice de arquitetura](docs/architecture/README.md), pela [auditoria inicial](docs/architecture/assessments/repository-baseline.md) e pelo [plano incremental](docs/roadmap/implementation.md). Todas as decisões técnicas estão propostas e dependem de revisão.

O desenvolvimento Python usa uv e um workspace único. Com uv e Make instalados, execute `make sync` e `make hooks-install`; use `make check` para validar localmente e `make ci` para reproduzir a entrada do CI. O hook pre-commit chama `make check`. O fluxo de contribuição usa branches e pull requests; detalhes em [padrões de engenharia](docs/development/standards.md).

O verificador de documentação cobre links Markdown locais e blocos de código delimitados, não a semântica dos diagramas ou a correção da arquitetura.
