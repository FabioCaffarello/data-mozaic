# Ferramentas de authoring do Scout — desenvolvimento

**Estado: implementação experimental; sem navegação operacional homologada.** As ferramentas HTTP ficam separadas do fluxo offline BC14. Elas aceitam apenas GET HTTPS via `HTTPDiscoveryTool`, revalidam aprovação por source e principal antes de cada salto, não seguem redirects automaticamente e armazenam bytes/trace apenas pelas portas injetadas BC06/BC14. A prova Docker opcional de egress isolado está em [sandbox de egress](scout-egress-sandbox.md), mas não há adapter produtivo BC01/BC06/BC11, ferramenta MCP/Codex ou sandbox do processo do agente.

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

URLs são canonicalizadas para HTTPS, IDNA e porta 443; userinfo, IPs privados/literais não públicos, dot segments, escapes ambíguos e parâmetros de query com nomes de credenciais são rejeitados. Prefix matching respeita limite de segmento. O transporte real exige `http://squid:3128`; Squid resolve destinos, restringe CONNECT a hostnames administrativos e nega ranges privados/especiais antes de conectar. Cada redirect passa novamente pela autorização e pelo budget no adapter; bytes, tipo de resposta e duração têm limites. Texto, links e outros bytes recebidos são conteúdo externo não confiável, nunca instruções ou policy.

O probe Docker prova isolamento apenas para um container de teste que não é o tool host. Ele não impede um agente em outro processo de usar shell ou rede direta. Ainda não conecte a ferramenta a um workflow agentivo. Source/policy file são configuração local; EvidenceStore/Trace/ToolContext são ports sem adapters de produção, e nenhum request HTTPS para fonte pública foi executado. O acceptance E2E da aplicação com host, servidor HTTPS local e evidência BC06 segue como gate.
