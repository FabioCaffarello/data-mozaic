# ADR 004 — Contratos, custódia e segurança

- Status: Proposed
- Contexto: fontes externas não confiáveis, consumidores distintos e evidências potencialmente sensíveis.
- Problema: manter contratos evolutivos e bytes originais sem confundir hash, observação ou autorização.
- Drivers de decisão: auditabilidade, privacidade, portabilidade e compatibilidade.
- Alternativas: DTOs internos compartilhados; schemas públicos com APIs referenciando-os; storage de resultados normalizados apenas; artefatos originais versionados.
- Trade-offs: schemas e testes têm custo de governança; raw aumenta storage e risco de dados sensíveis; deduplicação física complica retenção e autorização. CloudEvents padroniza envelope, não consistência ponta a ponta.
- Decisão proposta: schemas de payload como fonte estrutural, OpenAPI/AsyncAPI por interface, CloudEvents para envelope, major/minor com testes; IDs de artefato distintos do hash; raw e DOM separados. Egress restrito, isolamento, menores privilégios e revisão de IA.
- Consequências: políticas de retenção, segredos e acesso precisam ser definidas antes de dados reais; reconciliação entre storage e banco.
- Riscos: prompt injection, SSRF, dados sensíveis, quebra por enum ou semântica e dedup cross-tenant.
- Critérios de validação: testes de consumidor, DNS rebinding/redirect, revisão de ameaça, integridade, autorização e recuperação de objeto órfão.
