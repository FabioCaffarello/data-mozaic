# ADR 007 — Recuperação durável de comandos Scout

- Status: Proposed
- Contexto: S1.1 define semântica local de idempotência, CAS e efeitos externos incertos. Fakes em memória não sobrevivem a crash nem coordenam processos.
- Problema: impedir que timeout ou perda de ACK seja interpretado como falha sem efeito ou sucesso confirmado, mantendo recibes consultáveis e efeitos reconciliáveis.
- Decisão proposta: quando uma implementação durável for autorizada, persistir agregado, versão, recibes de comando e outbox na mesma transação; executar CAS por versão; atribuir IDs estáveis a efeitos BC06/BC15; registrar estados `pending`, `uncertain` e `confirmed`; reconciliar consultando o proprietário do efeito antes de repetir; revalidar autorização para leitura/replay; definir retenção de recibes e política de artefatos órfãos antes do primeiro uso produtivo.
- Alternativas: transação distribuída síncrona entre BC14/06/15; efeitos sem deduplicação com compensação; gravar apenas agregado e inferir comandos. Transação distribuída amplia acoplamento e disponibilidade; compensação não remove necessariamente efeitos externos; inferência não resolve ambiguidade de ACK.
- Consequências: requer contratos idempotentes nos BC06 e BC15 e integração entre banco e dispatcher; outbox não garante exatamente uma entrega, mas possibilita entrega ao menos uma vez com deduplicação no destino. Necessita políticas de retenção, autorização, reconciliação e operação.
- Riscos: contrato do receptor pode não oferecer consulta por ID; recibes expirados permitem replay tardio; autorização pode mudar entre commit e despacho. Egress e custódia continuam responsabilidades próprias.
- Critérios de validação: testes com crashes/restarts em cada fronteira de efeito, conflito CAS em banco real, duplicação de mensagens, ACK perdido, artefatos órfãos e retenção; revisão humana de segurança, ownership e operação. Nenhum backend ou broker é escolhido por este ADR.
