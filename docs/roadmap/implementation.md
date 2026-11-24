# Roadmap pós-S1

## Estado atual

Scout S1.6 estabeleceu um fluxo offline durável de authoring para BC14: sessão, autorização, evidências de fixture, findings e `PlaybookProposal`, com PostgreSQL, receipts, reconciliação de custódia e relay independente. PR #4 adicionou adapter HTTP experimental; PR #5 adicionou o gerador BC15 de draft CapturePlaybook e replay por fixtures; PR #6 adicionou uma prova Docker opcional de isolamento de egress para um container worker; PR #7 foi integrado em `main` no merge `bbc5df0` e entregou autoria incremental MCP STDIO com fixtures. PR #8 foi integrado no merge `5c3e2b1` e conectou `scout_fetch` ao host local, compondo o adapter HTTP existente com policy por source/revision, budget PostgreSQL e broker Squid. A custódia BC06 e política BC11 continuam doubles locais; não há sandbox do processo Codex nem validação de produção. Consulte a [matriz de ameaças HTTP](../architecture/assessments/scout-http-threat-matrix.md) e o [runbook](../operations/scout-authoring-tools.md).

Os ADRs 001–015 permanecem Proposed salvo aprovação registrada por decisão humana. Merge de código ou CI verde não altera o estado de um ADR.

## PR A — governança do repositório

PR #3 estabeleceu Conventional Commits, validação de título de PR, checks de naming e dependências de arquitetura, e atualizou a documentação de estado. A refatoração tática extraiu a projeção de proposta sem alterar contratos. Os checks offline funcionam sem Docker.

## PR B — ferramentas controladas de authoring

PR #4 entregou o adapter HTTP como base experimental com fixtures de transporte herméticas. PR #6 exige proxy Squid e adiciona topologia Docker interna com teste de bloqueio de egress direto e destinos não permitidos. O probe ainda não é o tool host, não contém shell de agente e não conecta adapters produtivos BC06/BC11 ou source aprovada. Não apresentar como fluxo operacional de agente. Não adicionar Playwright sem caso de uso que o justifique.

## PR C — drafts de CapturePlaybook

PR #5 implementou skill local, CLI e artefato separado de CapturePlaybook draft, sem alterar o schema público Scout v1. Valida schema, semântica, capabilities e escopo para HTTP fetch mínimo, descoberta de links, paginação limitada e saída raw. Reproduz o plano contra fixtures capturadas; não o executa em produção arbitrária. O E2E determinístico usa transcript e site fixture multipágina, evidências, proposta, YAML draft, validação, replay e relatório. A skill falha fechado quando não há tool host com isolamento verificável. Smoke test com Codex ao vivo não está disponível.

## PR #7 — MCP STDIO e autoria incremental (integrado)

O PR #7 está integrado a `main` como `bbc5df0` em 2026-11-18 e seu check remoto passou. O PR adicionou o fluxo hermético MCP client → source/policy local → sessão incremental → fixture catalog registrada → custody de referência e manifest → finding → proposal/outbox → transcript versionado → BC15 CLI draft/validate/replay. O SDK oficial `mcp>=1.30,<2` está travado em 1.30.0 para manter compatibilidade com o handshake legado. Isso não habilitou navegação pública, autenticação corporativa ou isolamento do processo Codex/agente. A API do GitHub não retornou decisões formais de review no momento em que o PR seguinte começou; o merge e o check verde foram confirmados. Consulte [avaliação de fechamento](../architecture/assessments/scout-mcp-pr7-closure.md), [readiness histórico](../architecture/assessments/scout-mcp-host-readiness.md) e [runbook](../operations/scout-authoring-tools.md).

## PR #8 integrado — Scout Authorized HTTP Exploration

Conectar somente `scout_fetch` ao host, com URL dentro do source/revision da sessão, redirects por hop, budget cumulativo no ledger PostgreSQL, transporte via proxy e evidência final bruta em custody. Links são retornados para decisão incremental, nunca seguidos automaticamente. Testar negatives de path, redirects, DNS/IP no proxy, timeouts, tamanhos, cancelamento, concorrência, prompt injection, offline e restart/replay. Fixture HTTPS e source pública aprovada exigem broker/configuração de runtime explícitos. Gaps: corpos de redirects não são anexados ao transcript; a shell/rede do agente não está isolada; Playwright e adapters de produção permanecem futuros.

