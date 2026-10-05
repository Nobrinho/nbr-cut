"""Cortador: a janela principal, com serviço falso (sem Telegram, sem banco, sem diálogos reais).

Cria uma janela Tk de verdade (escondida). Pula sozinho onde não há ambiente gráfico.
"""
import os
import time
from pathlib import Path

import pytest

ctk = pytest.importorskip("customtkinter")

from cortador import config, execucao
from cortador.fila import CANCELADO, CONCLUIDO, ERRO, NOVO, PRONTO, ItemFila
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
