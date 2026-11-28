# Roadmap pós-S1

> **Estado atualizado em 2026-11-26:** PRs #21–#26 integraram aquisição live autorizada, avaliação de maturidade, runtime Scrapy, Trusted Acquisition Application, resolução imutável BC08 e lifecycle Source/Release. M5 estabelece Work Intake durável e aquisição parametrizada; seu estado de revisão e integração é registrado pela [assessment M5](../architecture/assessments/work-intake-2026-11-26.md). Consulte também a [avaliação de maturidade](../architecture/assessments/acquisition-platform-maturity-assessment-2026-11-25.md), [aplicação de aquisição](../../apps/acquisition-application/README.md), [aplicação de lifecycle](../../apps/acquisition-lifecycle/README.md), [ADR 029 Proposed](../architecture/decisions/029-source-release-lifecycle-management.md) e [evidência M4](../architecture/assessments/source-release-lifecycle-2026-11-25.md). Nenhuma fonte externa foi implementada ou explorada.

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


## BC04 durable integrado — PR #19

PR #19 foi integrado por squash em 2026-11-25 (`1db31b3`). BC04 persiste Runs, Attempts, leases, gerações de fencing, receipts, reservas de finalização e eventos PostgreSQL. A reserva vincula uma publicação exata e impede substituir a tentativa enquanto o resultado estiver sem resolução. BC05 verifica grant Ed25519 e grava publication receipt/event no mesmo commit MongoDB da publicação. Reconciliação limitada consulta ou repete a mesma operação de publicação e confirma BC04 somente com receipt correspondente.

O protocolo não é uma transação distribuída: PostgreSQL autoriza, MongoDB publica e PostgreSQL registra o resultado terminal em linearizações separadas. Reservas indeterminadas não expiram automaticamente, preservando segurança com custo de disponibilidade. A integração local de BC04 usa composição no processo, ainda sem endpoint autenticado de serviço ou autorização BC11 produtiva. ADRs 022 e 024 continuam Proposed.

## Roadmap revisado — revisão arquitetural de 2026-11-24

Esta sequência substitui a ordem anterior de MVP que colocava um source real antes de definir o perfil de aquisição live. Os incrementos anteriores e seus resultados continuam como evidência histórica. Aprovação para planejar uma capacidade não é autorização de acesso a uma fonte nem aprovação de ADR.

1. **Revisão arquitetural** — ownership BC05/BC06/BC04/BC09, contratos, escopo de fonte, risco e limites operacionais revisados.
2. **BC05 Output foundation** — CaptureOutput/OutputSet, variantes, identidade, operação idempotente, canonicalização padronizada, staging/seal/publicação local e contratos draft.
3. **Persistência MongoDB BC05** — coleções, validadores, indexes, transação local de publication/outbox, recuperação e backup/restore de desenvolvimento. MongoDB continua substituível atrás de port.
4. **Custody BC06** — streaming de bytes, identidade lógica, recibo de custody, inventário PostgreSQL e adapter S3 compatível; a decisão proposta usa Go para o serviço operacional, sem alegar vantagem de desempenho medida. Output dependente de artefato não publica sem custody confirmada.
5. **Coordenação durável** — concluída em PR #19: BC04 usa PostgreSQL para Runs/Attempts, leases/fencing, retry/deadline/cancelamento, reserva de finalização e reconciliação BC04↔BC05. A reserva continua Proposed em ADR 024 e não estabelece ACID distribuído ou exactly-once.
6. **Aquisição fixture-only integrada** — integrada pelo PR #20, squash `b2c2e1d` em 2026-11-25. Compila/verifica BotBuild, executa Scrapy real com `--network none`, valida bundle no host confiável, confirma bytes do fixture por BC06, publica outputs BC05 e conclui/reconcilia BC04. O assessment preserva as evidências e limites; isso não é live acquisition.
7. **Fundação de aquisição live autorizada** — integrada em PR #21 (`5f0f8ca`). O incremento implementou perfis live separados, gateway Go com operação autorizada e quotas, evidência confiável, JSON e HTML, paginação next-URL limitada, encerramento natural ou janela planejada, redirects controlados e recuperação. A aceitação foi feita contra uma fonte TLS local controlada, não contra fonte externa. Ver [ADR 025](../architecture/decisions/025-authorized-live-acquisition-profile.md) e [assessment](../architecture/assessments/authorized-live-acquisition-2026-11-25.md).
8. **Vertical Focus** — após etapas 1–7: JSON, registros, observação histórica, persistência e publicação; evidências sintéticas e fixtures derivadas de exploração autorizada permanecem separadas de execução live.
9. **Vertical CVM DFP/ITR** — downloads permitidos, custody MinIO, CSV/ZIP parsing, backfill e dependências de pipeline; só após validação de políticas e formatos atuais.
10. **Vertical infraestrutura composite** — discovery HTML, artefatos PDF, outputs composite/change observation, sob autorização e segurança correspondentes.

