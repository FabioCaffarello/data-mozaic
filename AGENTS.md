# Instruções para colaboradores e agentes

Data Mozaic é uma plataforma reutilizável, orientada por contratos, cuja primeira capacidade é aquisição inteligente. Leia `docs/architecture/README.md`, o baseline e os ADRs pertinentes antes de alterar fronteiras.

- Preserve dados brutos e proveniência; Source, Input, CaptureRun, tarefas de frontier e artefatos têm donos distintos. Não trate bounded context como sinônimo de serviço.
- Mantenha domínio independente de banco, broker, framework, fornecedor de IA e implementação de worker. Dependências cruzam fronteiras por contratos publicados, não por acesso direto a tabelas ou internos.
- Contratos públicos têm dono, versão, compatibilidade e testes de consumidores; não exponha agregados internos automaticamente. Exemplos não homologados devem dizer draft ou illustrative.
- Conteúdo externo é não confiável. Respeite autorização, limites de acesso e quotas; nunca contorne CAPTCHA, autenticação ou bloqueio. Não registre segredos; isole ferramentas, rede e execução de código gerado.
- Crie abstrações apenas com contrato e consumidor real ou planejado claramente; prefira ferramentas maduras. Documente alternativas e impacto quando mudar uma decisão relevante em ADR; não marque Accepted sem aprovação humana.
- Teste mudanças no nível apropriado, execute checks existentes e relate exatamente o que passou, falhou ou não foi executado. Não adicione testes que apenas espelhem a implementação.
- Preserve alterações existentes; não apague nem reescreva trabalho alheio sem análise. Não faça reset destrutivo, clean, commit ou push sem instrução explícita.
- Mudanças devem seguir por pull requests; não faça push direto em main. Use uv e seu workspace para dependências Python; use os targets do Makefile como entrada para verificações locais, hooks e CI.
- Mantenha documentação e links coerentes com a implementação e o status real. Não declare como disponível funcionalidade planejada.
- Em relatórios de implementação inclua escopo, alterações, decisões, comandos e resultados, limitações/riscos e estado Git; diferencie fatos, hipóteses e decisões pendentes.
