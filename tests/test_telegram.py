"""Cortador: envio ao Telegram (Telethon) com um cliente FALSO — nada de rede.

O mais importante: os blocos enviados, remontados pelo índice, têm de ser exatamente o intervalo
do arquivo original (um byte errado corrompe o filme em silêncio).
"""
import asyncio
import hashlib
import random
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from cortador import divisao
from cortador import telegram_envio as te
from cortador.jobs import ParteJob
from cortador.publicador import PublicacaoCancelada
from cortador.telegram_envio import EnvioCancelado, EnviadorTelethon

MIB = 1024 * 1024


def _arquivo(tmp_path, tamanho, nome="origem.mkv"):
    caminho = tmp_path / nome
    dados = random.Random(7).randbytes(tamanho)
    caminho.write_bytes(dados)
    return caminho, dados


class ClienteFalso:
    """Imita só o que o enviador usa do TelegramClient."""

    def __init__(self, falhas=None, atraso=0.0):
        self.blocos: dict[int, dict[int, bytes]] = {}   # upload_id -> {indice: bytes}
        self.pedidos: list = []
        self.enviados: list[tuple] = []
        self.apagadas: list = []
        self.textos: list[str] = []
        self._falhas = list(falhas or [])               # exceções a levantar, em ordem
        self._atraso = atraso
        self.em_voo = 0
        self.pico_em_voo = 0
        self._id = 500
        self.mensagens_existentes: set[int] = set()

    async def __call__(self, pedido):
        self.pedidos.append(pedido)
        if self._falhas:
            raise self._falhas.pop(0)
        self.em_voo += 1
        self.pico_em_voo = max(self.pico_em_voo, self.em_voo)
        try:
            if self._atraso:
                await asyncio.sleep(self._atraso)
            if isinstance(pedido, functions.upload.SaveBigFilePartRequest):
                self.blocos.setdefault(pedido.file_id, {})[pedido.file_part] = pedido.bytes
            elif isinstance(pedido, functions.upload.SaveFilePartRequest):
                self.blocos.setdefault(pedido.file_id, {})[pedido.file_part] = pedido.bytes
            return True
        finally:
            self.em_voo -= 1

    def remontar(self, upload_id: int) -> bytes:
        partes = self.blocos[upload_id]
        assert sorted(partes) == list(range(len(partes))), "faltou algum bloco"
        return b"".join(partes[i] for i in sorted(partes))

    async def send_file(self, entidade, media, caption=None):
        self._id += 1
        self.enviados.append((entidade, media, caption, self._id))
        return SimpleNamespace(id=self._id)

    async def send_message(self, entidade, texto, link_preview=True):
        self._id += 1
        self.textos.append(texto)
        return SimpleNamespace(id=self._id)

    async def delete_messages(self, entidade, ids):
        self.apagadas.extend(ids)

    async def get_messages(self, entidade, ids=None):
        return [SimpleNamespace(id=i) if i in self.mensagens_existentes else None for i in ids]


def _parte(offset, tamanho, nome="Filme.mkv.part01of02", indice=1, total=2):
    return ParteJob(indice, total, offset, tamanho, nome)


def _enviar(enviador, parte, caminho, legenda=None, como_video=False, duracao=None):
    avancos = []
    mid = asyncio.run(enviador.enviar_parte(parte, str(caminho), legenda, como_video,
                                            lambda f, t: avancos.append((f, t)), duracao))
    return mid, avancos


async def _sem_espera(segundos):
    return None


# --------------------------------------------------------------------------- integridade do upload

def test_blocos_remontados_sao_exatamente_o_intervalo_do_original(tmp_path):
    caminho, dados = _arquivo(tmp_path, 3 * MIB + 12_345)
    cliente = ClienteFalso()
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    _enviar(enviador, _parte(1000, 2 * MIB + 77), caminho)
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados[1000:1000 + 2 * MIB + 77]


def test_arquivo_pequeno_usa_o_fluxo_simples_com_md5(tmp_path):
    caminho, dados = _arquivo(tmp_path, 700_000)
    cliente = ClienteFalso()
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    _enviar(enviador, _parte(0, 700_000, "Filme.mkv", 1, 1), caminho, como_video=True)
    assert all(isinstance(p, functions.upload.SaveFilePartRequest) for p in cliente.pedidos)
    media = cliente.enviados[0][1]
    assert isinstance(media.file, types.InputFile)
    assert media.file.md5_checksum == hashlib.md5(dados[:700_000]).hexdigest()
    assert media.file.parts == 2  # 700.000 bytes em blocos de 512 KB


