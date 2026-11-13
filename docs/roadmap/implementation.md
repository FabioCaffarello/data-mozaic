# Roadmap pós-S1

## Estado atual

Scout S1.6 estabeleceu um fluxo offline durável de authoring para BC14: sessão, autorização, evidências de fixture, findings e `PlaybookProposal`, com PostgreSQL, receipts, reconciliação de custódia e relay independente. PR #4 adicionou adapter HTTP experimental; PR #5 adicionou o gerador BC15 de draft CapturePlaybook e replay por fixtures; PR #6 adicionou uma prova Docker opcional de isolamento de egress para um container worker. O incremento PR #7 em `feat/scout-mcp-tool-host` fecha autoria incremental MCP STDIO: source/policy local → sessão → fixtures registradas → custody de referência e manifest → findings → proposal/outbox → transcript versionado → BC15 draft/validate/replay. Os gates locais incluem PostgreSQL/recovery sem skips e a prova Docker do worker. O host não navega por HTTP público nem autentica usuários corporativamente; custody BC06 e integrações BC11/BC15 permanecem de referência. Os relatórios S0–S1 anteriores são snapshots históricos e permanecem preservados em `docs/architecture/assessments/`.

Os ADRs 001–015 permanecem Proposed salvo aprovação registrada por decisão humana. Merge de código ou CI verde não altera o estado de um ADR.

## PR A — governança do repositório

PR #3 estabeleceu Conventional Commits, validação de título de PR, checks de naming e dependências de arquitetura, e atualizou a documentação de estado. A refatoração tática extraiu a projeção de proposta sem alterar contratos. Os checks offline funcionam sem Docker.

## PR B — ferramentas controladas de authoring

PR #4 entregou o adapter HTTP como base experimental com fixtures de transporte herméticas. PR #6 exige proxy Squid e adiciona topologia Docker interna com teste de bloqueio de egress direto e destinos não permitidos. O probe ainda não é o tool host, não contém shell de agente e não conecta adapters produtivos BC06/BC11 ou source aprovada. Não apresentar como fluxo operacional de agente. Não adicionar Playwright sem caso de uso que o justifique.

## PR C — drafts de CapturePlaybook

PR #5 implementou skill local, CLI e artefato separado de CapturePlaybook draft, sem alterar o schema público Scout v1. Valida schema, semântica, capabilities e escopo para HTTP fetch mínimo, descoberta de links, paginação limitada e saída raw. Reproduz o plano contra fixtures capturadas; não o executa em produção arbitrária. O E2E determinístico usa transcript e site fixture multipágina, evidências, proposta, YAML draft, validação, replay e relatório. A skill falha fechado quando não há tool host com isolamento verificável. Smoke test com Codex ao vivo não está disponível.

## PR #7 — MCP STDIO e autoria incremental

O branch local `feat/scout-mcp-tool-host` fecha o fluxo hermético MCP client → source/policy local → sessão incremental → fixture catalog registrada → custody de referência e manifest → finding → proposal/outbox → transcript versionado → BC15 CLI draft/validate/replay. O SDK oficial `mcp>=1.30,<2` está travado em 1.30.0 para manter compatibilidade com o handshake legado. BC15 carrega schemas empacotados e wheel/sdist passam instalação externa isolada. PostgreSQL/recovery passou 19 testes sem skips em cada alvo explícito; o `make ci` agregado passou com os 19 casos PostgreSQL ignorados porque não carrega `.env`. O sandbox Docker de egress do worker passou. Isso não habilita navegação pública, autenticação corporativa ou isolamento do processo Codex/agente. O Draft PR #7 será publicado após commits locais e permanece sujeito à revisão humana. Consulte [avaliação de fechamento](../architecture/assessments/scout-mcp-pr7-closure.md), [readiness histórico](../architecture/assessments/scout-mcp-host-readiness.md) e [runbook](../operations/scout-authoring-tools.md).

## PR #8 — navegação hermética integrada ao host

Integrar o adapter HTTP autorizado ao worker e proxy de PR #6, adicionar teste HTTPS local multipágina com budgets/egress e validar intent/recovery/outbox em PostgreSQL real. Não conectar Codex ao workflow até provar como o processo do agente fica sem shell/rede de bypass ou delimitar explicitamente a ameaça.

## PR #9 — adapters proprietários e adoção controlada

Integrar somente fontes e approvals fornecidas pelos owners BC01/BC11 e custódia/traces por BC06, com revisão dos contratos de receipts e validação de transcript pelo CLI BC15. Não implementar produção até haver política operacional e gates de egress e sandbox do agente.

## Critérios do primeiro MVP útil

1. Uma fonte HTTPS de teste aprovada/possuída e um objetivo produzem findings com evidências e um arquivo candidato de playbook.
2. Navegação, redirects, DNS/egress e budgets passam por política executável aplicada no runtime.
3. Cada finding aponta para referência durável de evidência e explicita sua classificação. Selectors não são inventados.
4. O playbook candidato passa validadores de DSL, capabilities e policy, além de replay por fixture; falhas ficam explícitas.
5. Codex, outro agente ou frontend podem dirigir o workflow sem alterar a lógica de domínio BC14/BC15.

## Depois

Playwright e browser sandbox, parser e capture runtime, backend Scrapy, source drift, UI, cloud orchestration e frontier distribuída são capacidades separadas. Cada uma precisa de caso de uso, ownership, contrato e PR próprios.

## Política de bibliotecas

Usar `libs/<language>/<library>` somente para capacidades técnicas coesas com consumer e owner identificados. ScoutSession, repositórios BC14, migrations, receipts e regras CAS continuam privados ao contexto. `mozaic-observability` permanece interna e experimental; estendê-la somente com primitives estáveis e consumer real. Compor clients HTTP, PostgreSQL e instrumentação de terceiros; não criar `common`, `utils`, `mozaic-postgres`, `mozaic-http` ou `mozaic-proxy` sem critérios de extração e consumidores demonstrados.