### Quatro classes de evidência

- **Fixture sintética:** construída para teste; não representa resposta nem comportamento de fonte real.
- **Fixture de fonte real:** resposta coletada previamente em escopo de exploração aprovado, datada, com digest e proveniência; autoriza replay offline desse artefato, não nova navegação.
- **Execução live autorizada:** worker de captura ativo, em perfil live e escopo atual aprovado, com egress e quotas aplicados no runtime. Não é inferida de Scout HTTP exploration nem de fixture replay.
- **Vertical end-to-end:** consumer case que atravessa definição/release, coordenação, aquisição, custody, OutputSet, parsing/quality e publicação de dados conforme seu escopo. Não é sinônimo de um teste isolado de endpoint.

### Estado BC05 integrado

BC05 fornece variantes Record/Artifact/Composite, contratos e fixtures draft, OutputSet limitado, staging/seal/publicação, receipts e ports de custody. O adapter MongoDB e o perfil local replica set foram integrados em PR #17 (`138e90b`). O armazenamento é incremental e mantém consultas published-only. Testes locais não estabelecem HA nem autorização de tentativa corrente entre PostgreSQL e MongoDB.

### Incremento atual — BC06 Artifact Custody

O incremento atual introduz `apps/artifact-custody` em Go como primeiro serviço operacional multilíngue, contratos HTTP draft, inventário PostgreSQL BC06 e object storage S3 compatível local. RustFS é usado apenas no ambiente local/teste; o provedor de produção permanece decisão futura. A implementação demonstra interoperabilidade com o adapter HTTP Python de BC05 em fixture local sintética. ADR 023 permanece Proposed. O serviço não implementa autorização BC11 produtiva, HA, delivery de eventos nem protocolo de finalização/fencing BC04↔BC05.

MongoDB mantém-se substituível atrás dos ports BC05. BC11 produtivo, fleet BC16 e HA continuam separados. A avaliação de maturidade de aquisição substitui as etapas antigas de Focus/CVM como o gate atual antes de fontes reais.

## Snapshot histórico pré-merge — BC04 durable coordination

O branch `feat/execution-coordination-durable`, derivado do `main` que integrou PR #18, implementa Run/attempts, leases com tempo autoritativo PostgreSQL, fencing generation monotônica, command receipts, reservation de finalização e outbox BC04. A reservation bloqueia substituição do attempt até a conclusão; cancelamento antes dela impede autorização, enquanto cancelamento posterior é registrado e não revoga o grant já emitido. O grant draft Ed25519 v1 limita uma publicação a tenant/Run/attempt/generation/OutputSet/manifest/operação; BC05 verifica com chave pública e persiste receipt e evento no mesmo commit MongoDB da publicação. Um comando limitado reconcilia reservations pendentes por receipt/replay do mesmo grant.

