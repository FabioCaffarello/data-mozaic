# ADR 014 — Isolamento de egress do tool worker Scout

- Status: Proposed
- Contexto: o adapter HTTP do BC14 precisa de DNS/egress observável e proibido fora de policy; prompts e checks no adapter não isolam o processo que o hospeda.
- Problema: permitir somente um proxy de saída controlado para o worker, com negação de conexão direta e restrição de destino.
- Alternativas: rede bridge padrão; `network_mode: none` sem acesso autorizado; proxy local sem isolamento de rede; worker em rede Compose interna e proxy Squid dedicado conectado também à rede externa.
- Decisão proposta: worker participa somente de uma rede Compose `internal: true`; Squid é o único serviço que participa também da rede com saída. O adapter fixa seu transporte em `http://squid:3128`; redirects, GET, origem, path, budgets e evidência permanecem no adapter. Squid aceita somente CONNECT 443 para nomes explicitamente listados e nega ranges locais/especiais antes de conectar.
- Consequências: Docker vira requisito apenas para o runtime isolado; checks offline continuam sem Docker. A allowlist do proxy é input administrativo e limita hostnames, enquanto a policy executável BC14 limita origem, caminho e budgets por source. O proxy não inspeciona HTTPS path/method; isso é responsabilidade do adapter. Egress firewall no deployment permanece defense in depth.
- Limites: o worker/probe ainda não é um MCP host integrado. Isolar o worker não restringe uma shell/network separada do agente que o chama. Imagem Squid está pinada por digest; atualização e revisão de CVEs são operação necessária.
- Validação: `make scout-sandbox-check` prova que o worker não abre TCP nem resolve nomes diretamente para egress e que o proxy nega hostname não registrado e destino loopback mapeado localmente, sem buscar conteúdo público.
