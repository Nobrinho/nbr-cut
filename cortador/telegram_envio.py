"""Envio direto ao canal pela conta do usuário (Telethon/MTProto).

Bots só sobem 50 MB; arquivos de ~2 GB exigem uma sessão de usuário. O upload é um laço próprio
(em vez de `client.upload_file`) por três motivos: lê a parte DIRETO do arquivo original (sem
gravar uma cópia dos 20 GB), mantém vários blocos em voo ao mesmo tempo (o upload sequencial do
Telethon é lento) e tenta de novo só o bloco que falhou, sem perder o que já subiu.

Tudo que toca a rede está atrás de `cliente` (um TelegramClient), então os testes usam um falso.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon import errors, functions, helpers, types

from cortador.divisao import LeitorDeIntervalo
from cortador.publicador import PublicacaoCancelada

TAMANHO_BLOCO = 512 * 1024          # máximo aceito pelo Telegram por bloco
LIMITE_ARQUIVO_GRANDE = 10 * 1024 * 1024  # acima disso o Telegram usa o fluxo de "arquivo grande"
SIMULTANEOS = 4                     # blocos em voo ao mesmo tempo
TENTATIVAS = 5
ESPERA_MAXIMA_S = 30
MAX_REPAROS = 8                     # blocos que o Telegram pode dizer que perdeu antes de desistirmos
INTERVALO_ENTRE_AVISOS_S = 10.0     # o mesmo aviso (4 blocos falham juntos) não se repete antes disto

Progresso = Callable[[int, int], None]

# Falhas passageiras de rede/servidor: vale tentar de novo. Qualquer outro erro do Telegram
# (parte inválida, sem permissão no canal…) é definitivo e sobe para a tela.
_TRANSITORIOS = (ConnectionError, TimeoutError, asyncio.TimeoutError) + tuple(
    getattr(errors, nome) for nome in ("ServerError", "RpcCallFailError", "TimedOutError", "RpcMcgetFailError")
    if hasattr(errors, nome)
)


class EnvioCancelado(PublicacaoCancelada):
    """O usuário cancelou durante o upload de uma parte (o publicador trata como cancelamento)."""


def mime_do_nome(nome: str) -> str:
    if nome.lower().endswith(".mkv"):
        return "video/x-matroska"
    return mimetypes.guess_type(nome)[0] or "application/octet-stream"


def montar_media(
    arquivo,
    nome: str,
    *,
    como_video: bool,
    duracao_s: int | None,
    largura: int = 0,
    altura: int = 0,
) -> types.InputMediaUploadedDocument:
    """Mídia do arquivo já subido. Parte de filme dividido = DOCUMENTO sem tipo de vídeo (o Telegram
    não deve tratar um pedaço de arquivo como vídeo). Arquivo único = VÍDEO com atributo de vídeo,
    senão o sincronismo do monitor (`mensagem.video`) o tiraria do registro."""
    atributos: list = [types.DocumentAttributeFilename(file_name=nome)]
    if como_video:
        atributos.insert(0, types.DocumentAttributeVideo(
            duration=int(duracao_s or 0), w=int(largura or 0), h=int(altura or 0), supports_streaming=True,
        ))
        return types.InputMediaUploadedDocument(
            file=arquivo, mime_type=mime_do_nome(nome), attributes=atributos, force_file=False,
        )
    return types.InputMediaUploadedDocument(
        file=arquivo, mime_type="application/octet-stream", attributes=atributos, force_file=True,
    )


def _propagar(tarefas) -> None:
    """Levanta a primeira falha entre [tarefas] depois de LER o resultado de todas — uma exceção
    de tarefa que ninguém lê vira aviso 'Task exception was never retrieved'."""
    primeira: BaseException | None = None
    for tarefa in tarefas:
        try:
            tarefa.result()
        except BaseException as erro:  # noqa: BLE001 — repassada logo abaixo
            primeira = primeira or erro
    if primeira is not None:
        raise primeira


@dataclass
class Subida:
    """Arquivo já enviado em blocos, mas ainda não anexado a uma mensagem.

    O Telegram só confere se TODOS os blocos chegaram no `send_file`; `reenviar(indice)` repõe um bloco que ele
    disse não ter (mesmo `file_id`), sem refazer as dezenas de minutos de upload da parte."""
    arquivo: types.InputFile | types.InputFileBig
    quantidade: int
    reenviar: Callable[[int], Awaitable[None]]


class EnviadorTelethon:
    def __init__(
        self,
        cliente,
        entidade,
        *,
        simultaneos: int = SIMULTANEOS,
        tentativas: int = TENTATIVAS,
        dormir: Callable[[float], Awaitable] = asyncio.sleep,
    ):
        self.cliente = cliente
        self.entidade = entidade
        self.simultaneos = max(1, simultaneos)
        self.tentativas = max(1, tentativas)
        self._dormir = dormir
        self._cancelado: Callable[[], bool] = lambda: False
        self._aviso: Callable[[str], None] = lambda texto: None
        self._ultimo_aviso: tuple[str, float] = ("", 0.0)

    def definir_cancelamento(self, funcao: Callable[[], bool]) -> None:
        self._cancelado = funcao

    def definir_aviso(self, funcao: Callable[[str], None]) -> None:
        """[funcao] recebe o que o usuário deve saber (espera imposta pelo Telegram, retry, bloco reenviado)."""
        self._aviso = funcao

    def _avisar(self, texto: str) -> None:
        agora = time.monotonic()
        ultimo, quando = self._ultimo_aviso
        if texto == ultimo and agora - quando < INTERVALO_ENTRE_AVISOS_S:
            return
        self._ultimo_aviso = (texto, agora)
        try:
            self._aviso(texto)
        except Exception:  # noqa: BLE001 — aviso nunca derruba o upload
            pass

    # ----------------------------------------------------------------------- upload

    async def enviar_parte(
        self,
        parte,
        caminho: str,
        legenda: str | None,
        como_video: bool,
        progresso: Progresso,
        duracao_s: int | None,
        largura: int = 0,
        altura: int = 0,
    ) -> int:
        subida = await self._subir(caminho, parte.offset, parte.tamanho, parte.nome, progresso)
        media = montar_media(subida.arquivo, parte.nome, como_video=como_video, duracao_s=duracao_s,
                             largura=largura, altura=altura)
        reparos = 0
        while True:
            try:
                mensagem = await self._com_retry(
                    lambda: self.cliente.send_file(self.entidade, media, caption=legenda)
                )
                return mensagem.id
            except errors.FilePartMissingError as erro:
                # "Part N of the file is missing": o Telegram não achou o bloco N. Repõe SÓ ele e repete.
                if reparos >= MAX_REPAROS or not 0 <= erro.which < subida.quantidade:
                    raise
                if self._cancelado():
                    raise EnvioCancelado() from erro
                reparos += 1
                self._avisar(f"O Telegram não recebeu o bloco {erro.which + 1}/{subida.quantidade} de {parte.nome}: "
                             f"reenviando só ele ({reparos}/{MAX_REPAROS})")
                await subida.reenviar(erro.which)

    async def _enviar_bloco(self, upload_id: int, indice: int, quantidade: int, dados: bytes, grande: bool,
                            nome: str) -> None:
        if grande:
            pedido = functions.upload.SaveBigFilePartRequest(upload_id, indice, quantidade, dados)
        else:
            pedido = functions.upload.SaveFilePartRequest(upload_id, indice, dados)
        ok = await self._com_retry(lambda: self.cliente(pedido))
        if not ok:
            raise IOError(f"O Telegram recusou o bloco {indice + 1}/{quantidade} de {nome}")

    async def _subir(self, caminho: str, offset: int, tamanho: int, nome: str, progresso: Progresso) -> Subida:
        """Sobe [offset, offset+tamanho) de [caminho] em blocos de 512 KB, vários em voo."""
        grande = tamanho > LIMITE_ARQUIVO_GRANDE
        quantidade = max(1, -(-tamanho // TAMANHO_BLOCO))
        upload_id = helpers.generate_random_long()
        md5 = hashlib.md5()  # só o fluxo de arquivo pequeno usa
        feito = 0

        async def enviar_bloco(indice: int, dados: bytes) -> None:
            nonlocal feito
            await self._enviar_bloco(upload_id, indice, quantidade, dados, grande, nome)
            feito += len(dados)
            progresso(feito, tamanho)

        pendentes: set[asyncio.Task] = set()
        try:
            with LeitorDeIntervalo(caminho, offset, tamanho) as leitor:
                for indice in range(quantidade):
                    if self._cancelado():
                        raise EnvioCancelado()
                    dados = await asyncio.to_thread(leitor.read, TAMANHO_BLOCO)
                    if not grande:
                        md5.update(dados)
                    while len(pendentes) >= self.simultaneos:
                        prontas, pendentes = await asyncio.wait(pendentes, return_when=asyncio.FIRST_COMPLETED)
                        _propagar(prontas)  # propaga a falha de um bloco
                    pendentes.add(asyncio.create_task(enviar_bloco(indice, dados)))
                if pendentes:
                    prontas, _ = await asyncio.wait(pendentes)
                    pendentes = set()
                    _propagar(prontas)
        except BaseException:
            for tarefa in pendentes:
                tarefa.cancel()
            if pendentes:
                await asyncio.gather(*pendentes, return_exceptions=True)
            raise

        async def reenviar(indice: int) -> None:
            with LeitorDeIntervalo(caminho, offset, tamanho) as leitor:
                leitor.seek(indice * TAMANHO_BLOCO)
                dados = await asyncio.to_thread(leitor.read, TAMANHO_BLOCO)
            if not dados:
                raise IOError(f"Bloco {indice + 1} fora do arquivo ({nome})")
            await self._enviar_bloco(upload_id, indice, quantidade, dados, grande, nome)

        if grande:
            arquivo = types.InputFileBig(id=upload_id, parts=quantidade, name=nome)
        else:
            arquivo = types.InputFile(id=upload_id, parts=quantidade, name=nome, md5_checksum=md5.hexdigest())
        return Subida(arquivo, quantidade, reenviar)

    # ----------------------------------------------------------------------- mensagens

    async def enviar_texto(self, texto: str) -> int:
        mensagem = await self._com_retry(
            lambda: self.cliente.send_message(self.entidade, texto, link_preview=False)
        )
        return mensagem.id

    async def apagar(self, ids: list[int]) -> None:
        if ids:
            await self._com_retry(lambda: self.cliente.delete_messages(self.entidade, ids))

    async def existem(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        mensagens = await self._com_retry(lambda: self.cliente.get_messages(self.entidade, ids=ids))
        if not isinstance(mensagens, (list, tuple)):
            mensagens = [mensagens]
        return {m.id for m in mensagens if m is not None and getattr(m, "id", None)}

    # ----------------------------------------------------------------------- tentativas

    async def _com_retry(self, fazer: Callable[[], Awaitable]):
        for tentativa in range(1, self.tentativas + 1):
            try:
                return await fazer()
            except errors.FloodWaitError as erro:
                if tentativa == self.tentativas:
                    raise
                espera = min(erro.seconds + 1, 600)  # o Telegram diz quanto esperar
                self._avisar(f"O Telegram pediu para esperar {erro.seconds} s (limite de pedidos); "
                             f"retoma sozinho ({tentativa}/{self.tentativas})")
                await self._dormir(espera)
            except _TRANSITORIOS as erro:
                if tentativa == self.tentativas:
                    raise
                espera = min(2 ** tentativa, ESPERA_MAXIMA_S)
                self._avisar(f"Falha de rede ({type(erro).__name__}); nova tentativa em {espera} s "
                             f"({tentativa}/{self.tentativas})")
                await self._dormir(espera)


# --------------------------------------------------------------------------- sessão e canal

def criar_cliente(api_id: int, api_hash: str, arquivo_sessao: str | os.PathLike):
    """Cliente com sessão PRÓPRIA do app (nunca a do monitor)."""
    from telethon import TelegramClient

    os.makedirs(os.path.dirname(os.fspath(arquivo_sessao)), exist_ok=True)
    return TelegramClient(os.fspath(arquivo_sessao), api_id, api_hash)


async def conectar(cliente) -> bool:
    """Conecta e diz se a sessão já está autorizada (senão é preciso entrar com telefone e código)."""
    await cliente.connect()
    return await cliente.is_user_authorized()


async def entrar(
    cliente,
    telefone: str,
    pedir_codigo: Callable[[], Awaitable[str]],
    pedir_senha: Callable[[], Awaitable[str]],
) -> None:
    """Login pela janela: telefone → código enviado pelo Telegram → senha de 2 etapas, se houver."""
    await cliente.send_code_request(telefone)
    codigo = await pedir_codigo()
    try:
        await cliente.sign_in(telefone, codigo)
    except errors.SessionPasswordNeededError:
        await cliente.sign_in(password=await pedir_senha())


async def resolver_canal(cliente, canal: int | str):
    """Entidade do canal. O id numérico só resolve depois que a conta "viu" o canal: se o
    primeiro get_entity falhar, carrega os diálogos uma vez e tenta de novo."""
    try:
        return await cliente.get_entity(canal)
    except ValueError:
        await cliente.get_dialogs()
        return await cliente.get_entity(canal)