O gate local executou integração PostgreSQL e um fluxo sintético PostgreSQL/MongoDB/RustFS com injeção de ACK perdido após o commit MongoDB, clientes novos e reconciliação concorrente. Isso não é serviço de produção: o branch não está integrado, fixture-only permanece obrigatório, BC11 e endpoint autenticado de serviço estão ausentes, reservations pendentes não expiram/revogam automaticamente, e não há HA ou exactly-once. ADRs 022 e 024 seguem Proposed. A avaliação e os limites devem acompanhar a revisão do branch antes de qualquer integração.

> Nota histórica: o parágrafo acima conserva o snapshot pré-integração. PR #19 agora está integrado em `main`; veja “BC04 durable integrado — PR #19” e o assessment de consolidação para a evidência final.

## Aquisição raw — incrementos de maturidade

PR #24 integrou o Trusted Acquisition Application; veja [M2 implementation assessment](../architecture/assessments/trusted-acquisition-application-2026-11-25.md). M3 está em implementação local na branch `feat/immutable-contract-resolution`: BC08 instala e resolve revisões imutáveis de payload; BotRelease v2 vincula o ref; BC04 persiste a binding. O incremento permanece não publicado até revisão humana; veja [M3 assessment](../architecture/assessments/immutable-contract-resolution-2026-11-25.md) e [ADR 028 Proposed](../architecture/decisions/028-immutable-payload-contract-resolution.md).

M3 não concluiu sozinho a aprovação operacional de fontes/releases. PR #25 integrou a resolução imutável de contratos; PR #26 integrou o lifecycle de Source e Release. ADRs 028 e 029 permanecem Proposed até aprovação humana. Nenhuma fonte externa fez parte desses incrementos.

## M5 — Work Intake e aquisição parametrizada

M5 inclui composição reutilizável de BC13 com os adapters existentes de BC04 e M4, migração PostgreSQL owner-scoped, submissões idempotentes, claims recuperáveis e binding v3 de um parâmetro query escalar. A aceitação controlada executa três Inputs distintos com o mesmo BotBuild e BotRelease por worker, gateway, BC06 e BC05, além de exercitar reconciliação de ACK perdido e falha pós-admission sem publicação. ADR 030 permanece Proposed. A matriz W1–W18, limites e evidência executada estão no [assessment M5](../architecture/assessments/work-intake-2026-11-26.md).

## M6 — aquisição de artefato raw-only

O M6 raw-only v4 foi integrado pelo PR #28 (head revisado `3451359c4a31121ee1e57df29dd484d31ff16312`; main `1eb17ce9dfcab87ba65ee0cd3c96b8fcbf21c1e3`). O perfil executa um GET sem parâmetros, redirects ou continuação, publica `ArtifactOutput` com evidence gateway, custody BC06 e publicação BC05 finalizada por BC04. A aceitação cobre PDF, CSV, ZIP, binário, JSON raw, reacquisição intencional, negativas v4 e reconciliação de ACK perdido. ADR 031 permanece Proposed; veja [evidência M6](../architecture/assessments/raw-artifact-acquisition-2026-11-26.md).

## M7 — bounded transfer e resource maturity

M7 implementa gateway disk spool com hashing incremental, referência de evidence sem body no worker raw-only, transferência privada do importer para BC06 com buffer fixo, reserva single-instance de spool e snapshot autenticado de recursos. Playbook/BotBuild/BotRelease v4 continuam limitados a 2.000.000 bytes. Workloads controlados de 64 KiB, 1.000.000 e 2.000.000 bytes completaram a aquisição integral com duas repetições; dois workloads independentes de 1.000.000 bytes também se sobrepuseram e publicaram corretamente. Os experimentos de 8/32/64 MiB foram explicitamente diferidos porque excedem a autorização imutável v4. Medidas e limitações estão no [assessment M7](../architecture/assessments/acquisition-resource-maturity-2026-11-27.md), e ADR 032 segue Proposed. Não houve execução externa nem aumento do limite v4.