def test_arquivo_grande_usa_o_fluxo_de_arquivo_grande(tmp_path):
    caminho, dados = _arquivo(tmp_path, 11 * MIB)
    cliente = ClienteFalso()
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    _enviar(enviador, _parte(0, 11 * MIB), caminho)
    assert all(isinstance(p, functions.upload.SaveBigFilePartRequest) for p in cliente.pedidos)
    arquivo = cliente.enviados[0][1].file
    assert isinstance(arquivo, types.InputFileBig)
    assert arquivo.parts == 22 and all(p.file_total_parts == 22 for p in cliente.pedidos)
    assert cliente.remontar(arquivo.id) == dados


def test_blocos_tem_no_maximo_512kb_e_so_o_ultimo_e_menor(tmp_path):
    caminho, _ = _arquivo(tmp_path, 2 * MIB + 5)
    cliente = ClienteFalso()
    _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(0, 2 * MIB + 5), caminho)
    (upload_id,) = cliente.blocos
    tamanhos = [len(b) for _, b in sorted(cliente.blocos[upload_id].items())]
    assert tamanhos[:-1] == [te.TAMANHO_BLOCO] * (len(tamanhos) - 1)
    assert tamanhos[-1] == 5 and max(tamanhos) <= 512 * 1024


def test_varios_blocos_em_voo_mas_nunca_mais_que_o_limite(tmp_path):
    caminho, _ = _arquivo(tmp_path, 6 * MIB)
    cliente = ClienteFalso(atraso=0.005)
    enviador = EnviadorTelethon(cliente, "canal", simultaneos=4, dormir=_sem_espera)
    _enviar(enviador, _parte(0, 6 * MIB), caminho)
    assert 1 < cliente.pico_em_voo <= 4


def test_progresso_cresce_ate_o_total(tmp_path):
    caminho, _ = _arquivo(tmp_path, 3 * MIB)
    cliente = ClienteFalso()
    _, avancos = _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(0, 3 * MIB), caminho)
    assert avancos[-1] == (3 * MIB, 3 * MIB)
    assert all(t == 3 * MIB for _, t in avancos)
    assert [f for f, _ in avancos] == sorted(f for f, _ in avancos)


# --------------------------------------------------------------------------- o que vai na mensagem

def test_parte_de_filme_dividido_vai_como_documento_sem_tipo_de_video(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    cliente = ClienteFalso()
    mid, _ = _enviar(
        EnviadorTelethon(cliente, -100123, dormir=_sem_espera),
        _parte(0, 1 * MIB, "Filme (2020).mkv.part01of02"), caminho, legenda="Título: X\nTMDB: 5",
    )
    entidade, media, legenda, id_enviado = cliente.enviados[0]
    assert mid == id_enviado and entidade == -100123 and legenda == "Título: X\nTMDB: 5"
    assert media.force_file is True and media.mime_type == "application/octet-stream"
    assert [type(a).__name__ for a in media.attributes] == ["DocumentAttributeFilename"]
    assert media.attributes[0].file_name == "Filme (2020).mkv.part01of02"


def test_arquivo_unico_vai_como_video_com_atributo_e_streaming(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    cliente = ClienteFalso()
    _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera),
            _parte(0, 1 * MIB, "Filme (2020).mkv", 1, 1), caminho, como_video=True, duracao=9_960)
    media = cliente.enviados[0][1]
    video = next(a for a in media.attributes if isinstance(a, types.DocumentAttributeVideo))
    assert video.duration == 9_960 and video.supports_streaming is True
    assert media.mime_type == "video/x-matroska" and media.force_file is False


def test_mime_por_extensao():
    assert te.mime_do_nome("a.mkv") == "video/x-matroska"
    assert te.mime_do_nome("a.MP4") == "video/mp4"
    assert te.mime_do_nome("a.part01of02") == "application/octet-stream"


def test_texto_apagar_e_existem(tmp_path):
    cliente = ClienteFalso()
    cliente.mensagens_existentes = {501, 503}
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    assert asyncio.run(enviador.enviar_texto("Título: X")) > 0 and cliente.textos == ["Título: X"]
    assert asyncio.run(enviador.existem([501, 502, 503])) == {501, 503}
    assert asyncio.run(enviador.existem([])) == set()
    asyncio.run(enviador.apagar([501, 503]))
    assert cliente.apagadas == [501, 503]
    asyncio.run(enviador.apagar([]))
    assert cliente.apagadas == [501, 503]


# --------------------------------------------------------------------------- falhas e tentativas

def test_falha_passageira_num_bloco_e_repetida_sem_perder_os_outros(tmp_path):
    caminho, dados = _arquivo(tmp_path, 3 * MIB)
    cliente = ClienteFalso(falhas=[ConnectionError("rede"), TimeoutError()])
    espera = []

    async def dormir(s):
        espera.append(s)

    _enviar(EnviadorTelethon(cliente, "canal", dormir=dormir), _parte(0, 3 * MIB), caminho)
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados
    assert len(espera) == 2 and all(e > 0 for e in espera)


