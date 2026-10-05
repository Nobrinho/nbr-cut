"""Cortador: a janela principal, com serviço falso (sem Telegram, sem banco, sem diálogos reais).

Cria uma janela Tk de verdade (escondida). Pula sozinho onde não há ambiente gráfico.
"""
import os
import threading
import time
from pathlib import Path

import pytest

ctk = pytest.importorskip("customtkinter")

from cortador import config, execucao
from cortador.fila import CANCELADO, CONCLUIDO, ERRO, NOVO, OTIMIZANDO, PRONTO, ItemFila
from cortador.publicador import Evento, PublicacaoCancelada
from cortador.registro import JaPublicado

DADOS = {
    "tmdb_id": 693134, "titulo": "Duna: Parte 2", "titulo_original": "Dune: Part Two", "ano": "2024",
    "duracao": 166, "generos": ["Ficção científica"], "elenco": [("Zendaya", None)],
    "sinopse": "Resumo.", "poster_url": None,
}


class Mensagens:
    """Substitui tkinter.messagebox: guarda o que seria mostrado (um diálogo real travaria o teste)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.infos, self.erros, self.avisos, self.perguntas = [], [], [], []
        self.resposta_sim = False

    def showinfo(self, titulo, texto, **kw):
        self.infos.append((titulo, texto))

    def showerror(self, titulo, texto, **kw):
        self.erros.append((titulo, texto))

    def showwarning(self, titulo, texto, **kw):
        self.avisos.append((titulo, texto))

    def askyesno(self, titulo, texto, **kw):
        self.perguntas.append((titulo, texto))
        return self.resposta_sim


@pytest.fixture(scope="module")
def _janela(tmp_path_factory):
    """UMA janela para o módulo todo: criar e destruir várias raízes Tk no mesmo processo é instável."""
    from cortador.ui import app as app_mod

    mp = pytest.MonkeyPatch()
    mp.setattr(app_mod.Aplicativo, "_iniciar", lambda self: None)  # nada de rede/banco
    pasta = tmp_path_factory.mktemp("cortador_ui")
    cfg = config.Configuracao(
        api_id=1, api_hash="x", tmdb_key="k", canal_destino="-100PROD",
        tamanho_parte=3_000, pasta_jobs=pasta / "jobs",
    )
    try:
        janela = app_mod.Aplicativo(cfg)
    except Exception as erro:  # sem display (CI)
        mp.undo()
        pytest.skip(f"sem ambiente gráfico: {erro}")
    janela.withdraw()
    mensagens = Mensagens()
    mp.setattr(app_mod, "messagebox", mensagens)
    janela.mensagens = mensagens
    janela._postar_original = janela.servico.postar
    janela._descartar_original = janela.servico.descartar
    janela.update()
    yield janela
    try:
        janela._encerrando = True
        janela.loop.parar()
        janela.pool.shutdown(wait=False, cancel_futures=True)
        janela.destroy()
    except Exception:
        pass
    import gc
    gc.enable()  # o app desliga o GC automático (ver Aplicativo.__init__)
    mp.undo()


@pytest.fixture
def app(_janela, tmp_path):
    """A janela compartilhada, com o estado zerado a cada teste."""
    j = _janela
    while not j._fila_ui.empty():  # sobra de callbacks do teste anterior
        j._fila_ui.get_nowait()
    j.itens, j.sel, j.postando, j._fila_envio, j._job_atual = [], None, None, [], None
    j._progresso_fixo = j._trocando = False
    j._telegram_ok = j._banco_ok = True
    j.cancelamento.zerar()
    j.cfg.tamanho_parte, j.cfg.modo_teste = 3_000, False
    j.servico.postar, j.servico.descartar = j._postar_original, j._descartar_original
    j.servico.repositorio.pasta = tmp_path / "jobs"
    j._descartados = []
    j.mensagens.reset()
    j.caixa_log.configure(state="normal")
    j.caixa_log.delete("1.0", "end")
    j.caixa_log.configure(state="disabled")
    j._atualizar_fila()
    j._atualizar_detalhe()
    j.update()
    return j


@pytest.fixture
def filme(tmp_path):
    caminho = tmp_path / "Duna.Parte.2.2024.2160p.WEB-DL.DUAL.mkv"
    caminho.write_bytes(os.urandom(10_000))
    return str(caminho)


def _pronto(app, filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    app.itens = [item]
    app.sel = item
    app._atualizar_fila()
    app._atualizar_detalhe()
    return item


def bombear(app, condicao, limite=6.0):
    """Roda o loop do Tk até [condicao] ou estourar o tempo (os callbacks chegam por after())."""
    fim = time.time() + limite
    while time.time() < fim:
        app.update()
        if condicao():
            return True
        time.sleep(0.01)
    app.update()
    return condicao()


def _servico_falso(app, comportamento):
    async def postar(job, emitir, cancelado):
        return await comportamento(job, emitir, cancelado)

    async def descartar(job):
        app._descartados.append(job.id)

    app.servico.postar = postar
    app.servico.descartar = descartar


async def _sucesso(job, emitir, cancelado):
    emitir(Evento("inicio", total_partes=job.total_partes, mensagem=job.nome_base))
    for parte in sorted(job.partes, key=lambda p: -p.indice):
        emitir(Evento("parte_inicio", parte=parte.indice, total_partes=job.total_partes, total=parte.tamanho,
                      mensagem=parte.nome))
        emitir(Evento("progresso", parte=parte.indice, total_partes=job.total_partes,
                      feito=parte.tamanho // 2, total=parte.tamanho))
        emitir(Evento("progresso", parte=parte.indice, total_partes=job.total_partes,
                      feito=parte.tamanho, total=parte.tamanho))
        emitir(Evento("parte_ok", parte=parte.indice, total_partes=job.total_partes, mensagem=parte.nome,
                      dados={"message_id": 100 + parte.indice}))
    emitir(Evento("registrado", mensagem="tmdb:693134"))
    emitir(Evento("indice", mensagem="enfileirado"))
    emitir(Evento("concluido", mensagem="tmdb:693134"))
    return "tmdb:693134"


# --------------------------------------------------------------------------- estado inicial

def test_janela_sobe_vazia_e_sem_barra_de_acao(app):
    app._atualizar_detalhe()
    assert app.sel is None and app.itens == []
    assert not app.painel_acao.winfo_ismapped()


def test_item_pronto_mostra_nome_plano_e_legenda(app, filme):
    item = _pronto(app, filme)
    app.update()
    assert app.var_nome.get() == "Duna - Parte 2 (2024) [2160p WEB-DL].mkv"
    assert "4 partes" in app.lbl_plano.cget("text") or "partes" in app.lbl_plano.cget("text")
    assert "Título: Duna: Parte 2" in app.texto_legenda.get("1.0", "end")
    assert app.botao_postar.cget("state") == "normal"
    assert app.botao_cortar.cget("state") == "normal"
    assert item.estado == PRONTO


def _info_dv7():
    from cortador import midia
    video = midia.Video("HEVC", 3840, 2160, 23.976, 10, None, 7, 6, True)
    return midia.InfoMidia("mkv", 9_318.3, video, [midia.Audio("TrueHD", 8, "en"), midia.Audio("AC3", 6, "pt")], 2)


class _ConfirmacaoFalsa:
    """Troca o DialogoConfirmar para capturar os avisos que seriam mostrados."""
    capturado: dict = {}

    def __init__(self, pai, linhas, destino, teste, ao_confirmar, avisos=None):
        type(self).capturado = {"linhas": linhas, "avisos": avisos}


def test_secao_midia_mostra_formato_audios_e_avisos(app, filme):
    item = _pronto(app, filme)
    app.update()
    assert "Lendo metadados" in app.lbl_midia_video.cget("text")
    item.definir_midia(_info_dv7())
    app._atualizar_detalhe()
    assert "HEVC 10-bit" in app.lbl_midia_video.cget("text") and "Dolby Vision P7" in app.lbl_midia_video.cget("text")
    assert "TrueHD 7.1 (en)" in app.lbl_midia_audio.cget("text")
    assert "perfil 7" in app.lbl_midia_avisos.cget("text") and app.lbl_midia_avisos.cget("text").startswith("\u26a0")


def test_secao_midia_quando_nao_deu_para_ler(app, filme):
    item = _pronto(app, filme)
    item.midia_erro = "formato nao reconhecido"
    app._atualizar_detalhe()
    assert "Não deu para ler" in app.lbl_midia_video.cget("text") and app.lbl_midia_avisos.cget("text") == ""


def test_leitura_em_segundo_plano_preenche_o_item_e_loga_o_aviso(app, filme, monkeypatch):
    from cortador import midia
    monkeypatch.setattr(midia, "ler", lambda caminho: _info_dv7())
    item = _pronto(app, filme)
    app._ler_midia(item)
    assert bombear(app, lambda: item.midia is not None)
    app.update()
    assert "Dolby Vision P7" in app.lbl_midia_video.cget("text")
    assert "aviso(s) de formato" in app.caixa_log.get("1.0", "end")


def test_leitura_que_falha_vira_mensagem_e_nao_trava(app, filme, monkeypatch):
    from cortador import midia

    def ler(caminho):
        raise midia.MidiaIlegivel("formato nao reconhecido")
    monkeypatch.setattr(midia, "ler", ler)
    item = _pronto(app, filme)
    app._ler_midia(item)
    assert bombear(app, lambda: bool(item.midia_erro))
    app.update()
    assert "Não deu para ler" in app.lbl_midia_video.cget("text")
    assert app.botao_postar.cget("state") == "normal"      # metadados nao impedem postar


def test_confirmacao_lista_os_avisos_de_formato(app, filme, monkeypatch):
    from cortador.ui import app as app_mod
    item = _pronto(app, filme)
    item.definir_midia(_info_dv7())
    monkeypatch.setattr(app_mod, "DialogoConfirmar", _ConfirmacaoFalsa)
    app._postar_selecionado()
    avisos = _ConfirmacaoFalsa.capturado["avisos"]
    assert len(avisos) == 1 and "perfil 7" in avisos[0]


def test_confirmacao_sem_aviso_nao_traz_lista(app, filme, monkeypatch):
    from cortador.ui import app as app_mod
    _pronto(app, filme)
    monkeypatch.setattr(app_mod, "DialogoConfirmar", _ConfirmacaoFalsa)
    app._postar_selecionado()
    assert _ConfirmacaoFalsa.capturado["avisos"] == []


def test_postar_todos_resume_os_filmes_com_aviso(app, filme, tmp_path, monkeypatch):
    from cortador.ui import app as app_mod
    a = _pronto(app, filme)
    a.definir_midia(_info_dv7())
    monkeypatch.setattr(app_mod, "DialogoConfirmar", _ConfirmacaoFalsa)
    app._postar_todos()
    (aviso,) = _ConfirmacaoFalsa.capturado["avisos"]
    assert aviso.startswith("1 filme(s) com aviso de formato")


def test_dialogo_de_confirmacao_mostra_os_avisos(app):
    from cortador.ui.dialogos import DialogoConfirmar
    d = DialogoConfirmar(app, [("Filme", "X")], "-100123", True, lambda: None, avisos=["Dolby Vision perfil 7 ..."])
    app.update()
    textos = []

    def varrer(w):
        for f in w.winfo_children():
            try:
                textos.append(f.cget("text"))
            except Exception:  # noqa: BLE001
                pass
            varrer(f)
    varrer(d)
    d.destroy()
    assert any("Dolby Vision perfil 7" in t for t in textos)


def test_sem_conexao_com_o_telegram_nao_deixa_postar(app, filme):
    _pronto(app, filme)
    app._telegram_ok = False
    app._atualizar_detalhe()
    assert app.botao_postar.cget("state") == "disabled"


def test_filme_ja_publicado_bloqueia_o_botao_e_mostra_o_aviso(app, filme):
    item = _pronto(app, filme)
    item.duplicado = {"publicado_em": "2026-10-01T10:00:00"}
    app._atualizar_detalhe()
    assert app.botao_postar.cget("state") == "disabled"
    assert "já foi publicado" in app.lbl_aviso.cget("text")


def test_editar_o_nome_a_mao_e_preservado(app, filme):
    item = _pronto(app, filme)
    app.var_nome.set("Meu Filme.mkv")
    app._nome_editado()
    assert item.nome_final == "Meu Filme.mkv" and item.nome_editado
    app.var_qualidade.set("720p")
    app._qualidade_editada()
    assert item.nome_final == "Meu Filme.mkv"  # a edição manual não é sobrescrita


def test_trocar_a_qualidade_atualiza_o_nome_automatico(app, filme):
    item = _pronto(app, filme)
    app.var_qualidade.set("1080p, BluRay")
    app._qualidade_editada()
    assert item.nome_final == "Duna - Parte 2 (2024) [1080p BluRay].mkv"


# --------------------------------------------------------------------------- postagem

def test_postagem_com_sucesso_atualiza_progresso_estado_e_avisa(app, filme):
    item = _pronto(app, filme)
    _servico_falso(app, _sucesso)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == CONCLUIDO)
    assert app.postando is None and item.job_id is None
    assert app.barra_total.get() == 1
    assert "postado e registrado" in app.lbl_total.cget("text")
    assert app.mensagens.infos and "Postado no canal" in app.mensagens.infos[0][1]
    log = app.caixa_log.get("1.0", "end")
    assert "parte 4/4 no canal" in log and "registrado no bot" in log and "pedido de atualização do índice" in log
    # o resumo fica na tela até trocar de item
    assert app.area_progresso.winfo_manager() == "pack"
    app._selecionar(item)
    assert app.area_progresso.winfo_manager() == ""


def test_progresso_total_considera_so_o_que_falta_enviar(app, filme):
    item = _pronto(app, filme)
    job = app.servico.novo_job(item)
    job.partes[3].message_id = 99  # parte 4 já estava no canal (retomada)
    pendente = sum(p.tamanho for p in job.partes if not p.message_id)
    visto = []

    async def comportamento(j, emitir, cancelado):
        emitir(Evento("parte_inicio", parte=3, total_partes=4, total=j.partes[2].tamanho, mensagem="x"))
        emitir(Evento("progresso", parte=3, total_partes=4, feito=j.partes[2].tamanho, total=j.partes[2].tamanho))
        emitir(Evento("parte_ok", parte=3, total_partes=4, mensagem="x", dados={"message_id": 1}))
        return "tmdb:693134"

    _servico_falso(app, comportamento)
    app._rodar_job(item, job)
    assert bombear(app, lambda: item.estado == CONCLUIDO)
    assert app._bytes_total == pendente
    assert app._bytes_concluidos == job.partes[2].tamanho


def test_falha_no_envio_deixa_o_item_retomavel(app, filme):
    item = _pronto(app, filme)

    async def falha(job, emitir, cancelado):
        raise ConnectionError("rede caiu")

    _servico_falso(app, falha)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == ERRO)
    assert item.job_id is not None and "rede caiu" in item.mensagem
    assert app.postando is None
    assert app.mensagens.erros and "rede caiu" in app.mensagens.erros[0][1]
    app._atualizar_detalhe()
    assert app.botao_postar.cget("text") == "Retomar envio…"


def test_retomar_reaproveita_o_job_salvo(app, filme):
    item = _pronto(app, filme)
    job = app.servico.novo_job(item)
    job.partes[3].message_id = 42
    app.servico.repositorio.salvar(job)
    item.job_id, item.estado = job.id, ERRO
    recebido = []

    async def captura(j, emitir, cancelado):
        recebido.append(j)
        return "tmdb:693134"

    _servico_falso(app, captura)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == CONCLUIDO)
    assert recebido[0].id == job.id and recebido[0].partes[3].message_id == 42


def test_cancelar_pergunta_se_apaga_o_que_ja_subiu(app, filme):
    item = _pronto(app, filme)
    app.mensagens.resposta_sim = True

    async def cancelado(job, emitir, cancelar):
        job.partes[3].message_id = 7  # uma parte já está no canal
        raise PublicacaoCancelada()

    _servico_falso(app, cancelado)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == PRONTO and app._descartados)
    assert app.mensagens.perguntas and "Apagar o que já foi enviado" in app.mensagens.perguntas[0][1]
    assert len(app._descartados) == 1


def test_cancelar_e_guardar_deixa_o_item_cancelado_e_retomavel(app, filme):
    item = _pronto(app, filme)
    app.mensagens.resposta_sim = False

    async def cancelado(job, emitir, cancelar):
        job.partes[3].message_id = 7
        raise PublicacaoCancelada()

    _servico_falso(app, cancelado)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == CANCELADO)
    assert "retomar" in item.mensagem and not app._descartados


def test_duplicidade_descoberta_na_hora_nao_mexe_no_canal(app, filme):
    item = _pronto(app, filme)

    async def duplicado(job, emitir, cancelado):
        raise JaPublicado("tmdb:693134", {"publicado_em": "2026-10-01T00:00:00"})

    _servico_falso(app, duplicado)
    app._iniciar_postagem([item])
    assert bombear(app, lambda: item.estado == NOVO and item.duplicado)
    assert "já publicado" in item.mensagem


def test_postar_todos_envia_um_de_cada_vez_na_ordem(app, tmp_path):
    itens = []
    for nome in ("A.2020.mkv", "B.2021.mkv"):
        caminho = tmp_path / nome
        caminho.write_bytes(os.urandom(5_000))
        item = ItemFila.criar(str(caminho))
        item.escolher_filme({**DADOS, "tmdb_id": 100 + len(itens), "titulo": nome[0]})
        itens.append(item)
    app.itens = itens
    app.sel = itens[0]
    ordem = []

    async def comportamento(job, emitir, cancelado):
        ordem.append(job.nome_base)
        await _sucesso(job, emitir, cancelado)
        return f"tmdb:{job.tmdb_id}"

    _servico_falso(app, comportamento)
    app._iniciar_postagem(list(itens))
    assert bombear(app, lambda: all(i.estado == CONCLUIDO for i in itens))
    assert len(ordem) == 2 and ordem[0].startswith("A") and ordem[1].startswith("B")


def test_nao_dispara_dois_envios_ao_mesmo_tempo(app, filme):
    item = _pronto(app, filme)
    app.postando = object()  # já tem um envio rodando
    assert any("andamento" in p for p in app._problemas_para_postar(item))


# --------------------------------------------------------------------------- só cortar

def test_so_cortar_grava_as_partes_na_pasta_escolhida(app, filme, tmp_path, monkeypatch):
    from cortador.ui import app as app_mod

    item = _pronto(app, filme)
    destino = tmp_path / "saida"
    destino.mkdir()
    monkeypatch.setattr(app_mod.filedialog, "askdirectory", lambda **kw: str(destino))
    app._so_cortar()
    pasta = destino / (item.nome_final + ".partes")
    assert bombear(app, lambda: (pasta / (item.nome_final + ".manifest.json")).exists() and app.mensagens.infos)
    partes = sorted(p.name for p in pasta.iterdir() if ".part" in p.name and not p.name.endswith(".json"))
    assert len(partes) == 4 and partes[0].endswith(".part01of04")
    assert app.mensagens.infos and "4 partes gravadas" in app.mensagens.infos[0][1]


def test_so_cortar_recusa_arquivo_que_cabe_numa_parte(app, filme):
    item = _pronto(app, filme)
    app.cfg.tamanho_parte = 1_000_000
    app._atualizar_detalhe()
    assert app.botao_cortar.cget("state") == "disabled"
    app._so_cortar()
    assert app.mensagens.infos and "não precisa cortar" in app.mensagens.infos[0][1]


# --------------------------------------------------------------------------- pendentes

def test_retomar_job_antigo_cria_o_item_e_roda(app, filme):
    item = _pronto(app, filme)
    job = app.servico.novo_job(item)
    job.partes[3].message_id = 5
    app.itens = []
    app.sel = None
    vistos = []

    async def captura(j, emitir, cancelado):
        vistos.append(j)
        return "tmdb:693134"

    _servico_falso(app, captura)
    app._retomar_job(job)
    assert bombear(app, lambda: vistos)
    assert len(app.itens) == 1 and app.itens[0].nome_final == job.nome_base


def test_retomar_sem_telegram_avisa(app, filme):
    item = _pronto(app, filme)
    job = app.servico.novo_job(item)
    app._telegram_ok = False
    app._retomar_job(job)
    assert app.mensagens.avisos and "Conecte" in app.mensagens.avisos[0][1]


def test_retomar_com_arquivo_alterado_nao_deixa(app, filme):
    item = _pronto(app, filme)
    job = app.servico.novo_job(item)
    Path(filme).write_bytes(b"outro conteudo")
    app._retomar_job(job)
    assert app.mensagens.erros and "mudou" in app.mensagens.erros[0][1]


def test_formatadores():
    from cortador.ui.app import formatar_tempo, formatar_velocidade

    assert formatar_tempo(45) == "45s" and formatar_tempo(125) == "2min05s" and formatar_tempo(3725) == "1h02min"
    assert formatar_velocidade(6.5 * 1024 * 1024) == "6.5 MB/s"


# ====================================================================== otimizar para streaming

class _Falsos:
    """ffmpeg/placa falsos: o que a tela pede ao núcleo, sem rodar nada de verdade."""

    def __init__(self, monkeypatch, tmp_path, app):
        from cortador import otimizar
        self.otimizar = otimizar
        self.tmp = tmp_path
        self.app = app
        self.nvenc = True
        self.chamadas = []
        self.liberar = threading.Event()
        self.falhar = None
        self.ff = otimizar.Ferramentas("ffmpeg.exe", "ffprobe.exe")
        trilhas = [otimizar.Trilha(0, "video", "hevc", largura=3840, altura=2160, bits=10),
                   otimizar.Trilha(1, "audio", "eac3", 6, "en"), otimizar.Trilha(2, "audio", "ac3", 6, "pt")]
        monkeypatch.setattr(otimizar, "localizar_ffmpeg", lambda configurado="": self.ff)
        monkeypatch.setattr(otimizar, "nvenc_funciona", lambda ff, timeout=30.0: self.nvenc)
        monkeypatch.setattr(otimizar, "listar_trilhas", lambda ff, caminho: trilhas)
        monkeypatch.setattr(otimizar, "espaco_livre", lambda pasta: 500 * 1024 ** 3)
        monkeypatch.setattr(otimizar, "executar", self.executar)

    def executar(self, comando, destino, duracao_s, progresso, cancelado):
        self.chamadas.append((comando, str(destino), duracao_s))
        progresso(self.otimizar.Progresso(0.25, 90.0, 3.8, 1_000, 60.0))
        while not self.liberar.is_set():
            if cancelado():
                raise self.otimizar.OtimizacaoCancelada()
            time.sleep(0.01)
        if self.falhar:
            raise self.otimizar.ErroOtimizacao(self.falhar)
        Path(destino).parent.mkdir(parents=True, exist_ok=True)      # o executar real também cria a pasta
        Path(destino).write_bytes(b"\0" * 3_000)
        progresso(self.otimizar.Progresso(1.0, tamanho_bytes=3_000, restante_s=0.0))
        return Path(destino)


@pytest.fixture
def fals(app, monkeypatch, tmp_path):
    f = _Falsos(monkeypatch, tmp_path, app)
    app.cfg.pasta_otimizados = str(tmp_path / "otim")
    app.cfg.apagar_otimizado = True
    app.mensagens.resposta_sim = True
    yield f
    f.liberar.set()
    app.cancel_otim.cancelar()
    app.otimizando = None
    app.cfg.pasta_otimizados = ""


def _item_com_midia(app, filme):
    item = _pronto(app, filme)
    item.definir_midia(_info_dv7())
    app._atualizar_detalhe()
    app.update()
    return item


class _DialogoFalso:
    ultimo = None

    def __init__(self, pai, cabecalho, opcoes, padrao, avisos, ao_confirmar):
        type(self).ultimo = self
        self.cabecalho, self.opcoes, self.padrao, self.avisos, self.ao_confirmar = cabecalho, opcoes, padrao, avisos, ao_confirmar


def _abrir_dialogo(app, monkeypatch):
    from cortador.ui import app as app_mod
    monkeypatch.setattr(app_mod, "DialogoOtimizar", _DialogoFalso)
    _DialogoFalso.ultimo = None
    app._abrir_otimizacao()
    assert bombear(app, lambda: _DialogoFalso.ultimo is not None)
    return _DialogoFalso.ultimo


def test_botao_otimizar_fica_liberado_com_midia_lida(app, filme, fals):
    item = _pronto(app, filme)
    assert app.botao_otimizar.cget("state") == "disabled"          # ainda sem metadados
    item.definir_midia(_info_dv7())
    app._atualizar_detalhe()
    assert app.botao_otimizar.cget("state") == "normal"
    assert "Bitrate" not in app.lbl_otim.cget("text")              # 10 KB / 2h35: nada de bitrate alto


def test_dica_de_bitrate_alto_na_secao(app, filme, fals):
    item = _item_com_midia(app, filme)
    item.midia.duracao_s = 0.001          # 10 KB em 1 ms = bitrate alto, sem inflar o plano de partes
    app._atualizar_detalhe()
    assert "otimizar evita travar" in app.lbl_otim.cget("text")


def test_dolby_vision_5_nao_deixa_otimizar_e_diz_porque(app, filme, fals):
    item = _pronto(app, filme)
    info = _info_dv7()
    info.video.dv_perfil = 5
    item.definir_midia(info)
    app._atualizar_detalhe()
    assert app.botao_otimizar.cget("state") == "disabled" and "perfil 5" in app.lbl_otim.cget("text")


def test_sem_ffmpeg_mostra_como_instalar(app, filme, fals, monkeypatch):
    _item_com_midia(app, filme)
    monkeypatch.setattr(fals.otimizar, "localizar_ffmpeg", lambda configurado="": None)
    app._abrir_otimizacao()
    assert "winget install Gyan.FFmpeg" in app.mensagens.avisos[-1][1]


def test_sem_placa_nvidia_explica(app, filme, fals):
    _item_com_midia(app, filme)
    fals.nvenc = False
    app._abrir_otimizacao()
    assert bombear(app, lambda: bool(app.mensagens.avisos))
    assert "NVENC" in app.mensagens.avisos[-1][1]


def test_dialogo_traz_um_perfil_por_opcao_com_estimativas(app, filme, fals, monkeypatch):
    _item_com_midia(app, filme)
    d = _abrir_dialogo(app, monkeypatch)
    assert [o[0] for o in d.opcoes] == ["4k18", "4k25", "4k12", "1080p8"] and d.padrao == "4k18"
    assert "MB/s" in d.opcoes[0][2] and "RTX 3080" in d.opcoes[0][2]
    assert "Áudio: pt AC3 5.1, en EAC3 5.1" in d.cabecalho[1]


def test_perfil_padrao_vem_da_configuracao(app, filme, fals, monkeypatch):
    _item_com_midia(app, filme)
    app.cfg.perfil_otimizacao = "1080p8"
    assert _abrir_dialogo(app, monkeypatch).padrao == "1080p8"
    app.cfg.perfil_otimizacao = "4k18"


def test_otimizacao_completa_troca_o_arquivo_e_le_os_metadados_de_novo(app, filme, fals, monkeypatch):
    item = _item_com_midia(app, filme)
    d = _abrir_dialogo(app, monkeypatch)
    monkeypatch.setattr(fals.otimizar.midia, "ler", lambda caminho: _info_dv7())
    d.ao_confirmar("4k18")
    assert item.estado == OTIMIZANDO and app.otimizando is item
    assert bombear(app, lambda: bool(fals.chamadas))
    comando, destino, duracao = fals.chamadas[0]
    assert destino.endswith("filme.4k18.mkv".replace("filme", Path(filme).stem)) and duracao == pytest.approx(9_318.3)
    assert "hevc_nvenc" in comando and comando[comando.index("-b:v") + 1] == "18M"
    assert bombear(app, lambda: "25%" in app.lbl_total.cget("text"))
    assert "3.8× o tempo real" in app.lbl_total.cget("text") and "faltam" in app.lbl_total.cget("text")
    assert app.botao_cancelar.cget("state") == "normal" and app.botao_postar.cget("state") == "disabled"
    assert app.botao_otimizar.cget("state") == "disabled"
    fals.liberar.set()
    assert bombear(app, lambda: item.otimizado)
    app.update()
    assert app.otimizando is None and item.estado == PRONTO and item.tamanho == 3_000
    assert Path(item.caminho).is_file() and item.caminho_original == filme
    assert bombear(app, lambda: item.midia is not None)
    app.update()
    assert "Cópia otimizada" in app.lbl_otim.cget("text") and app.botao_original.winfo_ismapped()
    assert "Otimizado: 9.8 KB" in app.caixa_log.get("1.0", "end") or "Otimizado:" in app.caixa_log.get("1.0", "end")


def test_cancelar_a_otimizacao_restaura_o_item(app, filme, fals, monkeypatch):
    item = _item_com_midia(app, filme)
    d = _abrir_dialogo(app, monkeypatch)
    d.ao_confirmar("4k12")
    assert bombear(app, lambda: bool(fals.chamadas))
    app._cancelar()
    assert bombear(app, lambda: app.otimizando is None)
    app.update()
    assert item.estado == PRONTO and not item.otimizado and item.caminho == filme
    assert "Otimização cancelada" in app.caixa_log.get("1.0", "end")
    assert app.mensagens.erros == [] and app.botao_otimizar.cget("state") == "normal"


def test_erro_do_ffmpeg_mostra_a_mensagem_e_nao_troca_o_arquivo(app, filme, fals, monkeypatch):
    item = _item_com_midia(app, filme)
    fals.falhar = "Unrecognized option"
    d = _abrir_dialogo(app, monkeypatch)
    fals.liberar.set()
    d.ao_confirmar("4k18")
    assert bombear(app, lambda: bool(app.mensagens.erros))
    assert "Unrecognized option" in app.mensagens.erros[0][1] and item.estado == PRONTO and not item.otimizado


def test_sem_espaco_em_disco_nao_comeca(app, filme, fals, monkeypatch):
    item = _item_com_midia(app, filme)
    d = _abrir_dialogo(app, monkeypatch)
    monkeypatch.setattr(fals.otimizar, "espaco_livre", lambda pasta: 1_000)
    d.ao_confirmar("4k18")
    assert app.otimizando is None and item.estado == PRONTO and fals.chamadas == []
    assert "Sem espaço" in app.mensagens.avisos[-1][0]


def test_voltar_ao_original_apaga_a_copia_se_o_usuario_quiser(app, filme, fals, tmp_path, monkeypatch):
    item = _item_com_midia(app, filme)
    copia = tmp_path / "c.mkv"
    copia.write_bytes(b"x" * 100)
    item.aplicar_otimizado(str(copia), "4k18")
    monkeypatch.setattr(fals.otimizar.midia, "ler", lambda caminho: _info_dv7())
    app._atualizar_detalhe()
    app._voltar_original()
    assert not item.otimizado and item.caminho == filme and not copia.exists()
    assert "Apagar a cópia" in app.mensagens.perguntas[-1][1]


def test_voltar_ao_original_mantem_a_copia_se_o_usuario_recusar(app, filme, fals, tmp_path, monkeypatch):
    item = _item_com_midia(app, filme)
    copia = tmp_path / "c.mkv"
    copia.write_bytes(b"x" * 100)
    item.aplicar_otimizado(str(copia), "4k18")
    monkeypatch.setattr(fals.otimizar.midia, "ler", lambda caminho: _info_dv7())
    app.mensagens.resposta_sim = False
    app._voltar_original()
    assert not item.otimizado and copia.exists()


def test_depois_de_postar_a_copia_otimizada_e_apagada(app, filme, fals, tmp_path):
    item = _item_com_midia(app, filme)
    copia = tmp_path / "c.mkv"
    copia.write_bytes(b"x" * 100)
    item.aplicar_otimizado(str(copia), "4k18")
    app._postou(item, "tmdb:693134")
    assert not copia.exists() and not item.otimizado and item.caminho == filme


def test_depois_de_postar_mantem_a_copia_se_a_configuracao_mandar(app, filme, fals, tmp_path):
    item = _item_com_midia(app, filme)
    copia = tmp_path / "c.mkv"
    copia.write_bytes(b"x" * 100)
    item.aplicar_otimizado(str(copia), "4k18")
    app.cfg.apagar_otimizado = False
    app._postou(item, "tmdb:693134")
    assert copia.exists() and "mantida" in app.caixa_log.get("1.0", "end")


def test_nao_posta_enquanto_otimiza(app, filme, fals):
    item = _item_com_midia(app, filme)
    app.otimizando = item
    assert any("otimização" in p for p in app._problemas_para_postar(item))
    app.otimizando = None


def test_item_otimizando_nao_entra_no_postar_todos(app, filme, fals):
    item = _pronto(app, filme)
    item.estado = OTIMIZANDO
    app._atualizar_fila()
    assert "(0)" in app.botao_postar_todos.cget("text")
    item.estado = PRONTO
