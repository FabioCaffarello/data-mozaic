# Roadmap e prontidão

Os incrementos dependem de revisão da arquitetura e de critérios de aprovação. S0 documenta BC14 e schemas draft; S1 fornece um fluxo offline com fakes, sem navegação ou serviço implantado. Veja [modelo S0](../architecture/scout.md) e [contratos](../../contracts/scout/v1/README.md).

## S1 — fluxo offline implementado

O fluxo de sessão usa domínio puro e adapters fake em memória, fonte e evidências de fixture local, sem rede ou agente. Exercita autorização, idempotência, estados, evidência e entrega simulada ao BC15; veja [BC14 S1](../architecture/scout.md). Decidir localização da aplicação, modelo de revisão de evidência e política de retenção antes de conectar serviços reais. Isso não substitui o vertical slice de captura proposto abaixo.

## S1.1 — hardening local implementado; sem homologação produtiva

O incremento acrescenta semântica de replay autorizada, fingerprint por comando, CAS sincronizado no repositório fake e retries de identidade estável para custódia/handoff simulados. A auditoria e os limites estão em [S1.1](../architecture/assessments/scout-s1-1.md); a arquitetura futura de recuperação está no [ADR 007 Proposed](../architecture/decisions/007-scout-durable-recovery.md). Crash recovery, armazenamento durável, coordenação multiprocesso e egress real continuam pendentes. O próximo gate é revisão humana de fronteiras/segurança e aprovação dos ADRs antes de escolher persistência ou conectar serviços reais; não implica início automático de S1.2/S2.

## S1.2 — adapter durável experimental; gate pendente

Foi adicionado um adapter PostgreSQL BC14 com SQL/migration numerada, JSON estruturado versionado, command receipts, CAS `UPDATE ... WHERE version`, e outbox para handoff. A especificação está no [ADR 008 Proposed](../architecture/decisions/008-scout-command-receipts.md). A integração PostgreSQL em CI está configurada, mas a homologação depende de executar os testes com PostgreSQL real, validar crash/restart e completar a reconciliação de custódia incerta. Até esse gate, S1 permanece não homologado e nenhum PR deve ser aberto como pronto.

S1.2b acrescenta perfil Docker Compose, migration/checksum, crash da transação, ACK BC15 de teste e intenção/reconciliação de custódia BC06 com test double. Ver [avaliação S1.2b](../architecture/assessments/scout-s1-2b-recovery-gate.md) e [ADR 009 Proposed](../architecture/decisions/009-scout-local-postgres-profile.md). O perfil é local e development-only; replay completo da aplicação após restart e outros gates registrados na avaliação continuam pendentes.

## S1.3–S1.5 — recuperação e fundação de libraries; gate S1 aberto

G1 agora inclui um teste PostgreSQL de aplicação em processos novos, com crash após conclusão persistida, replay, reautorização e contagens duráveis que mostram uma única custódia e handoff lógico. Isso não fecha G1 para todos os cenários de falha/autorização. G2 ainda não tem lookup/reconciliador BC06 por identidade nem política de órfãos executável. G3 ainda não tem relay com lease, reclaim, fencing ou backoff; o caminho atual entrega diretamente pela aplicação. O gate S1 permanece aberto.

S1.5 adiciona `mozaic-observability` em `libs/python/observability`, consumida pelo app Scout e instalada pelo uv workspace; veja [Library Architecture](../architecture/libraries.md) e [ADR 010 Proposed](../architecture/decisions/010-shared-library-architecture.md). A library não contém domínio BC14 e não substitui os gates funcionais. Não existe `mozaic-postgres` porque há apenas um adapter específico BC14 e nenhum comportamento técnico reutilizável comprovado.

## S1.6 — recovery e relay independentes; GO técnico local

