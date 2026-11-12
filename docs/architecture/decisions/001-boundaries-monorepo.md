# ADR 001 — Planos, contextos e monorepo

- Status: Proposed
- Contexto: plataforma reutilizável com aquisição inicial e linguagens distintas, sem requisitos de carga homologados.
- Problema: separar ownership sem converter cada contexto em um serviço, mantendo consumidores atualizáveis.
- Drivers de decisão: evolução incremental, limites auditáveis, custo operacional e composição.
- Alternativas: monólito sem fronteiras; microserviço por contexto; módulos com deploys extraídos por necessidade.
- Trade-offs: módulos reduzem operação mas compartilham falhas; serviços isolam escala e release com custo de rede e consistência. Template puro diverge; fork facilita merge mas aumenta conflitos; pacotes e perfis exigem publicação e compatibilidade.
- Decisão proposta: quatro planos lógicos, contextos com ownership explícito e processo modular inicial; diretórios por tipo de composição somente quando houver componente. Go para control, Python para aquisição/IA, TypeScript para UI e Rust apenas sob evidência são direções, não obrigação. Consumidores compõem core versionado, extensões e perfis.
- Consequências: limites e contratos devem ser testados mesmo em processo único; sem workspaces vazios.
- Riscos: separação inadequada de BC05/BC16, excesso de linguagens, dependência de manutenção de versões.
- Critérios de validação: cenário de aquisição revisado, ausência de dependências indevidas, prova de atualização de consumidor e custo operacional medido.
