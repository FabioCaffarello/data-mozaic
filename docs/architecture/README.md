# Arquitetura Data Mozaic

Status das decisões: **Proposed**, salvo indicação explícita; nenhuma ADR foi homologada. Os assessments registram evidências de incrementos implementados. Scout S1.6 é um fluxo offline com fixtures, doubles e relay durável. Um adapter HTTP experimental foi adicionado, sem tool host, source aprovada ou adapters BC06/BC11 configurados; não há navegação operacional, serviço de captura ou runtime de agente.

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

O [roadmap pós-S1](../roadmap/implementation.md) descreve o estado atual e os próximos limites de PR. Os assessments por incremento permanecem registros históricos de seu snapshot e comandos observados.

O [runbook das ferramentas HTTP](../operations/scout-authoring-tools.md) documenta a policy local e os limites do adapter experimental.
