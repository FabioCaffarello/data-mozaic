# ADR 014 — Isolamento de egress do tool worker Scout

- Status: Proposed
- Contexto: o adapter HTTP do BC14 precisa de DNS/egress observável e proibido fora de policy; prompts e checks no adapter não isolam o processo que o hospeda.
- Problema: permitir somente um proxy de saída controlado para o worker, com negação de conexão direta e restrição de destino.
- Alternativas: rede bridge padrão; `network_mode: none` sem acesso autorizado; proxy local sem isolamento de rede; worker em rede Compose interna e proxy Squid dedicado conectado também à rede externa.
- Decisão proposta: worker participa somente de uma rede Compose `internal: true`; Squid é o único serviço que participa também da rede com saída. O adapter fixa seu transporte em `http://squid:3128`; redirects, GET, origem, path, budgets e evidência permanecem no adapter. Squid aceita somente CONNECT 443 para nomes explicitamente listados e nega ranges locais/especiais antes de conectar.
- Consequências: Docker vira requisito apenas para o runtime isolado; checks offline continuam sem Docker. A allowlist do proxy é input administrativo e limita hostnames, enquanto a policy executável BC14 limita origem, caminho e budgets por source. O proxy não inspeciona HTTPS path/method; isso é responsabilidade do adapter. Egress firewall no deployment permanece defense in depth.
- Limites: o worker/probe ainda não é um MCP host integrado. Isolar o worker não restringe uma shell/network separada do agente que o chama. Imagem Squid está pinada por digest; atualização e revisão de CVEs são operação necessária.
- Validação: `make scout-sandbox-check` prova que o worker não abre TCP nem resolve nomes diretamente para egress e que o proxy nega hostname não registrado e destino loopback mapeado localmente, sem buscar conteúdo público.

## Complemento proposto para exploração HTTP controlada (PR #8)

Status continua **Proposed**. Para o adapter HTTP, Squid é o resolvedor e socket opener. ACL `dst` nega a resposta DNS inteira se qualquer candidato A/AAAA atingir os ranges proibidos; ACL e forwarding usam o cache IP do Squid, evitando uma segunda resolução independente entre validação e conexão. A imagem Squid permanece fixada por digest. O harness `make scout-dns-adversarial-check` configura o DNS no próprio container Squid, valida mixed-answer/rebinding e exige evidência do peer real no access log (`%<a`) e zero accepts no sink bloqueado. O evento de observabilidade BC14 registra SHA-256 da policy local e origin/path hash; o log do proxy registra host, peer, hierarquia, status e método sem path/body/headers.

Esse complemento não instala firewall independente na interface egress do Squid nem contém o MCP Host/Codex local. A validade da garantia depende da versão/configuração do broker; deployment operacional deve acrescentar firewall de egress com política IPv4 e IPv6 equivalente e reexecutar testes no runtime real antes de alegar isolamento global.