G1 cobre crash pré-commit de `ScoutApplication`, rollback, retry em subprocesso novo, replay após ACK em outro processo e reautorização antes do resultado terminal. G2 consulta por identidade e valida sessão, fonte, size, digest, status normalizado e integridade; divergências ficam em manual review e são verificadas após restart. Inventário, bytes e retenção continuam sob ownership BC06. G3 usa `scout.relay_worker` independente: a aplicação retorna `delivery_pending`, o worker tem operação one-shot/polling e shutdown gracioso, e ACK recebido é um fato distinto de aprovação. Migrations 003–005 adicionam lease/fence/backoff/auditoria e size esperado, sem alterar as migrations anteriores ou contratos v1.

Os gates locais PostgreSQL, recovery, `make ci`, `make check`, contratos, library e fronteiras passaram com zero testes skipped. BC06/BC15 seguem doubles; isso não afirma egress ou exactly-once global. O resultado é **GO técnico local para Draft PR**, sujeito ao CI remoto no PR e revisão humana das ADRs 006–011, todas ainda Proposed. Ver [avaliação S1.6](../architecture/assessments/scout-s1-6-acceptance.md), [runbook](../operations/scout-recovery.md) e [ADR 011](../architecture/decisions/011-scout-outbox-relay-protocol.md).

## S1.7 — revisão humana e CI do Draft PR

Revisar o Draft PR único `feat/scout-s1 -> main`, os diffs staged/unstaged preservados, contracts e ADRs 006–011. Aprovação humana das ADRs permanece explícita; não se marca nenhuma Accepted automaticamente. Verificar o CI remoto e decidir o merge separadamente. Não iniciar S2 neste gate.

## Sequência de plataforma

1. Homologar fronteiras e semântica mínima, ameaças, orçamento e contrato de fonte/captura; validar com cenário real autorizado e ownership.
2. Vertical slice mínimo abaixo, com módulos juntos e dependências controladas.
3. Adicionar authoring manual de playbook tipado e build/manifest reprodutível, suite de compatibilidade e aprovação.
4. Introduzir Scout assistido, evidências e revisão, mantendo operação manual; avaliar prompt injection e isolamento.
5. Introduzir frontier durável distribuída e broker somente quando volume/necessidade justificar, testes de falha, budgets compartilhados e operação.
6. Adicionar backend browser, drift/canary e expansão; parsing/quality/lineage por contratos após necessidade demonstrada.

## Proposta do primeiro incremento funcional — para revisão, não executar agora

Objetivo: provar SourceRef → CaptureInput → CaptureRun fixado → HTTP autorizado em fixture local → receipt e raw artifact recuperáveis. Escopo: aplicação modular única, source configurada manualmente, uma release de fixture identificada por digest, armazenamento local com interface de custódia e persistência transacional escolhida após revisão. Sem Scout autônomo, geração de código, browser ou distribuição real. Módulos BC01/13/04/05/06 têm ownership separado em processo; fronteira BC16 pode começar como fila local explícita, sem reivindicar garantias distribuídas.

Contratos mínimos: SourceRef, CaptureInput, referência de BotRelease, CaptureRun, CaptureReceipt, RawArtifactRef e CaptureOutcome, em versão draft revisada; política de autorização e chave idempotente. Dependências: fonte HTTP de teste controlada, banco/armazenamento locais, versão pinada de toolchain e aprovação dos ADRs relevantes. Aceitação: raw bytes exatos e digest, receipt vincula input/run/release, input duplicado não cria efeitos lógicos duplicados, falhas de persistência não reportam sucesso, cancelamento e erros de acesso são explícitos. Testes: contrato e consumidor, integração com fixture, duplicação, falha entre objeto/registro e reconciliação, autorização/SSRF, logs sem segredos. Não reivindicar escalabilidade ou broker nesta fase.

Riscos: acesso externo e privacidade, consistência entre objeto e metadado, semântica de idempotência e custo de migração de contratos. Definition of Done: contratos revisados e compatibilidade testada, limites de contexto demonstrados, threat model e retenção definidos, testes executados, documentação e operação local reproduzíveis, evidências de falhas e revisão arquitetural humana antes da promoção. A implementação do vertical slice de captura permanece fora do S1.
