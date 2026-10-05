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


def test_atributo_de_video_leva_largura_e_altura_quando_conhecidas():
    media = te.montar_media(None, "Filme.mkv", como_video=True, duracao_s=9_318, largura=3840, altura=2160)
    video = next(a for a in media.attributes if isinstance(a, types.DocumentAttributeVideo))
    assert (video.duration, video.w, video.h) == (9_318, 3840, 2160)
    sem = te.montar_media(None, "Filme.mkv", como_video=True, duracao_s=None)
    video = next(a for a in sem.attributes if isinstance(a, types.DocumentAttributeVideo))
    assert (video.duration, video.w, video.h) == (0, 0, 0)


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


# ====================================================================== bloco perdido (FILE_PART_X_MISSING)

class ClienteQueConfere(ClienteFalso):
    """Imita o servidor: um bloco pode "sumir" no envio e o Telegram só reclama no send_file."""

    def __init__(self, perder=(), perder_sempre=(), **kw):
        super().__init__(**kw)
        self._perder = set(perder)                  # perdidos só na 1.ª vez
        self._perder_sempre = set(perder_sempre)    # perdidos toda vez (o reparo nunca resolve)
        self.chamadas_send_file = 0

    async def __call__(self, pedido):
        indice = getattr(pedido, "file_part", None)
        if indice in self._perder_sempre or indice in self._perder:
            self._perder.discard(indice)
            self.pedidos.append(pedido)
            return True                              # "ok" do Telegram, mas o bloco não foi guardado
        return await super().__call__(pedido)

    async def send_file(self, entidade, media, caption=None):
        self.chamadas_send_file += 1
        arquivo = media.file
        guardados = self.blocos.get(arquivo.id, {})
        faltando = [i for i in range(arquivo.parts) if i not in guardados]
        if faltando:
            raise errors.FilePartMissingError(None, capture=faltando[0])
        return await super().send_file(entidade, media, caption)


def _blocos_enviados(cliente, indice):
    return [p for p in cliente.pedidos if getattr(p, "file_part", None) == indice]


def test_bloco_perdido_e_reenviado_sem_refazer_a_parte(tmp_path):
    caminho, dados = _arquivo(tmp_path, 11 * MIB)           # >10 MiB = arquivo grande (22 blocos)
    cliente = ClienteQueConfere(perder={2})
    avisos = []
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    enviador.definir_aviso(avisos.append)
    mid, _ = _enviar(enviador, _parte(0, 11 * MIB, "Filme.mkv.part01of02"), caminho)
    assert mid == cliente.enviados[0][3] and len(cliente.enviados) == 1
    assert cliente.chamadas_send_file == 2                        # falhou uma vez, repetiu
    assert len(_blocos_enviados(cliente, 2)) == 2                 # o bloco 2 foi mandado 2 vezes...
    assert all(len(_blocos_enviados(cliente, i)) == 1 for i in range(22) if i != 2)   # ...e só ele
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados                   # arquivo íntegro, byte a byte
    assert len(avisos) == 1 and "bloco 3/22" in avisos[0] and "reenviando só ele" in avisos[0]


def test_varios_blocos_perdidos_sao_repostos_um_a_um(tmp_path):
    caminho, dados = _arquivo(tmp_path, 11 * MIB)
    cliente = ClienteQueConfere(perder={2, 7, 21})                 # 21 = o último (menor que 512 KiB não, mas o final)
    mid, _ = _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera),
                     _parte(0, 11 * MIB), caminho)
    assert cliente.chamadas_send_file == 4                         # 3 reparos + o envio final
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados


def test_ultimo_bloco_curto_e_reposto_com_o_tamanho_certo(tmp_path):
    tamanho = 10 * MIB + 100_000                                   # último bloco com 100 000 bytes
    caminho, dados = _arquivo(tmp_path, tamanho)
    quantidade = -(-tamanho // te.TAMANHO_BLOCO)
    cliente = ClienteQueConfere(perder={quantidade - 1})
    _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(0, tamanho), caminho)
    (upload_id,) = cliente.blocos
    assert len(cliente.blocos[upload_id][quantidade - 1]) == 100_000
    assert cliente.remontar(upload_id) == dados


def test_parte_no_meio_do_arquivo_repoe_o_bloco_do_intervalo_certo(tmp_path):
    caminho, dados = _arquivo(tmp_path, 30 * MIB)
    cliente = ClienteQueConfere(perder={5})
    _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(12 * MIB, 11 * MIB, indice=2), caminho)
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados[12 * MIB:23 * MIB]


