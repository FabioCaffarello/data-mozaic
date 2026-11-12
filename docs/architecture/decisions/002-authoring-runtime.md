# ADR 002 — Playbook, IR e ciclo de release

- Status: Proposed
- Contexto: centenas de estratégias potenciais e múltiplos backends; IA fornece propostas não confiáveis.
- Problema: preservar fonte auditável sem acoplar domínio aos frameworks ou gerar projetos arbitrários.
- Drivers de decisão: segurança, reprodutibilidade, extensibilidade e debugging.
- Alternativas: gerar código por bot; interpretar DSL diretamente; compilar para IR tipada e interpretar, com geração opcional.
- Trade-offs: geração pode otimizar backend mas aumenta manutenção e superfície executável; interpretação centraliza correções mas amplia impacto de regressão e requer compatibilidade rigorosa. YAML ajuda autoria mas exige parsing seguro; JSON é mais restrito.
- Decisão proposta: playbook revision é fonte declarativa, IR limitada sem código arbitrário, interpretação como padrão e geração excepcional após medição; SDK público sem dependência de worker; seleção por capabilities. Build e release são agregados separados e release imutável fixa digests e versões.
- Consequências: schema e semântica devem ser versionados; suite de conformidade por adapter; revisão e aprovação independentes de geração.
- Riscos: DSL excessiva, divergência de runtime e dependências não reproduzíveis.
- Critérios de validação: representar exemplos de HTTP e browser sem escape arbitrário; rebuild consistente, falha clara em capabilities incompatíveis e testes de promoção/canary/rollback.
