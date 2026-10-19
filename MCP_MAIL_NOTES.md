# MCP_MAIL_NOTES.md

Documentação de referência para a criação de dois novos módulos MCP seguindo o padrão deste
repositório (`bruno_calendar_mcp.py` / `bruno_contact_mcp.py`):

- **`bruno_mail_mcp.py`** — expõe o Mail.app (macOS).
- **`bruno_notes_mcp.py`** — expõe o Notes.app (macOS).

Este arquivo não é código — é o levantamento do que é possível fazer em cada app, como acessar
essas funcionalidades a partir de Python, e as armadilhas conhecidas, para servir de base ao
implementar os dois módulos.

## Diferença arquitetural importante em relação a Calendar/Contacts

`bruno_calendar_mcp.py` e `bruno_contact_mcp.py` usam **EventKit** e **Contacts**, frameworks
Cocoa públicos acessados via `pyobjc`. Mail.app e Notes.app **não têm framework público
equivalente** — não existe `MailKit` nem `NotesKit` para apps de terceiros. O único jeito
suportado pela Apple de automatizar esses dois apps é via **AppleScript/JXA (Open Scripting
Architecture)**, o que muda a arquitetura do zero:

- Em vez de `EKEventStore`/`CNContactStore`, o acesso é via **ScriptingBridge**
  (`pyobjc`: `from ScriptingBridge import SBApplication`,
  `SBApplication.applicationWithBundleIdentifier_("com.apple.mail")` /
  `"com.apple.Notes"`), ou via `subprocess` chamando `osascript` com um script
  AppleScript/JXA embutido.
- **Recomendação**: usar `ScriptingBridge` quando possível (mais parecido ao estilo pyobjc já
  usado no projeto, sem precisar escapar strings para shell), com fallback para
  `osascript -l JavaScript` (JXA) nos pontos em que a dictionary do ScriptingBridge se mostrar
  incompleta (isso é uma reclamação recorrente, especialmente em Notes, para corpo com
  checklists/anexos).
- **Permissão**: não é o popup único de "Calendar/Contacts access" que já existe hoje. É
  **Automação** (System Settings → Privacy & Security → Automation): o processo que chama
  (Python/Terminal/o app empacotado) precisa de permissão explícita para controlar
  "Mail" e "Notes", concedida app a app, na primeira vez que o script tenta enviar um evento
  Apple Event para eles. Se o Mail.app/Notes.app não estiverem abertos, o primeiro comando
  dispara a abertura do app (pode ser lento) e aí sim o popup aparece.
- **Mail.app e Notes.app precisam estar rodando (ou serão lançados automaticamente)** para
  responder aos Apple Events — diferente do EventKit, que funciona em background sem o
  Calendar.app aberto.
- Logging continua **somente em arquivo** (nunca `print`/stdout) — mesma regra do
  `CLAUDE.md` do projeto, já que stdout é o canal do protocolo MCP.

---

## 1. Mail.app

### Hierarquia de dados

```
Application
 └─ Accounts (imap/pop/icloud/exchange account)
     └─ Mailboxes (pastas: Inbox, Sent, Drafts, Trash, + pastas custom, recursivas)
         └─ Messages
```

- Uma **conta** (`account`) corresponde a uma conta de e-mail configurada no Mail.app
  (iCloud, Gmail/IMAP, Exchange, etc.). Propriedades úteis: `name`, `id`, `email addresses`,
  `enabled`.
- Cada conta tem suas próprias **mailboxes** (pastas: Inbox, Sent, Drafts, Junk, Trash, e
  pastas criadas pelo usuário — inclusive aninhadas). Existe também `mailbox` "unificada"
  (todas as contas) dependendo da view, mas via script sempre se navega conta por conta.
- Uma **mensagem** tem: `subject`, `sender`, `to/cc/bcc recipients`, `content` (corpo,
  texto puro — HTML só é acessível de forma limitada), `date sent`/`date received`,
  `read status`, `flagged status`, `mailbox` (pasta onde está), `id`, e uma lista de
  `mail attachments`.

### O que dá para fazer (mapeamento para possíveis tools)

