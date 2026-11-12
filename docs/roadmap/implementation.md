# Roadmap pós-S1

## Estado atual

Scout S1.6 estabeleceu um fluxo offline durável de authoring para BC14: sessão, autorização, evidências de fixture, findings e `PlaybookProposal`, com PostgreSQL, receipts, reconciliação de custódia e relay independente. O acceptance report registra os checks locais. BC06 e BC15 continuam representados por doubles. Um adapter HTTP controlado está em desenvolvimento experimental; não há tool host, source aprovada, navegação operacional, CapturePlaybook gerado, serviço de captura ou agente disponível. Os relatórios S0–S1 anteriores são snapshots históricos e permanecem preservados em `docs/architecture/assessments/`.

Os ADRs 001–012 permanecem Proposed salvo aprovação registrada por decisão humana. Merge de código ou CI verde não altera o estado de um ADR.

## PR A — governança do repositório

PR #3 estabeleceu Conventional Commits, validação de título de PR, checks de naming e dependências de arquitetura, e atualizou a documentação de estado. A refatoração tática extraiu a projeção de proposta sem alterar contratos. Os checks offline funcionam sem Docker.

## PR B — ferramentas controladas de authoring

Introduzir ferramentas first-party que medeiam cada request e aplicam origem, caminhos, métodos, autorização, limites, evidência e trace por política executável. URL e origem precisam de normalização segura; registro explícito de fonte e política local autorizada não podem ser delegados ao prompt. Começar com HTTP e fixtures de transporte local herméticas. Restringir também o processo e a rede da ferramenta; instruções textuais ao agente não são um sandbox. Não adicionar Playwright sem caso de uso que o justifique.

O workflow de aplicação BC14 continua separado da demonstração por fixtures. Adapters de ferramenta recebem somente portas com privilégio mínimo. Não alterar os contratos wire v1 nem adicionar navegação real fora desse incremento.

## PR C — drafts de CapturePlaybook

Adicionar `.agents/skills/data-mozaic-scout/SKILL.md` com templates e referências para consumir as ferramentas controladas. O usuário informa URL e objetivo, e aprova explicitamente escopo, orçamento, credenciais e acesso ao conteúdo. O workflow registra evidências, findings classificados como `observed`, `hypothesized` ou `independently verified`, e uma `PlaybookProposal`.

BC15 recebe a proposta e cria um artefato separado de CapturePlaybook draft, sem alterar o schema público Scout v1. Validar schema, semântica, capabilities e policy para HTTP fetch mínimo, descoberta de links, paginação limitada e saída raw. Reproduzir o plano contra fixtures capturadas; não executá-lo em produção arbitrária. O E2E determinístico obrigatório usa um site local multipágina, transcript determinístico, evidências, proposta, YAML draft, validação, replay e relatório. Smoke test com Codex ao vivo é manual e opcional.

## Critérios do primeiro MVP útil

1. Uma fonte HTTPS de teste aprovada/possuída e um objetivo produzem findings com evidências e um arquivo candidato de playbook.
2. Navegação, redirects, DNS/egress e budgets passam por política executável aplicada no runtime.
3. Cada finding aponta para referência durável de evidência e explicita sua classificação. Selectors não são inventados.
4. O playbook candidato passa validadores de DSL, capabilities e policy, além de replay por fixture; falhas ficam explícitas.
5. Codex, outro agente ou frontend podem dirigir o workflow sem alterar a lógica de domínio BC14/BC15.

## Depois

Playwright e browser sandbox, parser e capture runtime, backend Scrapy, source drift, UI, cloud orchestration e frontier distribuída são capacidades separadas. Cada uma precisa de caso de uso, ownership, contrato e PR próprios.

## Política de bibliotecas

Usar `libs/<language>/<library>` somente para capacidades técnicas coesas com consumer e owner identificados. ScoutSession, repositórios BC14, migrations, receipts e regras CAS continuam privados ao contexto. `mozaic-observability` permanece interna e experimental; estendê-la somente com primitives estáveis e consumer real. Compor clients HTTP, PostgreSQL e instrumentação de terceiros; não criar `common`, `utils`, `mozaic-postgres`, `mozaic-http` ou `mozaic-proxy` sem critérios de extração e consumidores demonstrados.
