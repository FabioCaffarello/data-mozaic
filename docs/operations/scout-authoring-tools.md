# Ferramentas de authoring do Scout — desenvolvimento

**Estado: host MCP STDIO experimental com HTTP autorizado em desenvolvimento local.** O fluxo usa cliente MCP oficial, tools fechadas de preflight, start, `scout_fetch`, exploração por fixture, finding, proposta, status e exportação de transcript. `scout_fetch` recebe a URL como alvo, resolve source/revision da sessão, reautoriza cada hop, usa o budget PostgreSQL e persiste os bytes da resposta final na custódia local antes de selar o manifest BC14. Links retornados são descobertas, não requests automáticos. A custódia em arquivo continua sendo um double de referência BC06, não integração produtiva. A configuração de política e identidade é local e não representa BC01/BC11. O transporte HTTP obriga `http://squid:3128`; a topologia está documentada em [sandbox de egress](scout-egress-sandbox.md). Isso não isola shell/rede do processo Codex, não autentica usuários corporativos e não configura produção.

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

O host local é iniciado com `SCOUT_LOCAL_PRINCIPAL`, `SCOUT_DATABASE_URL`, `SCOUT_HOST_POLICY_FILE` e `SCOUT_EVIDENCE_ROOT` no ambiente do launcher: `PYTHONPATH=apps/scout uv run --locked python -m scout.tool_host`. Inicie o Squid com `.local/scout-egress-domains.txt` configurado; o host local usa `SCOUT_EGRESS_PROXY=http://127.0.0.1:3128`, publicado somente no loopback. Dentro da rede Compose, o endpoint é `http://squid:3128`. O transporte rejeita proxy alternativo. A identidade é uma referência local de desenvolvimento, não autenticação corporativa. O arquivo de policy é administrativo, carregado pelo launcher, e nunca gravável por tools. A composição atual aceita apenas sources com uma origem por causa da forma `ScoutScope`; múltiplas origens falham fechado. MCP usa SDK oficial com faixa `mcp>=1.30,<2`, lock resolvido em `mcp 1.30.0`, para preservar o initialize handshake legado solicitado; o SDK é MIT e a linha 1.x está em manutenção. ADR 015 registra a migração v2 como dívida Proposed, sem alterar protocolo nesta entrega.

Os argumentos das tools não incluem principal, aprovação, call ID, budgets, URLs de configuração, caminhos ou credenciais. A identidade de replay é derivada pelo servidor do principal local, tool e slot/version; o fingerprint canônico rejeita argumentos diferentes para a mesma chave. Comandos mutáveis validam as regras de domínio antes de gravar intent, aplicam CAS e persistem receipt/result; uma rejeição definitiva sem commit remove o intent ativo, enquanto efeitos externos incertos permanecem bloqueados para reconciliação. Proposta e outbox são commitadas juntas. `scout_status` e exportação reautorizam antes de carregar o snapshot. O exportador `scout-transcript-draft/v1` valida as referências contra a fonte de verdade e custody, ordena os dados e emite JSON compacto UTF-8 com newline; o SHA-256 é retornado fora do documento. Os bytes originais não entram no transcript. O recibo de approval local apenas prova consistência interna: não tem assinatura criptográfica nem autenticação corporativa. A instalação BC15 inclui schemas via `importlib.resources`; wheel e sdist são exercitados fora do checkout.

O transcript é uma projeção das evidências e findings selecionados pela proposta, não de todas as capturas da sessão. A proposta deve incluir todas as referências de manifest dos findings selecionados e pode incluir evidência adicional de apoio. Enquanto o contrato transcript v1 exigir uma página por URL, uma sessão recusa uma nova captura de URL já observada antes de gravar receipt ou custody; use uma nova sessão para capturar novamente essa URL.

## Enforcement e limites

URLs são canonicalizadas para HTTPS, IDNA e porta 443; userinfo, IPs privados/literais não públicos, dot segments, escapes ambíguos e parâmetros de query com nomes de credenciais são rejeitados. Prefix matching respeita limite de segmento. O transporte real exige `http://squid:3128`; Squid resolve destinos, restringe CONNECT a hostnames administrativos e nega ranges privados/especiais antes de conectar. Cada redirect passa novamente pela autorização e pelo budget no adapter; bytes, tipo de resposta e duração têm limites. Os bytes originais da resposta final ficam na custódia e o MCP recebe somente texto extraído limitado, links em escopo e receipt. Corpos intermediários de redirects ainda não entram em manifest. Texto, links e outros bytes recebidos são conteúdo externo não confiável, nunca instruções ou policy.

O probe Docker prova isolamento apenas para o container worker de teste; ele não impede um agente em outro processo de usar shell ou rede direta. Source/policy file e principal são configuração local, não integrações produtivas BC01/BC11. O smoke desta etapa deve usar fixture HTTPS controlada através do broker. Um site público só pode ser usado com fonte e path registrados/aprovados e allowlist administrativa explícita; nenhuma restrição, login ou CAPTCHA deve ser contornado. Nenhum bot de produção é validado ou executado. PostgreSQL durable recovery e o sandbox do daemon Docker precisam de gate local/CI próprio antes de declarar GO do PR.
