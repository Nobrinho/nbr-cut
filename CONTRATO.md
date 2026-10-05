# Contrato com o bot (agente_filmes) e com o app Nbr PLAY

O Nbr Cortador **não importa código do bot**. Ele conversa com o bot e com o app por quatro contratos de dados.
Se o bot mudar um deles, `tests/test_contrato_bot.py` quebra (quando o repo do bot está acessível) e este
documento diz o que ajustar no Cortador.

Fonte no bot: `partes.py`, `legenda.py`, `extrator.py`, `tmdb_client.py`, `db_store.py`, `fila_comandos.py`,
`bot._registrar_postagem_publicada`.

## 1. Nome das partes (`.partNNofMM`)

`<nome do arquivo>.partNNofMM` — `NN` = parte (1..MM), `MM` = total, ambos com no mínimo 2 dígitos
(`Duna (2024) [1080p].mkv.part01of03`). Total ≥ 2; arquivo que cabe numa parte **não** recebe sufixo.
O mesmo formato é lido por `PartName.kt` (app), `partes.py` (bot) e `ntv2_split.py`.
Cópia: `cortador/compartilhado/formato.py`.

Tamanho da parte: 1900 MiB (conta comum) ou 3900 MiB (Premium). A parte é um documento; arquivo único sobe como
**vídeo** com `DocumentAttributeVideo` (o sync do monitor só mantém no registro o que tem `mensagem.video`
ou documento `.partNNofMM`).

## 2. Mensagens no canal

Ordem de envio: partes N…2 → texto de metadados (somente se a legenda excede 1024 caracteres) → parte 1.
Portanto o card (parte 1) só existe com o filme completo. A parte 1 leva a legenda (`montar_legenda_filme`,
cópia em `compartilhado/legenda.py`) ou, com texto separado, a referência `TMDB:{id}` (`legenda_ref`).
O app Nbr PLAY resolve as partes irmãs numa janela de mensagens ao redor da parte 1.

## 3. Registro de postagens — `app_state.postagens_publicadas`

Tabela `app_state(key TEXT PRIMARY KEY, value JSONB)` do Postgres do bot (ou, sem banco, `<pasta>/<chave>.json`).
Chave do registro: `postagens_publicadas` → `{ "tmdb:{id}": { … } }`.

| Campo | Valor gravado pelo Cortador |
|---|---|
| `tipo` | `"filme"` |
| `custom_id`, `serie_tmdb_id`, `titulo_serie`, `temporada`, `episodio` | `null` (v1 só filmes) |
| `tmdb_id`, `titulo`, `ano` | do TMDB |
| `nome_arquivo` | nome padronizado, **sem** sufixo `.partNNofMM` |
| `file_id` | `null` (upload por conta de usuário não tem file_id do Bot API) |
| `message_ids` | **ordem lógica**: `[texto?, parte 1, …, parte N]` |
| `publicado_em` | ISO, segundos |
| `origem` | `"cortador"` (campo extra; o bot ignora) |
| `partes`, `texto_separado`, `video_message_id` | só quando total > 1 (`campos_registro`) |

Regras:

- Escrita sempre transacional com `SELECT … FOR UPDATE` (`Armazenamento.atualizar`), `statement_timeout=5s`,
  `lock_timeout=3s`. Nunca sobrescreve a chave inteira sem ler.
- **A chave nunca é criada pelo Cortador.** Se `postagens_publicadas` não existe, o bot ainda pode migrar o histórico
  dele a partir de arquivos; criar uma chave vazia faria essa migração ser pulada. Nesse caso o app avisa e deixa o
  monitor registrar.
- Duplicidade: `tmdb:{id}` já presente → bloqueia antes de subir qualquer byte.

## 4. Fila de ordens — `comandos_bot` / `status_comandos` / `ponto_executores`

Depois de registrar, o app pede ao bot que regenere o índice (GitHub Pages) enfileirando:

```json
{"jobs": [{"id": "<12 hex>", "tipo": "regerar_indice", "criado_em": "<iso>", "origem": "cortador",
           "criado_por": null, "params": {"motivo": "<texto>"}, "retorno": null}]}
```

e, **antes** de enfileirar, grava em `status_comandos[<id>]` o estado `"pendente"` (um "pendente" tardio nunca
pode sobrescrever o que o executor já escreveu). O bot bate ponto em `ponto_executores["bot"]` (ISO) a cada
5–10 s; o Cortador considera o bot vivo se o último ponto tem ≤ 40 s. Se não está vivo, a ordem fica na fila e é
executada quando ele subir.

## Divergências intencionais em relação ao bot

- **Busca TMDB só de filmes**, sem os fallbacks (OMDb/Wikidata) do cliente do bot.
- **Sem séries/episódios** (v1).
- **Registro opcional**: sem banco/pasta o app posta sem checar duplicidade nem registrar.
- Sessão do Telegram própria; nada do `.env` do bot é lido em tempo de execução (só importado uma vez, a pedido).
- O campo `origem` no registro (`"cortador"`) e na ordem enfileirada.

## Como atualizar uma cópia

Os arquivos de `cortador/compartilhado/` têm no cabeçalho de que arquivo do bot vieram. Para sincronizar: copie o
trecho novo do bot, rode `tests/test_contrato_bot.py` com o Python do venv do bot e ajuste até ficar verde.
