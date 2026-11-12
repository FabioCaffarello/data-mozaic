# Roadmap e prontidão

Todos os incrementos dependem de revisão da arquitetura e de critérios de aprovação; não são serviços implementados.

1. Homologar fronteiras e semântica mínima, ameaças, orçamento e contrato de fonte/captura; validar com cenário real autorizado e ownership.
2. Vertical slice mínimo abaixo, com módulos juntos e dependências controladas.
3. Adicionar authoring manual de playbook tipado e build/manifest reprodutível, suite de compatibilidade e aprovação.
4. Introduzir Scout assistido, evidências e revisão, mantendo operação manual; avaliar prompt injection e isolamento.
5. Introduzir frontier durável distribuída e broker somente quando volume/necessidade justificar, testes de falha, budgets compartilhados e operação.
6. Adicionar backend browser, drift/canary e expansão; parsing/quality/lineage por contratos após necessidade demonstrada.

## Proposta do primeiro incremento funcional — para revisão, não executar agora

Objetivo: provar SourceRef → CaptureInput → CaptureRun fixado → HTTP autorizado em fixture local → receipt e raw artifact recuperáveis. Escopo: aplicação modular única, source configurada manualmente, uma release de fixture identificada por digest, armazenamento local com interface de custódia e persistência transacional escolhida após revisão. Sem Scout autônomo, geração de código, browser ou distribuição real. Módulos BC01/13/04/05/06 têm ownership separado em processo; fronteira BC16 pode começar como fila local explícita, sem reivindicar garantias distribuídas.

Contratos mínimos: SourceRef, CaptureInput, referência de BotRelease, CaptureRun, CaptureReceipt, RawArtifactRef e CaptureOutcome, em versão draft revisada; política de autorização e chave idempotente. Dependências: fonte HTTP de teste controlada, banco/armazenamento locais, versão pinada de toolchain e aprovação dos ADRs relevantes. Aceitação: raw bytes exatos e digest, receipt vincula input/run/release, input duplicado não cria efeitos lógicos duplicados, falhas de persistência não reportam sucesso, cancelamento e erros de acesso são explícitos. Testes: contrato e consumidor, integração com fixture, duplicação, falha entre objeto/registro e reconciliação, autorização/SSRF, logs sem segredos. Não reivindicar escalabilidade ou broker nesta fase.

Riscos: acesso externo e privacidade, consistência entre objeto e metadado, semântica de idempotência e custo de migração de contratos. Definition of Done: contratos revisados e compatibilidade testada, limites de contexto demonstrados, threat model e retenção definidos, testes executados, documentação e operação local reproduzíveis, evidências de falhas e revisão arquitetural humana antes da promoção. Nenhuma implementação funcional está autorizada por este documento.
