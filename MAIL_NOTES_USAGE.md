# Mail e Notes no Claude Desktop — o que dá para fazer

Guia de uso dos módulos `bruno_mail_mcp.py` (Mail.app) e `bruno_notes_mcp.py` (Notes.app) depois de
registrados no Claude Desktop. Para detalhes de implementação/arquitetura, ver [CLAUDE.md](CLAUDE.md)
e [MCP_MAIL_NOTES.md](MCP_MAIL_NOTES.md).

## Antes de usar

1. **Registrar os servidores** em `claude_desktop_config.json` (cada módulo é um servidor MCP
   separado, com `python3 bruno_mail_mcp.py` / `python3 bruno_notes_mcp.py` como comando).
2. **Reiniciar o Claude Desktop** para carregar os servidores.
3. Na primeira vez que uma ferramenta tentar falar com o Mail.app ou o Notes.app, o macOS mostra
   um popup de permissão em **Settings → Privacy & Security → Automation**, pedindo para liberar o
   processo (Python/Claude) a controlar "Mail" / "Notes". Sem aceitar isso, toda chamada retorna
   uma mensagem de erro em vez de dados.
4. Mail.app/Notes.app precisam estar abertos (ou são abertos automaticamente na primeira chamada —
   pode demorar alguns segundos).

Se alguma ferramenta falhar de forma estranha, o log de debug (sempre em arquivo, nunca no
terminal) fica em `~/bruno_mcp_debug.log`.

---

## Mail.app (`bruno-mail`)

Hierarquia: **conta de e-mail** → **mailbox/pasta** (Inbox, Sent, Drafts, Trash, pastas próprias,
aninháveis) → **mensagem** → anexos.

### Consultar

| Peça ao Claude... | Ferramenta | Observações |
|---|---|---|
| "quais contas de e-mail estão configuradas?" | `list_mail_accounts()` | nome, endereço(s), habilitada ou não |
| "lista as pastas da minha conta X" | `list_mailboxes(account)` | inclui pastas aninhadas; `account` vazio = todas as contas |
| "mostra os últimos e-mails da Inbox" | `list_messages(account, mailbox, page, page_size, unread_only)` | paginado (padrão 50/página, máx. 200); `unread_only` filtra só não lidos |
| "procura e-mails do fulano sobre o projeto X" | `search_messages(query, mailbox, account, page, page_size)` | busca por assunto/remetente, não pelo corpo |
| "abre esse e-mail e mostra o conteúdo" | `get_message(message_id)` | corpo completo, remetente/destinatários, anexos |

### Agir

| Peça ao Claude... | Ferramenta | Observações |
|---|---|---|
| "manda um e-mail para fulano@x.com com assunto Y e texto Z" | `send_message(to, subject, body, cc, bcc, account, attachments)` | **envio é irreversível**; se houver mais de uma conta configurada, `account` é obrigatório; corpo é só texto puro (sem HTML rico); anexos são caminhos de arquivo locais |
| "responde esse e-mail dizendo que..." | `reply_message(message_id, body, reply_all)` | compõe uma mensagem nova (assunto `Re:`, original citado abaixo) — não é uma resposta nativa encadeada por `In-Reply-To` |
| "encaminha esse e-mail para fulano" | `forward_message(message_id, to, body)` | assunto `Fwd:`, original citado; **anexos do original não são copiados automaticamente** — baixe com `download_attachment` e reanexe em um `send_message` se precisar |
| "move esse e-mail para a pasta Projetos" | `move_message(message_id, to_mailbox)` | funciona até entre contas diferentes |
| "marca esse e-mail como lido / sinaliza ele" | `mark_message(message_id, read, flagged)` | `read`/`flagged` aceitam `'true'`/`'false'` |
| "apaga esse e-mail" | `delete_message(message_id)` | move para o Trash da conta (ou apaga de vez se já estiver no Trash) |
| "baixa o anexo X desse e-mail" | `download_attachment(message_id, attachment_name, dest_path)` | salva em um caminho local |

### Organização de pastas