| Tool sugerida | O que faz |
|---|---|
| `list_mail_accounts()` | Lista contas configuradas (nome, endereço(s), habilitada/não) |
| `list_mailboxes(account)` | Lista pastas de uma conta (inclusive aninhadas) |
| `create_mailbox(name, account, parent)` | Cria pasta nova dentro de uma conta (ou subpasta) |
| `delete_mailbox(mailbox_id)` | Apaga uma pasta |
| `list_messages(account, mailbox, page, page_size, unread_only)` | Lista mensagens paginado (mesmo padrão de `list_events`/`list_contacts`) |
| `search_messages(query, account, mailbox)` | Busca por assunto/remetente/conteúdo |
| `get_message(message_id)` | Detalhe completo de uma mensagem (corpo, anexos, cabeçalhos) |
| `send_message(to, cc, bcc, subject, body, account, attachments)` | **Disparo de e-mail** — cria e envia uma "outgoing message" |
| `reply_message(message_id, body, reply_all)` | Responde um e-mail existente |
| `forward_message(message_id, to, body)` | Encaminha um e-mail |
| `move_message(message_id, to_mailbox)` | Move mensagem entre pastas/contas |
| `mark_message(message_id, read, flagged)` | Marca como lida/não lida, sinalizada |
| `delete_message(message_id)` | Apaga (move para o Trash da conta) |
| `download_attachment(message_id, attachment_name, dest_path)` | Salva um anexo em disco |

### Envio de e-mail ("disparo") — detalhes e armadilhas

- **Padrão AppleScript/JXA para enviar**:
  1. Criar uma *outgoing message* (`make new outgoing message with properties {subject:…, content:…, visible:false}`).
  2. Adicionar destinatários (`make new to recipient at end of to recipients of msg with properties {address:…}`), e da mesma forma `cc recipient`/`bcc recipient`.
  3. Opcionalmente setar `sender` da mensagem (endereço de uma das contas habilitadas) — se
     omitido, Mail usa a conta/identidade padrão configurada nas preferências.
  4. Anexos: adicionar antes de enviar, referenciando o arquivo (`POSIX file "/caminho"`) como
     attachment do corpo da mensagem — a API só aceita arquivo local, não é possível anexar a
     partir de bytes em memória sem escrever em disco primeiro.
  5. Chamar `send msg`.
  - **Se o passo 5 for omitido, a mensagem fica como rascunho** — é um erro comum em scripts
    copiados da internet que só "fazem" a mensagem sem enviar.
- **Depois de `send`, a mensagem não pode mais ser consultada/manipulada** (Mail "solta" o
  objeto) — então não dá para, no mesmo comando, pegar o id da mensagem enviada e movê-la para
  uma pasta específica. Se for necessário (ex.: mover para uma pasta "Enviados/Projeto X"),
  isso exige uma Mail Rule configurada no próprio app, ou um polling posterior na mailbox
  "Sent" para localizar a mensagem por assunto+data logo após o envio — **não existe um jeito
  atômico via script**. Documentar isso como limitação conhecida (igual ao padrão de "gotchas"
  do `CLAUDE.md`).
- **Conta de envio**: se a conta não for setada explicitamente e o usuário tiver múltiplas
  contas configuradas, o Mail usa a conta default global (Mail > Settings > Composing). Para
  "disparo" previsível pelo MCP, a tool `send_message` deveria **exigir** o parâmetro `account`
  (ou endereço do remetente) sempre que houver mais de uma conta, para evitar enviar pelo
  endereço errado.
- **HTML no corpo**: via AppleScript, `content` de uma outgoing message é texto, não HTML
  renderizado — formatação rica (negrito, links, imagens inline) não é diretamente suportada
  pela dictionary pública. Para HTML real, a rota conhecida é "colar" HTML via
  clipboard/UI scripting (`System Events`) dentro da janela de composição, o que é frágil e
  depende da janela estar em foco — citar como limitação, não implementar isso na v1.
- **Rate limiting / múltiplos envios**: Mail.app não tem um limite artificial, mas enviar em
  massa (loop) pelo mesmo mecanismo do usuário pode disparar os limites do próprio provedor
  (Gmail/iCloud) e ser percebido como abuso — **qualquer tool de disparo em lote precisa ser
  tratada como potencialmente destrutiva/irreversível** (e-mail enviado não tem "undo") e deve
  pedir confirmação explícita antes de qualquer envio para múltiplos destinatários, assim como
  o projeto já trata `delete_event`/exclusões como operações sensíveis.

