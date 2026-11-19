# Sandbox de egress do Scout

O Compose opcional cria duas redes: o worker fica somente na rede interna sem rota externa; Squid é o único serviço também conectado à rede de egress. O adapter exige `http://squid:3128` e não herda proxy do ambiente. Docker Compose documenta que uma rede `internal` não tem conectividade externa e que um serviço em ambas as redes pode acessar a rede externa ([Docker networking](https://docs.docker.com/compose/how-tos/networking/)). A imagem Squid é publicada pela Canonical e está fixada por digest ([imagem `ubuntu/squid`](https://hub.docker.com/r/ubuntu/squid)).

## Prova local

```sh
make scout-sandbox-check
```

Este alvo requer Docker e é separado dos checks offline. O probe tenta conexão TCP direta e resolução DNS direta e exige falha; então exige HTTP 403 do proxy para um hostname não registrado e para um hostname registrado mapeado em loopback dentro do container proxy. Não faz requisição HTTP a um site público. O Compose e as redes são removidos ao final.

## Allowlist de origem

O arquivo padrão [egress-domains.example.txt](../../config/scout/egress-domains.example.txt) só lista `blocked-target.example.test`, um domínio de teste não roteável. Para uma fonte aprovada, copie para `.local/scout-egress-domains.txt` e liste nomes de host exatos, um por linha. Passe seu caminho absoluto como `SCOUT_EGRESS_DOMAINS_FILE`. Esse arquivo é configuração administrativa ignorada pelo Git; ele só restringe destinos no proxy e não substitui source registration ou autorização BC14.

Para o host MCP local no próprio computador, a porta do Squid é publicada somente em `127.0.0.1:3128`. Inicie o broker com o domínio já cadastrado no arquivo administrativo e use `SCOUT_EGRESS_PROXY=http://127.0.0.1:3128`. Dentro de um container na rede `tool-internal`, use `http://squid:3128`. A aplicação rejeita outros endpoints de proxy. Não publique essa porta em uma interface não-loopback.

Squid permite apenas CONNECT na porta 443, exige hostname listado e bloqueia ranges IPv4/IPv6 locais e especiais com ACL de IP destino ([referência Squid](https://www.squid-cache.org/Doc/config/acl/)). Ele termina o túnel em TCP/TLS sem descriptografar HTTPS: origem, caminho, GET, redirects, budgets e bytes ainda dependem do adapter. Egress firewall fora do container continua necessário para ambientes operacionais.

## Limites atuais

O alvo `scout-sandbox-check` continua sendo somente um probe de rede, não o processo MCP/tool host de produção. O perfil `scout-http-e2e` separado executa MCP Client e Tool Host reais como processos do host, passando o fetch pelo Squid. Nenhum agente remoto foi executado e uma shell/rede separada do Codex não é restringida pelo Compose. Não use esses testes como prova de isolamento global do agente.

## E2E de HTTP autorizado e DNS controlado

`make scout-http-e2e` também sobe um perfil Compose efêmero com PostgreSQL já ativo, Squid, servidor DNS programável, fixture HTTPS com CA local de curta duração e sink de conexão proibida. `make scout-dns-adversarial-check` executa o mesmo gate, incluindo private/mixed A, mixed AAAA, CNAME, rebinding, retry e SERVFAIL. O DNS configurado é o próprio nameserver usado pelo container Squid, que é o processo que resolve e abre o socket. O access log seguro do Squid registra host, peer IP efetivo, status e método; o runner exige peer `172.30.91.10` no CONNECT positivo e zero accepts no sink `172.30.91.11`.

O perfil E2E remove da lista deny somente o IP estático da fixture HTTPS. Essa exceção fica no arquivo temporário gerado para aquele Compose e não altera a ACL de produção. A rede de teste usa bridge para publicar o proxy apenas no loopback do host; por isso não demonstra isolamento do processo MCP/Codex nem instalação de firewall. A segurança do caminho testado depende do transporte fixar o proxy e do Squid manter a ACL `dst`/cache conforme a imagem digest-pinned. Se o proxy ficar indisponível, o transporte falha sem acesso direto. Não desative a allowlist nem use `--network host` para tornar o teste verde.

Reprodução: `make db-up`, `make db-migrate`, `make scout-http-e2e`. O runner encerra os containers do perfil com `docker compose down` e preserva volumes PostgreSQL. O teste usa apenas IPs de rede de fixture; não envia requisições a destinos públicos, serviços privados reais ou metadata.
