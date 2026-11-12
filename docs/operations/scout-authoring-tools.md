# Ferramentas de authoring do Scout — desenvolvimento

**Estado: implementação experimental; sem navegação operacional homologada.** As ferramentas HTTP ficam separadas do fluxo offline BC14. Elas aceitam apenas GET HTTPS via `HTTPDiscoveryTool`, revalidam aprovação por source e principal antes de cada salto, não seguem redirects automaticamente e armazenam bytes/trace apenas pelas portas injetadas BC06/BC14. Não há adapter produtivo BC01/BC06/BC11, ferramenta MCP/Codex ou sandbox de processo instalado neste repositório.

## Política local

`config/scout/authoring-policy.example.json` é um template vazio. Para desenvolvimento, copie-o para `.local/authoring-policy.json`; esse caminho é ignorado pelo Git. Cadastre um source/revision com grants agrupados por origem e prefixes, e uma aprovação separada ligada ao principal, source, validade e limites. A alteração dessa política é uma ação administrativa local: prompt, objetivo ou URL não criam registro nem aprovação. Não coloque credenciais no arquivo; esta capacidade não envia cookies nem headers de autorização.

Formato ilustrativo:

```json
{
  "version": 1,
  "sources": [{
    "source_id": "owned-test-source",
    "revision": "2026-11-12",
    "grants": [{"origin": "https://capture.example.org", "path_prefixes": ["/docs"]}],
    "limits": {"max_requests": 10, "max_duration_seconds": 60, "max_response_bytes": 2000000}
  }],
  "approvals": [{
    "principal": "approved-local-principal",
    "source_id": "owned-test-source",
    "revision": "2026-11-12",
    "grants": [{"origin": "https://capture.example.org", "path_prefixes": ["/docs"]}],
    "expires_at": "2030-01-01T00:00:00Z",
    "limits": {"max_requests": 5, "max_duration_seconds": 30, "max_response_bytes": 1000000}
  }]
}
```

Um tool host autenticado fornece `principal`, `session_id` e `call_id` via `ToolContextPort`; a ferramenta não aceita esses valores como argumento. Use `ReloadingAuthorizationStore` para reler grants antes de cada hop. `PostgresNavigationBudgetLedger` consome cada hop atomicamente e persiste o scope inicial; requests repetidos são negados e um scope posterior só pode reduzir o budget ou grants. `EvidenceStorePort` entrega bytes a BC06 e `TracePort` registra decisões sanitizadas. Adapters duráveis de identidade, custódia BC06 e trace ainda precisam ser fornecidos pelos owners antes de habilitar uma sessão real.

## Enforcement e limites

URLs são canonicalizadas para HTTPS, IDNA e porta 443; userinfo, IPs privados/literais não públicos, dot segments, escapes ambíguos e parâmetros de query com nomes de credenciais são rejeitados. Prefix matching respeita limite de segmento. O resolver valida todos os endereços DNS e retorna esses IPs exatos ao connector TLS, sem cache de DNS. Cada redirect passa novamente pela autorização e pelo budget; bytes, tipo de resposta e duração têm limites. Texto, links e outros bytes recebidos são conteúdo externo não confiável, nunca instruções ou policy.

Essas verificações protegem requests emitidos por este adapter. Elas não impedem um agente em outro processo de usar shell ou rede direta. Não conecte esta ferramenta a um workflow agentivo até o host efetivamente isolar o processo e restringir egress; instruções no prompt não implementam essa contenção. O adapter HTTP e o budget ledger estão implementados, mas source/policy file são configuração local, EvidenceStore/Trace/ToolContext são ports sem adapters de produção, e nenhum request live foi executado. O acceptance e2e com servidor HTTPS local ou transporte injetado fica como gate, sem depender de site público.
