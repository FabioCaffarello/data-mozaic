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

## Implementação local — evolução interna do transcript

O transcript BC14→BC15 evoluiu para `scout-transcript-draft/v2`, sem alteração do Scout v1 nem da DSL CapturePlaybook draft/v1. O motivo é que v1 não carrega URL de entrada, URL efetiva após redirect ou sequência/relação de observações; BC15 não pode reconstruí-las por ID ou timestamp. BC14 persiste esses fatos no agregado e exporta-os por sequência explícita. BC15 mantém leitura estrutural de v1 para diagnóstico, mas recusa gerar um playbook a partir de v1 por falta de identidade inicial inequívoca. Consumidores devem negociar explicitamente a versão do transcript. O formato v2 continua draft/illustrative.

Replay Execution e Acquisition Acceptance são gates separados. Replay materializa `output.raw` como bytes e verifica operações, budgets e integridade. Acceptance usa expectativas mantidas fora do transcript e do draft para verificar início, cobertura exigida, relação de navegação, identidade/digest dos outputs e limites. Isso não extrai nem afirma records estruturados. Não se introduziu DSL v2.

Compatibilidade: dados persistidos antigos de sessão ainda hidratam com metadados opcionais ausentes; novas sessões HTTP persistem sequência. Uma sessão HTTP antiga sem sequência pode exportar metadata, mas não pode produzir draft confiável. Novos consumidores devem rejeitar v1 para autoria ou manter um adapter explicitamente versionado que obtenha uma origem inequívoca fora da ordenação das páginas.
