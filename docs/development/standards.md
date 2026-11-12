# Padrões de engenharia

## Fluxo de contribuição

Trabalhe em branch própria e integre por pull request revisado; não faça push direto em `main`. Cada PR descreve problema, mudanças, decisões, verificações, riscos e limitações. Proteção de branch e checks obrigatórios dependem de configuração no GitHub; este repositório não os configura automaticamente. A criação de commits, push e PR depende de autorização aplicável.

## Ambiente e comandos

O projeto raiz é um projeto de ferramentas não publicável e membro raiz do workspace uv. `members = []` é intencional: acrescente projetos reais explicitamente, com seus próprios `pyproject.toml`; os membros compartilham resolução e `uv.lock`, portanto suas restrições precisam ser compatíveis. `.python-version` define Python 3.12 para desenvolvimento e CI; o projeto requer Python >=3.12. O CI fixa uv 0.9.8, enquanto desenvolvedores devem usar uma versão compatível com o lock. Instale uv e Make antes de começar.

| Comando | Comportamento |
|---|---|
| `make sync` | sincroniza dependências de desenvolvimento com `uv.lock`, sem atualizá-lo |
| `make check` | verifica lock, lint e formato Ruff, links/fences e testes de tooling e contratos draft |
| `make hooks-install` | sincroniza e instala o hook Git de pre-commit localmente |
| `make pre-commit` | sincroniza e executa hooks sobre arquivos rastreados |
| `make ci` | sincroniza e executa `make check`, como no GitHub Actions |

Depois de alterar dependências ou membros, execute `uv lock` e inclua `pyproject.toml` e `uv.lock` no PR. O hook local chama `make check`, sem duplicar comandos; instalá-lo altera somente a configuração local de hooks Git. O CI executa `make ci` em PRs e em pushes para `main`, com token somente de leitura. O job não executa serviços, não valida a sintaxe de Mermaid nem confere links externos. `make pre-commit` considera arquivos rastreados; antes do primeiro commit o hook `always_run` ainda executa os checks do repositório.

CI futura deve acrescentar checagens de tipos, testes de domínio, compatibilidade de contratos e dependências arquiteturais, testes de integração e end-to-end, auditoria de dependências, segurança, build e proveniência de release quando existirem componentes para tal. Filtrar por componentes alterados e dependentes somente quando houver grafo confiável. Testes de arquitetura incluem import boundaries, consumidores, contratos e state ownership; integração cobre broker/DB/storage reais em ambiente controlado; E2E usa fonte de teste autorizada. Fault injection cobre duplicação, expiração, crash e efeitos parciais. Sem metas de coverage arbitrárias.

Mudanças arquiteturais significativas exigem ADR com alternativas, trade-offs e critérios de validação. ADR Proposed não significa aprovação. Documentação deve separar estado real, proposta e hipóteses; diagramas descrevem relações nomeadas. Revisão humana é exigida para políticas de acesso, código gerado por IA e promoção de bot; privilégios e segredos mínimos. Alterações devem preservar trabalho não commitado.

As convenções para novos caminhos e identificadores estão em [naming-conventions.md](naming-conventions.md). Nomes públicos de contratos versionados são preservados.
