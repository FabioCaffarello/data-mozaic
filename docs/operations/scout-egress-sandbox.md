# Sandbox de egress do Scout

O Compose opcional cria duas redes: o worker fica somente na rede interna sem rota externa; Squid é o único serviço também conectado à rede de egress. O adapter exige `http://squid:3128` e não herda proxy do ambiente. Docker Compose documenta que uma rede `internal` não tem conectividade externa e que um serviço em ambas as redes pode acessar a rede externa ([Docker networking](https://docs.docker.com/compose/how-tos/networking/)). A imagem Squid é publicada pela Canonical e está fixada por digest ([imagem `ubuntu/squid`](https://hub.docker.com/r/ubuntu/squid)).

## Prova local

```sh
make scout-sandbox-check
```

Este alvo requer Docker e é separado dos checks offline. O probe tenta conexão TCP direta e resolução DNS direta e exige falha; então exige HTTP 403 do proxy para um hostname não registrado e para um hostname registrado mapeado em loopback dentro do container proxy. Não faz requisição HTTP a um site público. O Compose e as redes são removidos ao final.

## Allowlist de origem

O arquivo padrão [egress-domains.example.txt](../../config/scout/egress-domains.example.txt) só lista `blocked-target.example.test`, um domínio de teste não roteável. Para uma fonte aprovada, copie para `.local/scout-egress-domains.txt` e liste nomes de host exatos, um por linha. Passe seu caminho absoluto como `SCOUT_EGRESS_DOMAINS_FILE`. Esse arquivo é configuração administrativa ignorada pelo Git; ele só restringe destinos no proxy e não substitui source registration ou autorização BC14.

Squid permite apenas CONNECT na porta 443, exige hostname listado e bloqueia ranges IPv4/IPv6 locais e especiais com ACL de IP destino ([referência Squid](https://www.squid-cache.org/Doc/config/acl/)). Ele termina o túnel em TCP/TLS sem descriptografar HTTPS: origem, caminho, GET, redirects, budgets e bytes ainda dependem do adapter. Egress firewall fora do container continua necessário para ambientes operacionais.

## Limites atuais

Esta PR entrega a topologia isolada e sua prova, mas ainda não instala um MCP/tool host. O container probe não é o worker de produção. Nenhum agent foi executado nesta sandbox; uma shell/rede separada do agente não é restringida por este Compose. Não use isso como evidência de workflow Codex seguro nem habilite navegação real até conectar o host a esta rede e provar essa integração.
