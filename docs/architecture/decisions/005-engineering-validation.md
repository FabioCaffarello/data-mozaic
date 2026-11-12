# ADR 005 — Engenharia e validação

- Status: Proposed
- Contexto: repositório vazio e nenhum componente funcional, múltiplas toolchains disponíveis localmente.
- Problema: estabelecer checks úteis e builds reprodutíveis sem criar workspaces ou CI fictícios.
- Drivers de decisão: simplicidade, evidência, portabilidade e segurança de supply chain.
- Alternativas: configurar todas as linguagens agora; diferir workspaces até primeiro componente; CI com grafo completo; checks simples graduais.
- Trade-offs: deferir versões adia padronização; configurar cedo cria arquivos inválidos ou sem consumidores. Grafo incremental reduz custo futuro mas aumenta complexidade inicial.
- Decisão proposta: workspace uv na raiz e lock compartilhado para Python; Makefile como interface para CI e pre-commit; CI executa checks reais desde a fundação. Auditoria e proveniência de artefatos serão adicionadas com componentes publicáveis. Testes de fronteira e contratos evoluem com consumidores reais.
- Consequências: o CI cobre documentação e tooling, ainda sem build ou CI de serviços; critérios devem crescer com implementação. Os membros do workspace exigem dependências mutuamente compatíveis.
- Riscos: verificador de documentação não comprova validade de Mermaid ou arquitetura; versões locais não são suporte oficial.
- Critérios de validação: executar checker positivo e casos negativos, revisar links/diagramas, criar primeiro workflow somente com comandos executáveis.
