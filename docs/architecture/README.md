# Arquitetura Data Mozaic

Status das decisões: **Proposed**, salvo indicação explícita; nenhuma ADR foi homologada. Os assessments registram evidências de incrementos implementados. Scout S1.6 é um fluxo offline com fixtures, doubles e relay durável. PR #4 adicionou um adapter HTTP experimental; PR #6 prova uma topologia opcional de egress isolado. O host MCP de autoria oferece Streamable HTTP com autenticação e limites; o pacote pode ser produzido fora do checkout. O deployment confiável ainda depende de provisionamento administrativo, allowlist Codex efetiva e verificação de identidade/permissões, conforme ADR 017. Não há navegação pública pelo host, integração produtiva de custody, sandbox do agente, serviço de captura ou runtime de agente.

1. [Baseline](assessments/repository-baseline.md)
2. [Visão, planos e requisitos não funcionais](platform.md)
3. [Modelo de domínio e contextos](domain.md)
4. [Scout, playbooks e compilação](authoring.md)
5. [Execução distribuída e recuperação](execution.md)
6. [Contratos, segurança e governança](contracts-security.md)
7. [Monorepo e distribuição](monorepo.md)
8. [Registro de decisões](decisions/README.md) e [template](decisions/template.md)
9. [Padrões de engenharia](../development/standards.md)
10. [Roadmap e primeiro incremento](../roadmap/implementation.md)
11. [Validação e limitações](assessments/architecture-validation.md)

12. [BC14 Scout — modelo e segurança S0](scout.md)
13. [Auditoria do estado antes do S0](assessments/scout-s0-audit.md)
14. [Auditoria e garantias do Scout S1.1](assessments/scout-s1-1.md)
15. [Estado durável e recovery S1.2](assessments/scout-s1-2-durable-recovery.md)
16. [Convenções de nomenclatura](../development/naming-conventions.md)
17. [Auditoria de Compose e recovery S1.2b](assessments/scout-s1-2b-recovery-gate.md)
18. [Library Architecture](libraries.md)
19. [Scout S1.6 acceptance evidence](assessments/scout-s1-6-acceptance.md)
20. [Scout recovery operations](../operations/scout-recovery.md)
21. [CapturePlaybook draft operations](../operations/capture-playbook-drafts.md)
22. [Scout egress sandbox operations](../operations/scout-egress-sandbox.md)

O [roadmap pós-S1](../roadmap/implementation.md) descreve o estado atual e os próximos limites de PR. Os assessments por incremento permanecem registros históricos de seu snapshot e comandos observados.

23. [Scout MCP tool host readiness](assessments/scout-mcp-host-readiness.md)
24. [Scout MCP PR #7 closure e acceptance gates](assessments/scout-mcp-pr7-closure.md)
25. [Matriz de ameaças HTTP do Scout](assessments/scout-http-threat-matrix.md)
26. [Resultado Scout Authorized HTTP Exploration](assessments/scout-http-exploration.md)

27. [Scout Codex playbook authoring — pre-implementation assessment](assessments/scout-codex-authoring-pre-implementation.md)
28. [Scout Codex local authoring — implementation evidence](assessments/scout-codex-authoring.md)
29. [Scout transcript draft v2 — navigation identity and BC15 compatibility](../../contracts/scout/transcript/draft/v2/README.md)
30. [Scout Codex functional remediation — independent acceptance](assessments/scout-codex-functional-remediation.md)
31. [PR #9 BC15 filesystem trust boundary — security closure](assessments/scout-pr9-filesystem-trust-boundary.md)
32. [ADR 017 — Scout trusted MCP deployment](decisions/017-scout-trusted-mcp-deployment.md)
33. [ADR 018 — Collector Playbook v2, Typed IR, and Python Reference backend](decisions/018-collector-playbook-ir-and-reference-backend.md)
34. [PR #10 — Playbook v2 and Python Bot Compiler implementation evidence](assessments/pr10-playbook-compiler-implementation.md)
35. [ADR 019 — Scrapy fixture backend and runtime identity](decisions/019-scrapy-fixture-backend-and-runtime-identity.md)
36. [PR next — Scrapy fixture backend and cross-engine conformance](assessments/pr-next-scrapy-conformance.md)

O [runbook das ferramentas HTTP](../operations/scout-authoring-tools.md) documenta a policy local e os limites do adapter experimental. O [runbook de CapturePlaybook drafts](../operations/capture-playbook-drafts.md) descreve o gerador BC15 e replay offline.
