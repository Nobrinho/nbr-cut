# NBR Cut

App Windows para **cortar, padronizar e postar filmes direto no canal**, sem mandar o arquivo para o bot.

1. Adicione os filmes (**+ Adicionar filmes**).
2. O app sugere título e ano pelo nome do arquivo e **busca no TMDB**; você escolhe o filme certo.
3. Ele propõe o **nome padronizado** (`Título (Ano) [Qualidade].ext`), mostra o **plano de corte** (partes de até
   1900 MiB; 3900 MiB com conta Premium) e a **prévia da legenda**.
4. **Postar no canal…** confirma o destino e sobe tudo pela sua conta do Telegram, com progresso, velocidade e
   tempo restante. Ao terminar, registra o filme no bot e pede a atualização do índice do app Nbr PLAY.

Também: **Só cortar…** (grava as partes numa pasta para envio manual), **Postar todos os prontos** e
**Envios interrompidos** (retoma ou descarta o que ficou pela metade).

## Independente do bot

O app **não importa nada** do repositório do bot (`agente_filmes`). O que ele compartilha com o bot é um
**contrato de dados** (formato da legenda, nome `.partNNofMM`, esquema do registro e da fila de ordens), descrito
em [CONTRATO.md](CONTRATO.md) e vigiado por `tests/test_contrato_bot.py`.

- Os trechos do bot que o app usa foram **copiados** para `cortador/compartilhado/` (extrator de nome, cliente
  do TMDB para filmes, legenda, formato das partes) e o acesso ao registro/fila foi reescrito em
  `armazenamento.py` e `comandos.py`.
- A **configuração é própria** (`%APPDATA%\NbrCut\config.json`); os segredos (hash do Telegram, chave do
  TMDB, URL do banco) são gravados protegidos pela DPAPI do Windows. Quem já tem o bot usa o botão
  **Importar do .env do bot…** uma única vez.
- O **registro do bot é opcional**: sem ele o app posta normalmente e o monitor do bot registra o filme na próxima
  varredura (só não checa duplicidade antes de subir).

## Como funciona (resumo)

- Corte **por bytes**, sem recodificar e **sem copiar** o filme: cada parte é lida direto do arquivo original durante
  o upload. Nome das partes: `<arquivo>.partNNofMM` (o app Nbr PLAY junta na hora de tocar).
- Ordem de envio: partes N…2, depois o texto de metadados (se a legenda passar de 1024 caracteres) e **por último a
  parte 1**. O app só mostra o card da parte 1, então o filme só aparece quando está completo.
- Arquivo que cabe numa parte só sobe como **vídeo** (com duração e streaming); o sincronismo do monitor precisa disso.
- Falhou no meio? O estado fica salvo (`%APPDATA%\NbrCut\jobs`) e dá para **retomar** só o que falta.
- Filme já publicado (`tmdb:{id}` no registro) é bloqueado **antes** de subir qualquer byte.

## Primeiro uso

1. **Configurações → Importar do .env do bot…** (ou preencha os campos): `api_id` e `api_hash` do Telegram
   (my.telegram.org), chave do TMDB, canal de destino e, opcionalmente, a URL do Postgres do bot.
2. Ligue o **modo teste** e informe um canal privado de teste para os primeiros envios.
3. **Conectar ao Telegram** → telefone, código e (se houver) senha de duas etapas. É uma sessão **própria** do app
   (`%APPDATA%\NbrCut\telegram.session`); a do monitor do bot não é compartilhada.
4. O Postgres do bot (Docker) precisa estar no ar para checar duplicidade e registrar.

## Desenvolvimento

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m cortador          # abre o app
```

### Testes

```powershell
.venv\Scripts\python.exe -m pytest            # tudo (a interface precisa de ambiente gráfico)
```

`tests/test_contrato_bot.py` compara o app com o código do bot e só roda quando o repo do bot está acessível
(`NBR_BOT_DIR`, padrão `C:\DEV\agente_filmes`); sem ele, pula. Alguns testes de contrato precisam importar o
`bot.py` inteiro, que exige as dependências do bot — para exercitá-los, rode esse arquivo com o Python do venv do
bot: `C:\DEV\agente_filmes\venv\Scripts\python.exe -m pytest tests\test_contrato_bot.py`.

### Executável

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

Saída em `dist\NbrCut.exe` (~28 MB). O antivírus às vezes marca executáveis `--onefile` do PyInstaller como
suspeitos (falso positivo); nesse caso use `-PastaUnica`.

### Notas de arquitetura

- O upload roda numa thread com loop asyncio próprio, e as threads de fundo **nunca** tocam no Tk: entregam
  funções numa fila que a thread da interface esvazia. O GC automático do Python fica desligado (a própria
  interface coleta a cada 3 s): sem isso, o coletor podia finalizar um objeto do Tk dentro da thread do upload e
  travá-la para sempre.
- O laço de upload é próprio (`telegram_envio.py`): blocos de 512 KB lidos do original, vários em voo, retry por
  bloco e espera de `FloodWait`.
