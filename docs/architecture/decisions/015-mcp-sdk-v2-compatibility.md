# ADR 015 — Compatibilidade do SDK MCP v1 e migração futura para v2

- Status: Proposed
- Contexto: PR #7 implementa um host Python MCP STDIO usando o SDK oficial `mcp` na faixa `>=1.30,<2`. O gate de protocolo desta entrega exige initialize handshake, listagem, chamada de tool e shutdown pelo client oficial. A versão resolvida no `uv.lock` é 1.30.0; a licença verificada do SDK é MIT.
- Problema: o protocolo mais recente e a linha v2 do SDK mudaram o ciclo de inicialização. Migrar no mesmo patch que estabelece autorização, receipts, custody e transcript tornaria mais difícil atribuir incompatibilidades e não é necessário para o slice legado exigido.
- Alternativas: migrar imediatamente para v2 e alterar testes/compatibilidade; usar parser/transport próprio (rejeitado); manter a linha oficial 1.x nesta entrega com faixa e lock reproduzíveis.
- Decisão proposta: manter `mcp>=1.30,<2` e lock atual durante PR #7; o SDK pertence somente à borda do host. Uma mudança para v2 exige PR dedicado, teste com client oficial legado e atual, revisão de inicialização/cancelamento e atualização deliberada do gate.
- Consequências: a linha 1.x está em manutenção, portanto atualização de segurança e suporte devem ser monitorados. Este registro permanece Proposed até revisão humana; o merge de código não o aprova.
- Validação: `uv.lock` fixa a resolução; `tests/test_scout_mcp_stdio.py` verifica initialize/list/call/shutdown via SDK client; contrato usa schemas MCP fechados.