### Leitura e anexos

- Corpo de mensagens recebidas (`content`) normalmente retorna o texto; para o HTML original
  completo, às vezes só está disponível via `source` (o `.eml` cru) — útil se for necessário
  extrair HTML/headers completos.
- Anexos (`mail attachment` de uma mensagem) têm `name`, `file size`, `downloaded` (bool) e um
  comando `save … in POSIX file "…"` para persistir em disco. Anexos muito grandes podem não
  estar baixados localmente ainda (Mail faz download sob demanda) — pode ser necessário aguardar
  `downloaded = true` antes de salvar.

---

## 2. Notes.app

### Hierarquia de dados

```
Application
 └─ Accounts (iCloud, On My Mac, Gmail/Exchange se IMAP notes habilitado)
     └─ Folders (podem ser aninhadas: Folder dentro de Folder)
         └─ Notes
             └─ Attachments
```

- Uma **conta** (`account`) é a mesma noção de "On My Mac" vs. "iCloud" vs. contas IMAP com
  Notes habilitado (Gmail, Exchange, etc. — quando o provedor suporta notes via IMAP). Nem toda
  conta de e-mail tem notes; só aparece aqui se o usuário habilitou "Notes" para aquela conta
  em Internet Accounts.
- **Pastas (`folders`)** existem dentro de cada conta e podem ser aninhadas livremente
  (pasta dentro de pasta). Isso é exatamente o "questão de pasta e conta" citado — é a mesma
  relação 1 nota → 1 pasta → 1 conta, análoga ao padrão de `_build_account_map()` já usado em
  `bruno_contact_mcp.py` para contatos (lá, grupos; aqui, pastas, mas pasta é mais parecido com
  "container" de Contacts do que com "grupo", porque uma nota pertence a exatamente UMA pasta,
  não a várias).
- **Smart Folders** (pastas inteligentes baseadas em filtro) **não são scriptáveis** — não
  aparecem na dictionary pública como `folder` comum e não podem ser criadas/lidas via
  AppleScript.
- Uma **nota** tem: `name` (título — na prática é a primeira linha do corpo), `id`,
  `body` (HTML), `creation date`, `modification date`, `container` (a pasta/conta), `shared`
  (bool), `password protected` (bool, para notas trancadas), e contém `attachments`.

### O que dá para fazer (mapeamento para possíveis tools)

| Tool sugerida | O que faz |
|---|---|
| `list_note_accounts()` | Lista contas com Notes habilitado (iCloud, On My Mac, IMAP…) |
| `list_note_folders(account, parent_folder)` | Lista pastas de uma conta, com suporte a navegação em pastas aninhadas |
| `create_note_folder(name, account, parent_folder)` | Cria pasta (opcionalmente dentro de outra) |
| `rename_note_folder(folder_id, name)` | Renomeia pasta |
| `delete_note_folder(folder_id)` | Apaga pasta (e, por padrão do Notes, move as notas dela para a pasta "Notes Deletadas"/lixeira da conta, não apaga direto) |
| `list_notes(account, folder, page, page_size, query)` | Lista notas paginado, com filtro opcional de texto (mesmo padrão de paginação de `list_events`) |
| `get_note(note_id)` | Corpo completo (HTML), anexos, metadados |
| `create_note(title, body_html, account, folder)` | Cria nota nova em uma pasta/conta específica |
| `update_note(note_id, body_html)` | Substitui o corpo de uma nota existente |
| `move_note(note_id, to_folder)` | Move nota entre pastas (inclusive entre contas, com ressalva abaixo) |
| `delete_note(note_id)` | Apaga nota |
| `list_note_attachments(note_id)` | Lista anexos de uma nota |
| `download_note_attachment(note_id, attachment_id, dest_path)` | Salva um anexo em disco |

### Pastas e contas — pontos de atenção

- **Mover nota entre contas** (ex.: de "On My Mac" para "iCloud") via o comando nativo `move`
  do AppleScript **funciona na maioria dos casos**, mas, assim como o "move" de calendário/
  contato neste repo, pode não ser atômico internamente dependendo da versão do macOS —
  recomenda-se testar e, se necessário, implementar como "clonar na pasta destino + apagar
  original" (mesmo padrão já usado em `update_contact` para troca de conta, documentado em
  `CLAUDE.md`), para garantir resultado previsível.