M7 foi integrado pelo PR #29 (head revisado `f50ae1bdb4cebdfe49affc75e9084a24734be217`; main `f112ef6f947291d09f13729c07a8dde5b175be2f`). A avaliação de 8/32/64 MiB permanece deferida por escopo; retenção indefinida de evidence em spool finito exige lifecycle seguro de liberação como requisito operacional M9.

## M8 — Agent-to-Raw Functional Conformance

M8 implementa PlaybookProposal v2 com revisões imutáveis e digest canônico, validação baseada em evidências BC14 com semântica delegada ao compilador BC15, promoção explícita via reviewer e registro imutável M4. A aceitação determinística percorre três fontes HTTPS controladas — PDF raw, JSON estruturado e JSON parametrizado — desde Scout discovery até Work Intake, BC04, Gateway, import confiável e publicação BC05/BC06; os resultados publicados são lidos independentemente. Os Inputs parametrizados reutilizam o mesmo build, runtime e release. A aceitação roda no CI sem serviço de LLM ou sessão Codex.

**A6 — autoria real por agente protegido — foi diferida explicitamente pelo owner** para um gate futuro Protected Agent Operational Acceptance. Não foi demonstrada, não é declarada aprovada e continua obrigatória antes de usar essa modalidade com fontes externas reais. Source approval, preflight NO-GO, autenticação do broker, identidade reviewer, release approval e autoridade assinada permanecem intactos. Nenhuma fonte externa foi acessada.

Os templates de bundle Linux, systemd, identidade/reviewer, SSH, LaunchDaemon e configuração MDM são materiais operacionais opcionais e não requisitos para desenvolvimento ou conformance M8; nenhum ambiente protegido está provisionado. Linux continua referência aceitável para deployment protegido, mas suporte a múltiplos sistemas operacionais não é meta corrente. Ver [assessment M8](../architecture/assessments/agent-to-raw-conformance-2026-11-27.md), [runbook opcional de deployment](../operations/scout-trusted-broker-deployment.md) e ADR 033 (Proposed).

## M9 — Acquisition Operational Baseline (em implementação)

Uma branch `feat/acquisition-operational-baseline` está implementando M9.1–M9.5 no checkout principal. A liberação reutilizável verifica fatos públicos BC04/BC05/BC06; a CLI de operador cobre inspeção e release explícito de raw-only v4. O Gateway move metadados liberados para arquivo histórico bounded, separando bytes e entradas ativas do limite de armazenamento histórico; readers ativos bloqueiam unlink e startup reconcilia transições parciais. A aceitação controlada repete aquisições sobre um Gateway isolado de três entradas e também avalia release structured, HTML e paginação. Work Intake ainda não tem uma CLI de configuração completa para executar o fluxo protegido sem o harness. ADR 034 permanece Proposed. A implementação e a aceitação integrada seguem em progresso; não declare M9 pronto antes de completar a matriz O1–O22 e as validações finais.

Comando finito integrado: `make acquisition-operational-acceptance`, que executa as aceitações raw controladas, structured/HTML/paginação, ciclos de reutilização e a regressão determinística Agent-to-Raw. Não contata fontes reais, não requer uma sessão LLM e não altera o gate A6 deferido. Veja o [assessment M9](../architecture/assessments/acquisition-operational-baseline-2026-11-27.md) para evidências e limitações após validação.

## Future Protected Agent Operational Acceptance (planejado; não iniciado)

Depois de haver necessidade operacional, usar Linux como referência protegida e exigir ambiente provisionado, principal confiável, source approval legítima, ferramentas MCP efetivas, preflight bem-sucedido, sessão real de autoria agente, proposta baseada em evidências e handoff a reviewer independente. Essa aceitação permanece pré-requisito para usar autoria protegida contra fonte externa real. Não implica suporte multi-OS.

## Future production deployment maturity (planejado; não iniciado)

Avaliar somente quando requisitos de operação justificarem controles de disponibilidade, observabilidade, capacity, lifecycle e deployment produtivo. Não iniciar junto com M8 ou assumir necessidade de infraestrutura remota agora.