## Incremento atual — Codex local e autoria de Playbook

O PR #9 introduz skill Codex, MCP e BC15 local. A remediação preserva Scout v1 e CapturePlaybook draft v1, mas evolui explicitamente o DTO interno BC14→BC15 para transcript draft v2, pois a sequência inicial não existia em v1. Adiciona replay de `output.raw`, acceptance contra expectativas independentes, wrapper BC15 compatível com workspace-write e publicação privada de fixtures descriptor-relative. Duas sessões reais locais do Codex passaram acceptance em fixtures HTTPS independentes, incluindo uma variação sem paginação; os resultados e limites estão no [assessment de remediação e aceitação](../architecture/assessments/scout-codex-functional-remediation.md). A execução real permanece um gate manual por ambiente; testes determinísticos não a substituem. Isso não promove ACK, não implementa isolamento global do processo, Playwright, compilador ou runtime.

## PR #10 — CapturePlaybook v2 e compiler vertical

PR #10 preserva a DSL v1 e adiciona draft/v2, validação semântica com diagnostics estáveis, `collector-ir/v1`, profile versionado Python HTTP Reference, código Python específico da IR e BotBuild local verificável. A runtime resolve somente fixtures com digest e não abre rede. HTML, catálogo limitado, JSON e autoria local Codex têm exemplos/oracles independentes; o caso browser valida semanticamente e falha no capability checker. Ver evidências e limitações em [exemplos executáveis](../../examples/playbooks/README.md), [runbook BC15](../operations/capture-playbook-drafts.md) e [ADR 018](../architecture/decisions/018-collector-playbook-ir-and-reference-backend.md). Isso não fecha os gates do deployment Scout, não implementa Scrapy/Playwright, não cria BotRelease e não autoriza captura produtiva.

## PR #12 integrado — backend Scrapy fixture-only

O PR #12 adicionou o segundo backend BC15, mantendo Playbook/IR e a runtime semântica no owner BC15. A saída Scrapy ainda é um BotBuild standalone; o PR não entregou um projeto worker multi-bot, catálogo instalado nem lifecycle de execução do Collector.

## Próximo incremento proposto — Scrapy Collector multi-bot

O [ADR 020](../architecture/decisions/020-scrapy-collector-multi-bot-project.md) propõe um projeto Scrapy instalável, assembly imutável de múltiplos BotBuilds v2, catálogo local fechado e launcher com um processo fresco por execução. A implementação deve preservar os oracles independentes, exigir compatibilidade explícita entre Python/Scrapy/runtime/dependency lock/target de plataforma e executar a imagem Linux/amd64 com rede negada. Este worker não compila Playbooks nem substitui BC04/BC11/BC16/BC06; o ADR permanece Proposed até aprovação humana.

## Próximo limite — adapters proprietários e adoção controlada

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


## Incremento atual — BC04 Execution Coordination v0 (fixture-only)

O pacote apps/execution-coordination implementa CaptureRun/Attempt, state machine, idempotência scoped e repository in-memory; a aplicação depende de uma porta de execução, não do Scrapy/Docker. O adapter técnico BC05, composto no mesmo deployable, invoca a imagem Scrapy por digest OCI associado a local image ID, inspeciona configuração efetiva antes de start e executa com rede none, UID não-root, rootfs read-only, mounts mínimos e limites de recursos. ExecutionRequest/Report draft v0 cruzam a fronteira em JSON fechado. Output verificado fica em staging até BC04 aceitar sucesso.

O estado é apenas local à vida do processo coordenador; não há recuperação após restart, retries, PostgreSQL, BC11 authorization, BC06 custody, BC16 leases/frontier, API de coordenação ou egress produtivo. O Docker daemon local é privilegiado e continua na trust boundary do adapter. O alvo make execution-coordination-protected-e2e cobre Linux container real e deve rodar no Integration Security Gate; não trate execução direta do worker no host como sandbox.

ADR 021 registra a decisão como Proposed. Nenhum ADR foi promovido por implementação ou CI.
