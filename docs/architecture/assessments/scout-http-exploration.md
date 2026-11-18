# Scout Authorized HTTP Exploration — resultado do incremento

Data da validação: 2026-11-18. Branch: `feat/scout-authorized-http-exploration`, baseada diretamente em `origin/main` no merge de PR #7 `bbc5df029e2f833437d8ed341f1d036185dc2819`.

## Escopo implementado

- O MCP host adiciona `scout_fetch` com schema fechado contendo somente `session_id`, `expected_version` e URL. Source/revision vem da sessão; URL não cria aprovação.
- A aplicação BC14 expõe uma porta de discovery HTTP. O adapter existente normaliza URL, revalida autorização por hop, usa transporte Squid fixo, desativa redirects automáticos e contabiliza cada hop no ledger PostgreSQL.
- A resposta final é preservada em bytes originais no adapter local de custody. BC14 verifica identity, tamanho e digest antes de acrescentar observation/manifest. Texto extraído limitado e links autorizados são retornados; campos de segredo comuns no texto e título são redigidos. Links nunca são seguidos sem nova chamada.
- Erros de policy, rede, estrutura e custody são distintos no retorno MCP. Conteúdo HTML permanece dado externo não confiável.
- A skill Codex e runbooks descrevem descoberta incremental, fronteiras, smoke e limites. ADR 016 permanece Proposed; ADRs existentes continuam Proposed.

## Verificações executadas

- `make ci` — aprovado. Testes offline e verificações de arquitetura, links, migrações, naming, lint e formatação passaram. O perfil geral reportou skips dos testes PostgreSQL por não carregar `.env` automaticamente; eles foram executados separadamente.
- `make db-migrate` — repetido; migrations atuais/idempotentes.
- `make test-integration` — 21 testes PostgreSQL passaram, sem skips; inclui concorrência do budget e replay HTTP após restart.
- `make scout-sandbox-check` — passou: egress TCP/DNS direto bloqueado no worker probe, proxy negou hostname não registrado e hostname de teste resolvido para loopback.
- Testes herméticos cobrem normalização/percent-encoding, URL fora do scope, redirect privado/path, resposta compressed/oversized, falha de rede, conteúdo com prompt injection, custódia e replay após restart.
- O teste de pacote BC15 wheel/sdist, instalação externa, CLI draft/validate/replay continuou passando dentro de `make ci`.

## Smoke público e evidência limite

O MCP host foi configurado temporariamente para `example.com` e `www.iana.org`, com hostname cadastrado no arquivo de egress e source/revision/path/budget aprovados na policy local. Ambas as tentativas chegaram ao Squid e o log do proxy registrou `TCP_DENIED/403 CONNECT`. O adapter retornou `policy_denied`; nenhum conteúdo público foi lido. Nenhum fetch foi tentado via shell/curl e nenhuma restrição foi contornada.

Assim, a navegação pública autorizada não foi demonstrada neste ambiente. A causa exata da ACL/DNS do proxy não foi identificada e nenhum domínio ou rede privada foi liberado para forçar sucesso. Uma fixture HTTPS controlada positiva também ficou pendente porque o broker atualmente bloqueia endereços privados por design.

## Limites restantes

- Bytes intermediários de resposta a redirect ainda não são anexados a manifests; somente a resposta final é evidência de sessão.
- O container probe não é o processo MCP host. O shell/rede do Codex permanece fora de isolamento; o controle cobre somente o caminho de fetch pela ferramenta.
- Custody BC06, policy BC11 e source resolution BC01 são adapters locais de referência. Source com múltiplas origens não é aceito pelo composition atual.
- Não há Playwright, Scrapy, crawl distribuído, runtime de agente remoto ou BotRelease.
- A aprovação formal de review para PR #7 não foi reportada pela API GitHub (`reviewDecision` vazio), embora o PR esteja integrado em `main` e seu check remoto tenha passado.
