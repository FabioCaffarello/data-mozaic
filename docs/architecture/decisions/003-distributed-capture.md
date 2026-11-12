# ADR 003 — Distribuição de captura

- Status: Proposed
- Contexto: múltiplos workers compartilham origens com limites externos e entregas repetidas.
- Problema: diferenciar trabalho lógico de URLs e recuperar falhas sem duplicação lógica indevida.
- Drivers de decisão: controle global, simplicidade operacional, durabilidade, recuperação.
- Alternativas: banco transacional como frontier; Redis; filas duráveis; scheduler local; Kafka ou JetStream para eventos.
- Trade-offs: PostgreSQL fornece transações, unicidade e locks com contenção/vacuum; Redis exige desenho cuidadoso de durabilidade; broker não substitui estado. JetStream é candidato operacional mais simples; Kafka requer justificativa de retenção, throughput e ecossistema. Escala horizontal aumenta risco de saturar origem.
- Decisão proposta: BC13 possui inputs, BC04 runs, BC16 tarefas/leases/budgets; PostgreSQL como candidato inicial após testes e JetStream somente quando eventos distribuídos forem necessários. Outbox/inbox e at-least-once; fencing, rate gate compartilhado e limites por origem.
- Consequências: objetos e efeitos externos exigem reconciliação; token vencido não reverte request já enviado.
- Riscos: hotspots, expiração de leases, erro de política distribuída, crescimento de tabelas.
- Critérios de validação: fault injection, concorrência entre dez workers sob budget único, fairness e benchmark com volumes acordados; teste de recuperação/backup.