def test_arquivo_pequeno_tambem_repoe_o_bloco(tmp_path):
    caminho, dados = _arquivo(tmp_path, 3 * te.TAMANHO_BLOCO + 10)  # < 10 MiB: SaveFilePart + md5
    cliente = ClienteQueConfere(perder={1})
    _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(0, len(dados)), caminho)
    (upload_id,) = cliente.blocos
    assert cliente.remontar(upload_id) == dados
    assert isinstance(_blocos_enviados(cliente, 1)[0], functions.upload.SaveFilePartRequest)


def test_desiste_depois_de_muitos_reparos_sem_efeito(tmp_path):
    caminho, _ = _arquivo(tmp_path, 11 * MIB)
    cliente = ClienteQueConfere(perder_sempre={2})                 # o servidor nunca guarda o bloco 2
    with pytest.raises(errors.FilePartMissingError):
        _enviar(EnviadorTelethon(cliente, "canal", dormir=_sem_espera), _parte(0, 11 * MIB), caminho)
    assert cliente.chamadas_send_file == te.MAX_REPAROS + 1
    assert cliente.enviados == []


def test_bloco_fora_da_faixa_nao_tenta_reparar(tmp_path):
    caminho, _ = _arquivo(tmp_path, 11 * MIB)

    class Maluco(ClienteFalso):
        async def send_file(self, entidade, media, caption=None):
            raise errors.FilePartMissingError(None, capture=999)

    with pytest.raises(errors.FilePartMissingError):
        _enviar(EnviadorTelethon(Maluco(), "canal", dormir=_sem_espera), _parte(0, 11 * MIB), caminho)


def test_cancelar_durante_o_reparo_interrompe(tmp_path):
    caminho, _ = _arquivo(tmp_path, 11 * MIB)
    cliente = ClienteQueConfere(perder={2})
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)
    original = cliente.send_file
    chamadas = []

    async def send_file(entidade, media, caption=None):
        chamadas.append(1)
        enviador.definir_cancelamento(lambda: True)                # o usuário cancela logo após a falha
        return await original(entidade, media, caption)

    cliente.send_file = send_file
    with pytest.raises(EnvioCancelado):
        _enviar(enviador, _parte(0, 11 * MIB), caminho)
    assert len(chamadas) == 1 and cliente.enviados == []


def test_outro_erro_do_telegram_nao_vira_reparo(tmp_path):
    caminho, _ = _arquivo(tmp_path, 11 * MIB)

    class SemPermissao(ClienteFalso):
        async def send_file(self, entidade, media, caption=None):
            raise errors.ChatWriteForbiddenError(None)

    with pytest.raises(errors.ChatWriteForbiddenError):
        _enviar(EnviadorTelethon(SemPermissao(), "canal", dormir=_sem_espera), _parte(0, 11 * MIB), caminho)


# ====================================================================== avisos de espera e retry

def test_flood_wait_e_retry_geram_avisos(tmp_path):
    caminho, _ = _arquivo(tmp_path, 600_000)
    cliente = ClienteFalso(falhas=[errors.FloodWaitError(None, capture=16), ConnectionError("caiu")])
    avisos = []
    esperas = []

    async def dormir(s):
        esperas.append(s)

    enviador = EnviadorTelethon(cliente, "canal", dormir=dormir, simultaneos=1)
    enviador.definir_aviso(avisos.append)
    _enviar(enviador, _parte(0, 600_000), caminho)
    assert esperas[0] == 17
    assert any("esperar 16 s" in a for a in avisos) and any("Falha de rede (ConnectionError)" in a for a in avisos)


def test_aviso_repetido_em_pouco_tempo_aparece_uma_vez(monkeypatch):
    enviador = EnviadorTelethon(ClienteFalso(), "canal")
    avisos = []
    enviador.definir_aviso(avisos.append)
    relogio = [100.0]
    monkeypatch.setattr(te.time, "monotonic", lambda: relogio[0])
    enviador._avisar("mesmo texto")
    enviador._avisar("mesmo texto")                                # 4 blocos falham juntos: 1 aviso só
    enviador._avisar("outro texto")
    relogio[0] += te.INTERVALO_ENTRE_AVISOS_S + 1
    enviador._avisar("mesmo texto")                                # passou o intervalo: avisa de novo
    assert avisos == ["mesmo texto", "outro texto", "mesmo texto"]


def test_aviso_que_quebra_nao_derruba_o_upload(tmp_path):
    caminho, _ = _arquivo(tmp_path, 600_000)
    cliente = ClienteFalso(falhas=[ConnectionError("caiu")])
    enviador = EnviadorTelethon(cliente, "canal", dormir=_sem_espera)

    def estoura(texto):
        raise RuntimeError("tela fechada")

    enviador.definir_aviso(estoura)
    mid, _ = _enviar(enviador, _parte(0, 600_000), caminho)
    assert mid > 0
