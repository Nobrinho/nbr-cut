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

## Metadados do arquivo e avisos de formato

Ao adicionar um filme o app lê o próprio arquivo (só alguns MB, sem ffprobe) e mostra a seção **MÍDIA**: resolução,
codec, profundidade de cor, HDR/**Dolby Vision (e o perfil)**, fps, duração e a lista de áudios/legendas. Funciona
com MKV (cabeçalho EBML) e MP4/MOV (caixa `moov`, mesmo no fim do arquivo).

- **Avisos** antes de postar, para formatos que o Nbr PLAY não toca bem em todo aparelho: Dolby Vision perfil 7
  (toca só a camada base HDR10) e perfil 5 (cores distorcidas), AV1, VC-1/MPEG-2, H.264 de 10 bits, bitrate médio
  acima de 50 Mbps e arquivo sem vídeo/áudio. Os de atenção reaparecem na janela de confirmação; nenhum bloqueia o envio.
- **Qualidade**: se o nome do arquivo não dizia, o app preenche com o que leu (ex.: `2160p, HDR`).
- **Arquivo único** sobe como vídeo com a duração e a largura/altura reais (e não só a duração do TMDB).
- Se o arquivo não puder ser lido, o app avisa na seção e segue normalmente.

## Otimizar para streaming

Um remux 4K tem ~55 Mbps de vídeo; o download do Telegram entrega ~5 MB/s (~40 Mbps) a uma conta comum e o filme
trava em buffer (medido: o F1 de 76 GB passou 52% do tempo travado). O botão **Otimizar para streaming…** (seção
MÍDIA) gera uma **cópia** mais leve antes de postar; o original nunca é alterado.

| Perfil | Vídeo | F1 (2h35) | VMAF vs original* | Pede de download |
|---|---|---|---|---|
| 4K · 18 Mbps (recomendado) | HEVC 10-bit | ~21 GiB | 99,4 | ~2,4 MB/s |
| 4K · 25 Mbps | HEVC 10-bit | ~29 GiB | 99,7 | ~3,3 MB/s |
| 4K · 12 Mbps | HEVC 10-bit | ~15 GiB | 98,9 | ~1,7 MB/s |
| 1080p · 8 Mbps | HEVC 10-bit | ~10 GiB | 95,3 numa TV 4K | ~1,2 MB/s |

\* no trecho mais pesado do F1 (62 Mbps de origem). O VMAF não é calibrado para HDR (PQ): vale a ordem, não o valor.

- **Como roda:** `ffmpeg` com `hevc_nvenc` (preset `p4`; o `p7` é 3,5× mais lento sem ganho). Numa RTX 3080: ~3,8× o
  tempo real em 4K (F1 ≈ 41 min) e ~9× em 1080p. Progresso, velocidade e tempo restante na barra; **Cancelar** apaga o parcial.
- **O que mantém:** HDR10 (mastering display e MaxCLL), áudio em **português + original** copiado sem recodificar
  (TrueHD/DTS viram EAC3 640k), legendas de texto pt (até 3) e en, e o índice do MKV **no começo** do arquivo
  (o app não precisa ler o fim antes de tocar).
- **O que descarta:** Dolby Vision (o app toca a camada base HDR10), áudios extras, legendas de imagem (PGS).
  Dolby Vision perfil 5 não pode ser otimizado (sem camada base compatível).
- **Depois:** a cópia vira o arquivo que é cortado e postado; ao concluir, é apagada (configurável). **Voltar ao
  original** desfaz antes de postar.
- **Requisitos:** placa NVIDIA com driver recente e `ffmpeg` com NVENC: `winget install Gyan.FFmpeg`. O app procura
  no PATH e no winget; ou aponte o `ffmpeg.exe` em Configurações. Precisa de espaço livre para a cópia (~25 GB por 4K).

## Velocidade do upload e progresso

O envio usa o Telethon com blocos de 512 KB (4 em voo). **O teto é do Telegram, por conta**: medido numa conta sem
Premium, com internet de 143 Mbps de upstream, o upload ficou em **~1,2 MiB/s no total**, igual com 4, 8, 16 e 32
blocos em voo e com 1, 2 ou 4 conexões em paralelo (sem esperas do Telegram). Mais concorrência não ajuda, então o
app não tenta; o que reduz o tempo é **enviar menos bytes** (ver *Otimizar para streaming*). Para dar uma noção:

| Arquivo | Upload a ~1,2 MiB/s |
|---|---|
| remux 4K de 76 GiB | ~18 h |
| 4K a 18 Mbps (~21 GiB) | ~5 h |
| 4K a 12 Mbps (~15 GiB) | ~3,5 h |
| 1080p a 8 Mbps (~10 GiB) | ~2,5 h |

O diálogo do "Otimizar" já mostra esse tempo por perfil. Se a conta for Premium o resultado pode ser outro (não medido).

Durante o envio a tela mostra: a parte atual (`Parte 8/12 — 412 MiB de 1,79 GiB`), o **total contando o que já subiu**
(também ao retomar), a **velocidade** (agora e média), **há quanto tempo está ativo**, quanto falta e o **horário
previsto de término**. Um relógio de 1 s mantém tudo vivo mesmo sem eventos: se passam 15 s sem nenhum byte novo,
aparece `⏳ sem progresso há 40 s` com o motivo (espera do Telegram, falha de rede). O log registra o tempo e a
velocidade de cada parte e, no fim, `Concluído em 5h12min (média 1,1 MB/s)`.

**Bloco perdido:** o Telegram só confere se todos os blocos chegaram ao final da parte. Se disser que faltou o
bloco *k* (`Part k of the file is missing`), o app relê só aquele bloco do arquivo original, reenvia com o mesmo
`file_id` e repete o envio da mensagem (até 8 vezes), em vez de refazer a parte inteira (~25 min). Validado com o
Telegram real: bloco 2 pulado de propósito → reparo → arquivo baixado de volta idêntico byte a byte.

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
