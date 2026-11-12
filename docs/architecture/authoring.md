# Scout, playbook e compilação

**Proposta.** O Scout produz conhecimento e propostas, não código confiável automaticamente. Seu objetivo, escopo de domínios, paths, métodos, credenciais permitidas, orçamento, duração, ferramentas e aprovador são fixados antes da sessão. Adapter de agente recebe apenas ferramentas autorizadas e dados minimizados; browser e agente são isolados. Captcha, bloqueio ou autenticação não autorizada interrompem e geram sinalização, nunca contorno.

```mermaid
flowchart LR
 S[Source revision] --> O[Objetivo e escopo]
 O --> X[Scout session]
 X --> E[Evidence manifest e trace]
 E --> P[Source profile e achados]
 P --> D[Playbook proposal]
 D --> V[Revisão humana e validação]
 V --> R[Playbook revision]
 R --> B[Build e testes]
 B --> A[Aprovação separada]
 A --> L[BotRelease imutável]
```

O modelo detalhado, ciclo de sessão e responsabilidades de evidência foram refinados em [BC14 Scout — S0](scout.md). O manifesto BC14 referencia artefatos sob custódia BC06; proposta para BC15 não é release.

## DSL proposta e IR

Autoria YAML é legível, mas aceitar somente subconjunto seguro (sem tags arbitrárias, aliases ilimitados ou chaves duplicadas); normalização para JSON canônico e JSON Schema declarado por versão. JSON direto é alternativa mais simples e menos ambígua. O [CapturePlaybook draft v1](../../contracts/capture-playbook/draft/v1/CapturePlaybook.schema.json) é illustrative e não homologado: permite somente HTTP fetch, descoberta de links, paginação limitada e saída raw. Uma revisão futura pode ter ID e versão da DSL, SourceRef fixo, contrato e parâmetros de input tipados, escopo permitido, DAG limitado de operações, condições de término, capabilities, budgets, erros e contratos raw/receipt. Referências externas incluem versão e digest verificado; segredos apenas por referência autorizada.

Primitivas candidatas: fetch, navigate, select, extract referência, descobrir links, iterar página com limite, emitir artefato e condição simples. Não incluir loops ilimitados, eval, shell ou código arbitrário. Controle de fluxo acíclico com iteração delimitada; expressões puras pequenas, tipos e dependências explícitas. Operações específicas entram por plugin assinado e revisado, com versão e capacidade; sem escape hatch de código embutido. A seleção de sintaxe e semântica requer exemplos reais e revisão de segurança antes de publicar contrato.

```mermaid
flowchart LR
 P[Playbook revision] --> Q[Parser seguro]
 Q --> S[Schema e validação semântica]
 S --> I[Typed IR versionada]
 I --> K[Checagem de capabilities e políticas]
 K --> B[Backend interpretado ou gerado]
 B --> M[Manifest, digest, testes e build]
```

IR é grafo tipado e determinístico de operações com IDs, entradas/saídas e dependências, controle de fluxo e limites finitos, requirements de capability, constraints de recursos, error policy, metadados de execução e contratos de output. Validador detecta ciclos não autorizados, variáveis ausentes, incompatibilidades, violações de escopo e limite antes do build. Sem imports Scrapy/Playwright, código executável ou dependência de engine. Captura do estado do browser pode limitar portabilidade e deve ser declarada, não escondida.

| Critério | Geração de código | Interpretação da IR |
|---|---|---|
| Desempenho | otimização específica possível; medição necessária | overhead e despacho; provavelmente aceitável para I/O, hipótese |
| Flexibilidade / manutenção | código por backend e centenas de bots aumentam superfície | runtime único facilita correções, aumenta impacto de regressão |
| Segurança | código gerado precisa revisão e sandbox | superfície restrita, plugins ainda requerem isolamento |
| Observabilidade / debugging | rastreamento requer source maps e build | IDs de nós da IR permitem rastreamento uniforme |
| Reprodutibilidade / portabilidade | depende de compilador e dependências fixadas | IR e runtime pinados; efeitos externos não são determinísticos |
| Versionamento | artefato e gerador por backend | versão de IR, runtime e compatibilidade explícitas |

Recomendação: interpretação como padrão inicial, gerador especializado apenas após evidência de necessidade; ambos consomem IR e produzem manifest equivalente. Build reproduzível exige entradas e toolchain imutáveis, não promete resposta remota reproduzível.

## Ciclos independentes

Playbook: draft → validated, nova revisão para alteração. CompilationRequest: pending → running → succeeded/failed. BotBuild: built → tested/failed. BotRelease: proposed → approved → released → deprecated; conteúdo imutável desde criação, status de governança registrado separadamente. Deployment: pending → canary → active → rolled back/retired; BotHealth é observação temporal. BotCompatibility relaciona DSL/IR/runtime/SDK/capabilities e versão de backend, não um estado de release. Compilação SDK incompatível falha antes de gerar release.

```mermaid
stateDiagram-v2
 [*] --> Proposed
 Proposed --> Approved: revisão e testes
 Proposed --> Rejected
 Approved --> Released: promoção autorizada
 Released --> Deprecated
 Deprecated --> [*]
```

Releases fixam digest de playbook, IR, artefato, compilador, SDK, runtime, plugins e declaração de capabilities. Runs ativas continuam fixadas na release original mesmo após troca; revogação de segurança pode cancelar explicitamente. Sinais de saúde (erro, seletor, schema, volume, paginação e HTML) criam incidente; Scout investiga e propõe revisão, teste de regressão e comparação de artefatos, canary e aprovação antecedem promoção. Rollback seleciona release anterior permitida sem reescrever histórico.