| Peça ao Claude... | Ferramenta |
|---|---|
| "cria uma pasta chamada Projetos na minha conta iCloud" | `create_mailbox(name, account, parent)` |
| "apaga a pasta X" | `delete_mailbox(mailbox_id)` |

### Limitações conhecidas (não dá para pedir)

- **Sem HTML rico no envio** — corpo é sempre texto puro (negrito, links, imagens inline não são
  suportados pela API de scripting do Mail).
- **Depois de enviada, uma mensagem não pode ser movida/alterada no mesmo comando** — não existe
  jeito atômico de "enviar e já mover para pasta X".
- **Sem Travel Time/convite de calendário embutido em e-mail**, nem nada parecido — isso é só do
  módulo de Calendar.
- Envio em massa (vários destinatários/loop) pode disparar limites do próprio provedor (Gmail/
  iCloud) — tratar como ação sensível, não automatizar sem revisão.

---

## Notes.app (`bruno-notes`)

Hierarquia: **conta** (iCloud, On My Mac, IMAP com Notes habilitado) → **pasta** (aninhável) →
**nota** → anexos.

### Consultar

| Peça ao Claude... | Ferramenta | Observações |
|---|---|---|
| "quais contas têm Notes habilitado?" | `list_note_accounts()` | |
| "lista minhas pastas de notas" | `list_note_folders(account)` | inclui pastas aninhadas; Smart Folders não aparecem (não são scriptáveis) |
| "lista minhas notas mais recentes" | `list_notes(account, folder, query, page, page_size)` | paginado (padrão 100/página, máx. 300); `query` filtra por texto da nota |
| "abre essa nota e mostra o conteúdo" | `get_note(note_id)` | corpo completo, pasta, datas, anexos; notas trancadas por senha retornam aviso em vez do corpo |
| "lista os anexos dessa nota" | `list_note_attachments(note_id)` | |

### Agir

| Peça ao Claude... | Ferramenta | Observações |
|---|---|---|
| "cria uma nota chamada X com o texto Y na pasta Z" | `create_note(title, body, account, folder)` | título vira a primeira linha do corpo |
| "substitui o conteúdo dessa nota por..." | `update_note(note_id, title, body)` | **substitui o corpo inteiro** — não é um append |
| "move essa nota para a pasta X" | `move_note(note_id, to_folder)` | funciona até entre contas diferentes |
| "apaga essa nota" | `delete_note(note_id)` | vai para "Notas Recentemente Excluídas" da conta |
| "baixa o anexo X dessa nota" | `download_note_attachment(note_id, attachment_id, dest_path)` | |

### Organização de pastas

| Peça ao Claude... | Ferramenta |
|---|---|
| "cria uma pasta chamada Trabalho" | `create_note_folder(name, account, parent)` |
| "renomeia essa pasta para X" | `rename_note_folder(folder_id, name)` |
| "apaga essa pasta" | `delete_note_folder(folder_id)` | notas dela vão para "Recentemente Excluídas", a pasta em si some |

### Limitações conhecidas (não dá para pedir)

- **Notas trancadas por senha**: corpo não pode ser lido nem editado via scripting enquanto
  bloqueadas — precisa desbloquear manualmente no app primeiro.
- **Smart Folders** não existem para o servidor (não são scriptáveis pela Apple).
- **Colaboradores/permissões de notas compartilhadas** não aparecem — só dá para ver se a nota é
  `shared` ou não.
- **Checklists**: leitura e edição de texto simples funcionam, mas marcar/desmarcar itens
  individualmente de forma confiável não é suportado.
- **Anexar um arquivo novo a uma nota existente** não é uma feature garantida (ao contrário do
  Mail) — só leitura/download de anexos já existentes é confiável.

---

## Em comum aos dois módulos

- Toda operação passa primeiro por uma checagem de permissão de Automação — se vier a mensagem de
  "Could not reach any Mail/Notes account...", é sinal de liberar em Settings → Privacy & Security
  → Automation.
- Listagens grandes são paginadas; quando há mais páginas, a resposta já indica como pedir a
  próxima.
- Nenhuma operação de apagar é de fato "instantânea e definitiva" — tanto mensagens quanto notas
  vão primeiro para uma lixeira/pasta de recuperação da própria conta.
