# Instalação do Bruno Personal MCP (Calendário + Contatos)

Guia para instalar estes servidores MCP no Claude Desktop de um computador
Mac novo (do cliente). Funciona apenas em **macOS**.

## 1. Pré-requisitos

- **macOS** (qualquer versão recente; permissões de Calendário/Contatos
  exigem macOS 14+ para acesso total de escrita).
- **Claude Desktop** já instalado.
- **Python 3.9 ou superior**. Verifique com:

  ```bash
  python3 --version
  ```

  Se não tiver Python 3, instale com [Homebrew](https://brew.sh):

  ```bash
  brew install python3
  ```

## 2. Copiar os arquivos do projeto

Copie a pasta inteira do projeto para o computador do cliente, por exemplo
para `~/bruno_mcp/`. Ela deve conter pelo menos:

```
bruno_mcp/
├── bruno_calendar_mcp.py
├── bruno_contact_mcp.py
└── requirements.txt
```

Abra o Terminal e entre na pasta:

```bash
cd ~/bruno_mcp
```

## 3. Criar o ambiente virtual e instalar as dependências

```bash
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

Isso instala o SDK do MCP (`mcp`) e os frameworks nativos do macOS via
`pyobjc` (`EventKit` para calendário, `Contacts` para contatos).

## 4. Confirmar o caminho do Python do ambiente virtual

Anote o caminho absoluto do Python que acabou de ser criado — ele será usado
na configuração do Claude Desktop:

```bash
echo "$(pwd)/venv/bin/python"
```

Exemplo de saída: `/Users/cliente/bruno_mcp/venv/bin/python`

## 5. Registrar os servidores no Claude Desktop

Abra (ou crie) o arquivo de configuração do Claude Desktop:

```bash
open -e "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Se o arquivo não existir, crie-o com este conteúdo (ajustando os caminhos
para os do computador do cliente — use os caminhos obtidos nos passos 2 e 4):

```json
{
  "mcpServers": {
    "bruno-calendar": {
      "command": "/Users/cliente/bruno_mcp/venv/bin/python",
      "args": [
        "/Users/cliente/bruno_mcp/bruno_calendar_mcp.py"
      ]
    },
    "bruno-contact": {
      "command": "/Users/cliente/bruno_mcp/venv/bin/python",
      "args": [
        "/Users/cliente/bruno_mcp/bruno_contact_mcp.py"
      ]
    }
  }
}
```

Se o arquivo já existir com outras configurações, apenas adicione as duas
chaves `"bruno-calendar"` e `"bruno-contact"` dentro do bloco `"mcpServers"`
já existente, sem apagar o que já está lá.

## 6. Reiniciar o Claude Desktop

Feche completamente o Claude Desktop (Cmd+Q) e abra novamente para que ele
carregue os novos servidores MCP.

## 7. Autorizar permissões do macOS

Na primeira vez que uma ferramenta de calendário ou contatos for usada
dentro do Claude, o macOS vai mostrar um pop-up pedindo permissão. Aceite.
Se for negado por engano, libere manualmente em:

**Ajustes do Sistema > Privacidade e Segurança > Calendários** (e
**Contatos**) — marque o Claude/Claude Desktop como autorizado.

## 8. Testar

Dentro do Claude Desktop, peça por exemplo:

- "Liste meus calendários"
- "Liste meus contatos"

Se aparecer a lista corretamente, a instalação funcionou.

## 9. Solução de problemas

Os servidores nunca escrevem no terminal (isso quebraria a comunicação com
o Claude Desktop); todo log vai para um arquivo. Para acompanhar em tempo
real:

```bash
tail -f ~/bruno_mcp_debug.log
```

Problemas comuns:

- **Ferramenta não aparece no Claude**: confira se o JSON em
  `claude_desktop_config.json` é válido (sem vírgulas sobrando) e se os
  caminhos apontam para arquivos que existem nesse computador.
- **"ModuleNotFoundError"**: as dependências não foram instaladas no
  ambiente virtual certo — repita o passo 3.
- **Acesso negado a Calendário/Contatos**: revise o passo 7.
