# Contratos, versões e segurança

**Proposta.** Contrato pertence ao produtor com consumidores identificados e revisão conjunta; DTO público é projeção estável, nunca exposição automática do agregado. JSON Schema é fonte estrutural de dados reutilizados; OpenAPI referencia schemas de HTTP e AsyncAPI referencia payloads de eventos, sem duplicar definições manuais. CloudEvents fornece envelope de evento; Protobuf só quando transporte/desempenho/interoperabilidade comprovarem benefício. DSL e IR terão schemas e semânticas próprias; componentes e plugins têm manifests independentes. Geração de DTO/SDK somente quando consumidores e toolchain sustentarem manutenção.

| Nome | Natureza / owner | Semântica mínima proposta |
|---|---|---|
| SourceRef | VO público / BC01 | ID estável e revisão ou snapshot identificável |
| ScoutSession | agregado interno / BC14 | status, objetivo e orçamento; API expõe projeção |
| ScoutEvidence | mensagem/ref pública / BC14+BC06 | ID, hash, classificação e vínculo de evidência |
| SourceProfile | artefato versionado / BC14 | achados, confiança e evidências |
| CapturePlaybook | configuração / BC15 | DSL versionada e escopo de aquisição |
| PlaybookRevision | referência pública / BC15 | revisão imutável e digest da definição |
| CompiledBotManifest | manifesto / BC15 | IR, runtime, compilador, SDK, plugins e capabilities fixados |
| BotReleaseManifest | manifesto público / BC15 | build, artefatos e aprovações, conteúdo imutável |
| CaptureInput | comando público / BC13 | ID, source revision, parâmetros tipados e chave de intenção |
| CaptureRun | agregado BC04; projeção pública | ID, input, release fixada, status, tentativa e deadline |
| CrawlTask | contrato interno BC16/worker | run, operação, fingerprint versionado, lease/token e orçamento |
| CaptureReceipt | fato/projeção BC05 | tentativa, request sanitizado, status, tempo, artifact refs e release |
| RawArtifactRef | VO público BC06 | ID da observação, digest, tamanho, media type e política/acesso indireto |
| CaptureOutcome | evento/projeção BC04 | run, status, causa classificada e referências a receipts |

IDs opacos estáveis, únicos por namespace/tenant; formato exato pendente. Comando inclui idempotency key escopada ao emissor e operação, hash do payload e retenção declarada; mesma chave com conteúdo diferente é conflito. CloudEvents propõe `id`, `source`, `type`, `specversion`, `subject`, `time`, `dataschema`, correlation/causation IDs, versão de agregado e contexto de trace; par source+id identifica evento, e replay preserva identidade. Dados pessoais e segredos ficam fora de envelopes e logs. Trace context externo é não confiável; correlation não é autorização.

Versão major para incompatibilidade, minor para adição compatível após testes de consumidores, patch para correção sem alteração semântica. Compatibilidade depende de regras concretas do formato: adicionar campo obrigatório, alterar significado, enum fechado ou remover campo pode quebrar; tolerância a campos desconhecidos e enums precisa ser declarada por contrato. Eventos imutáveis publicados não são reescritos; migração usa versão paralela, período de suporte e prazo de depreciação acordado. Política de formatos de erro deve distinguir código estável, categoria, retryability, correlação e detalhes seguros; HTTP usa status adequado, eventos de falha são fatos, não exceções serializadas. Semântica de serialização, UTC, tamanho e normalização serão fixadas nos schemas futuros. Não existe contrato executável homologado nesta fundação.

## Segurança de execução

Fonte e arquivos são hostis por padrão; navegação e parsers devem executar com usuário sem privilégios, filesystem efêmero, quotas de CPU/memória/tempo/tamanho, limites de download e descompressão, sandbox de processo/container e nenhuma credencial ampla. BrowserContext do Playwright isola sessão, mas não substitui isolamento de processo, rede ou tenant. Acesso à rede passa por política egress com allowlist, DNS/redirect/IP validados no ponto de conexão, bloqueio de loopback, link-local, redes privadas e metadata por padrão; exceções explícitas e auditadas. Bloquear subrequests não autorizadas. Separar credenciais por source/tenant e escopo; injetar segredos de cofre somente na operação autorizada, rotacionar e redigir logs.

Política de retenção/classificação no ingresso; criptografia em trânsito e repouso, acesso mínimo e auditado, exclusão respeitando referências compartilhadas. Assinar builds/manifests e verificar digest, identidade, procedência, dependências e licença antes de promoção; CI futura gera SBOM/proveniência quando houver artefatos reais. Plugins e código gerado por IA requerem revisão, testes e isolamento; assinatura sozinha não prova segurança. Tenant isolation requer decisão explícita de modelo de ameaça e testes antes de oferecer multitenancy. Rate limits e autorizações prevalecem sobre throughput; sem contornar controles de acesso.

## Referências oficiais e limites

- [JetStream](https://docs.nats.io/concepts/jetstream): persistência, consumers, ack e redelivery; at-least-once não elimina idempotência de efeitos.
- [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html): `SKIP LOCKED` pode servir filas e fornece visão inconsistente; não é ordenação estrita.
- [Scrapy architecture](https://docs.scrapy.org/en/latest/topics/architecture.html) e [Playwright BrowserContext](https://playwright.dev/docs/api/class-browsercontext): responsabilidades de engine e isolamento de sessão distintos.
- [CloudEvents specification](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md), [AsyncAPI](https://www.asyncapi.com/docs/concepts/asyncapi-document/structure), [OpenAPI](https://spec.openapis.org/oas/latest.html) e [JSON Schema](https://json-schema.org/specification): formatos e especificações; não escolhem semântica de domínio.
- [OpenTelemetry context propagation](https://opentelemetry.io/docs/concepts/context-propagation/) e [OpenLineage](https://openlineage.io/docs/): candidatos a telemetria e linhagem, sem implementação agora.
- [Dagster](https://docs.dagster.io/), [Apache Iceberg](https://iceberg.apache.org/docs/latest/): alternativas futuras de orquestração e tabelas; nenhuma é pré-requisito da aquisição.
- [Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/), [Protocol Buffers](https://protobuf.dev/overview/), [Buf](https://buf.build/docs/), [Docker Compose](https://docs.docker.com/compose/) e [Kubernetes](https://kubernetes.io/docs/concepts/overview/): alternativas a avaliar com requisitos reais.

Fatos acima são limitados ao que as fontes descrevem; composição, recomendação e garantias de ponta a ponta são hipóteses de Data Mozaic. A revisão técnica deve verificar as versões específicas quando elas forem selecionadas.