- **Notas trancadas por senha** (`password protected = true`) **não expõem o `body`** via
  script enquanto estiverem bloqueadas — a dictionary retorna vazio ou erro. Não há como
  desbloquear via Apple Events (exigiria Face ID/Touch ID/senha pela UI). Qualquer tool de
  leitura precisa checar esse estado e devolver uma mensagem clara em vez de corpo vazio
  silencioso (mesmo princípio do `_notes_supported` usado para o campo `notes` de contatos).
- **Notas compartilhadas** (`shared = true`, colaborativas via iCloud) podem ser editadas via
  script normalmente, mas a lista de colaboradores/permissões **não é exposta** pela dictionary
  pública — não documentar/implementar isso como feature, é limitação de plataforma.
- **Checklists** (toggle de itens de lista nas notas) são representados no HTML do `body` com
  marcação própria do Notes (não é `<ul><li>` HTML padrão) — editar checklist via substituição
  de `body` funciona para texto simples, mas recriar o comportamento interativo de
  check/uncheck programaticamente é frágil; documentar como "suportado para leitura, edição
  básica de texto; não recomendado para manipular item a item".
- **Anexos**: Notes aceita imagens, PDFs, "Scan documents", desenhos do Markup e anexos de
  arquivos genéricos. Via script, dá para **ler/baixar** anexos existentes (`attachment` tem
  `name`, pode-se buscar o arquivo associado), mas **anexar um arquivo novo programaticamente
  não é suportado de forma confiável** pela dictionary pública (ao contrário do Mail, que tem
  comando de attachment documentado) — a rota realista é inserir uma imagem no `body` HTML via
  tag `<img>` apontando para um `file://` local ou embutida em base64 quando o Notes aceitar;
  tratar como "best effort / pode falhar silenciosamente", nunca prometer como feature
  garantida.

---

## 3. Padrão de implementação recomendado (para manter consistência com o resto do repo)

- Mesma estrutura: `FastMCP("bruno-mail")` / `FastMCP("bruno-notes")`, uma função por
  `@mcp.tool()`, strings simples de entrada/saída.
- Mesmo logging: tudo em arquivo (ex.: `~/bruno_mcp_debug.log`, pode ser o mesmo arquivo dos
  outros módulos ou um dedicado), nunca `print`.
- Mesmo padrão de paginação (`page`/`page_size`, rodapé indicando a próxima página) para
  `list_messages`/`list_notes`, já que ambos podem ter volumes grandes.
- Mesmo cuidado com operações destrutivas/irreversíveis: `send_message` (não tem undo),
  `delete_mailbox`/`delete_note_folder` (apaga conteúdo junto) devem ter comportamento explícito
  e, quando possível, pedir um parâmetro de confirmação — seguindo o mesmo espírito do guard de
  `delete_event` contra truncar série para o passado.
- `_ensure_access()` equivalente: em vez de bloquear em `threading.Event` esperando callback de
  autorização (que não existe para Automation), a abordagem é **tentar o primeiro Apple Event e
  tratar a exceção de permissão negada** com uma mensagem clara orientando o usuário a liberar
  em System Settings → Privacy & Security → Automation → (Python/Terminal) → Mail/Notes.
- Conversão de datas: ScriptingBridge devolve `NSDate` nativamente (compatível com a mesma
  conversão `NSDate.dateWithTimeIntervalSince1970_`/`timeIntervalSince1970()` já usada em
  `bruno_calendar_mcp.py`), então esse trecho de código pode ser reaproveitado como está.

## 4. Resumo das limitações de plataforma (não dá para implementar)

- Mail: HTML rico no corpo de envio, mover mensagem enviada atomicamente após `send`,
  anexar a partir de bytes em memória sem arquivo em disco.
- Notes: ler/editar notas trancadas por senha, listar colaboradores de notas compartilhadas,
  acessar Smart Folders, anexar arquivos novos de forma garantida, manipular itens de
  checklist individualmente.
- Ambos: não há framework público (EventKit-like) — tudo depende de Apple Events via
  ScriptingBridge/AppleScript/JXA, o que exige permissão de Automação (por processo, por app
  alvo) além da permissão de dados já conhecida de Calendar/Contacts.
