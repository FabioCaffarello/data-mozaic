# ADR 013 — CapturePlaybook draft artifact

- Status: Proposed
- Contexto: BC14 emite `PlaybookProposal`; BC15 é dono de revisões de playbook, validação e release. Scout v1 não é o contrato de configuração do runtime.
- Problema: produzir um candidato revisável e reproduzível sem promover uma proposta Scout, gerar código, ampliar autorização ou introduzir um runtime de captura.
- Drivers: ownership BC15; vínculo a proposal/finding/evidence; subset DSL fechado; replay somente por bytes capturados; status de draft separado de aprovação e release; compatibilidade futura sem alterar Scout v1.
- Alternativas: armazenar YAML livre em BC14; ampliar `PlaybookProposal`; criar um serviço BC15 agora; manter candidate separado e validar localmente em pacote proprietário de BC15.
- Decisão proposta: `CapturePlaybookDraft` é um artefato candidato versionado e propriedade de BC15, referenciando `proposal_ref`, `finding_id` e evidências. O draft v1 permite somente HTTP GET, descoberta de links same-origin/path, paginação limitada e saída raw, com budgets aprovados explícitos. Não possui selectors, código embutido, credenciais ou ações arbitrárias. Parser YAML usa safe loader com chaves duplicadas rejeitadas; schema, semântica, capability e escopo são validados antes do replay.
- Replay: carrega somente fixtures capturadas com digest verificado. Nenhum client de rede é importado pelo pacote; URL sem fixture, erro de digest, byte/budget excessivo ou resposta inválida gera falha visível. Um replay aprovado não prova comportamento em produção.
- Consequências: contrato `capture-playbook-draft/v1` é illustrative e não é contrato de runtime homologado. BC15 não se torna serviço e o draft não é `PlaybookRevision` aprovada, build ou `BotRelease`. A integração Codex exige tool host, autorização executável, isolamento e adapters produtivos de evidência/trace antes de exploração real.
- Riscos / gates: transcript/approval receipt local não é autenticação criptográfica; host deve autenticar aprovação e identidade. O DSL não autoriza requests por si só. Sem sandbox e egress enforcement, o workflow não pode ser exposto a agente externo.
- Validação proposta: schema e semântica negativas, referência cruzada de evidências, URL scope, duplicate YAML keys, integridade fixture, falha visível por fixture ausente, golden E2E multipágina sem rede.