def test_flood_wait_espera_o_que_o_telegram_pediu(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    cliente = ClienteFalso(falhas=[errors.FloodWaitError(None, capture=7)])
    espera = []

    async def dormir(s):
        espera.append(s)

    _enviar(EnviadorTelethon(cliente, "canal", dormir=dormir), _parte(0, 1 * MIB), caminho)
    assert espera == [8]  # 7 s + 1 de folga


def test_desiste_depois_das_tentativas(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    cliente = ClienteFalso(falhas=[ConnectionError("x")] * 50)
    enviador = EnviadorTelethon(cliente, "canal", tentativas=3, dormir=_sem_espera)
    with pytest.raises(ConnectionError):
        _enviar(enviador, _parte(0, 1 * MIB), caminho)


def test_erro_definitivo_do_telegram_nao_e_repetido(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    cliente = ClienteFalso(falhas=[errors.RPCError(None, "FILE_PARTS_INVALID", 400)])
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    with pytest.raises(errors.RPCError):
        _enviar(enviador, _parte(0, 1 * MIB), caminho)
    assert len(cliente.pedidos) <= 2  # um bloco chegou a ser tentado e nenhuma repetição


def test_bloco_recusado_pelo_telegram_e_erro(tmp_path):
    class Recusa(ClienteFalso):
        async def __call__(self, pedido):
            return False

    caminho, _ = _arquivo(tmp_path, 1 * MIB)
    with pytest.raises(IOError, match="recusou"):
        _enviar(EnviadorTelethon(Recusa(), "canal", dormir=_sem_espera), _parte(0, 1 * MIB), caminho)


# --------------------------------------------------------------------------- cancelar

def test_cancelar_no_meio_do_upload_para_de_ler_e_de_enviar(tmp_path):
    caminho, _ = _arquivo(tmp_path, 8 * MIB)
    cliente = ClienteFalso()
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    contador = {"n": 0}

    def cancelado():
        contador["n"] += 1
        return contador["n"] > 5

    enviador.definir_cancelamento(cancelado)
    with pytest.raises(EnvioCancelado):
        _enviar(enviador, _parte(0, 8 * MIB), caminho)
    assert len(cliente.pedidos) < 16          # não subiu os 16 blocos
    assert cliente.enviados == []             # nunca chegou a publicar a mensagem


def test_cancelamento_do_envio_e_um_cancelamento_de_publicacao():
    assert issubclass(EnvioCancelado, PublicacaoCancelada)


# --------------------------------------------------------------------------- sessão e canal

class ClienteDeLogin:
    def __init__(self, exigir_senha=False, autorizado=False):
        self.eventos = []
        self._exigir_senha = exigir_senha
        self._autorizado = autorizado

    async def connect(self):
        self.eventos.append("connect")

    async def is_user_authorized(self):
        return self._autorizado

    async def send_code_request(self, telefone):
        self.eventos.append(("codigo_para", telefone))

    async def sign_in(self, telefone=None, codigo=None, password=None):
        if password is not None:
            self.eventos.append(("senha", password))
            return
        self.eventos.append(("entrou", telefone, codigo))
        if self._exigir_senha:
            raise errors.SessionPasswordNeededError(None)


def test_conectar_diz_se_a_sessao_ja_esta_autorizada():
    assert asyncio.run(te.conectar(ClienteDeLogin(autorizado=True))) is True
    assert asyncio.run(te.conectar(ClienteDeLogin(autorizado=False))) is False


def test_login_com_codigo():
    cliente = ClienteDeLogin()

    async def codigo():
        return "12345"

    async def senha():
        raise AssertionError("não devia pedir senha")

    asyncio.run(te.entrar(cliente, "+5511999990000", codigo, senha))
    assert cliente.eventos == [("codigo_para", "+5511999990000"), ("entrou", "+5511999990000", "12345")]


def test_login_com_senha_de_duas_etapas():
    cliente = ClienteDeLogin(exigir_senha=True)

    async def codigo():
        return "12345"

    async def senha():
        return "segredo"

    asyncio.run(te.entrar(cliente, "+5511999990000", codigo, senha))
    assert cliente.eventos[-1] == ("senha", "segredo")


def test_resolver_canal_carrega_os_dialogos_se_a_primeira_tentativa_falha():
    class Cliente:
        def __init__(self):
            self.carregou = False

        async def get_entity(self, canal):
            if not self.carregou:
                raise ValueError("Could not find the input entity")
            return SimpleNamespace(id=canal)

        async def get_dialogs(self):
            self.carregou = True

    cliente = Cliente()
    assert asyncio.run(te.resolver_canal(cliente, -100123)).id == -100123
    assert cliente.carregou


def test_criar_cliente_usa_o_arquivo_de_sessao_do_app(tmp_path):
    sessao = tmp_path / "dados" / "telegram.session"
    cliente = te.criar_cliente(12345, "hash", sessao)
    try:
        assert sessao.parent.is_dir()
        assert "monitor_canais" not in str(cliente.session.filename)
    finally:
        cliente.session.close()
