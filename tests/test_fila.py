"""Cortador: fila da tela (regra de negócio), serviço e ponte com o loop de fundo."""
import asyncio
import threading
import time

import pytest

from cortador import config as cfg_mod
from cortador import divisao, execucao, midia, servico
from cortador.fila import ItemFila, PRONTO, NOVO, eh_video

MIB = 1024 * 1024

DADOS = {
    "tmdb_id": 693134, "titulo": "Duna: Parte 2", "titulo_original": "Dune: Part Two", "ano": "2024",
    "duracao": 166, "generos": ["Ficção científica"], "elenco": [("Zendaya", None)],
    "sinopse": "Resumo.", "poster_url": "https://x/p.jpg",
}


@pytest.fixture
def filme(tmp_path):
    caminho = tmp_path / "Duna.Parte.2.2024.2160p.WEB-DL.DUAL.mkv"
    caminho.write_bytes(b"\0" * 10_000)
    return str(caminho)


def test_item_nasce_com_sugestao_do_nome_do_arquivo(filme):
    item = ItemFila.criar(filme)
    assert item.tamanho == 10_000 and item.estado == NOVO
    assert item.termo_busca == "Duna Parte 2" and item.ano_busca == "2024"
    assert item.qualidade == "2160p, WEB-DL, DUAL" and item.audio == "Dual"
    assert item.extensao == ".mkv" and item.nome_original.endswith(".mkv")
    assert item.dados is None and item.nome_final == ""


def test_item_sem_qualidade_no_nome_fica_sem_qualidade(tmp_path):
    caminho = tmp_path / "Cidade.de.Deus.2002.mkv"
    caminho.write_bytes(b"x")
    assert ItemFila.criar(str(caminho)).qualidade is None


def test_escolher_filme_propoe_o_nome_padronizado_e_fica_pronto(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    assert item.estado == PRONTO
    assert item.nome_final == "Duna - Parte 2 (2024) [2160p WEB-DL].mkv"


def test_trocar_a_qualidade_atualiza_o_nome_automatico(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_qualidade("1080p, BluRay")
    assert item.nome_final == "Duna - Parte 2 (2024) [1080p BluRay].mkv"
    item.definir_qualidade("")
    assert item.nome_final == "Duna - Parte 2 (2024).mkv"


def test_nome_editado_a_mao_nao_e_sobrescrito(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_nome("Meu nome.mkv")
    assert item.nome_editado and item.nome_final == "Meu nome.mkv"
    item.definir_qualidade("720p")
    item.escolher_filme(DADOS)  # escolher de novo também não apaga a edição
    assert item.nome_final == "Meu nome.mkv"


def test_nome_editado_sem_extensao_ou_com_lixo_e_arrumado(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_nome('Duna: "Parte" 2')
    assert item.nome_final == "Duna - Parte 2.mkv"


def test_apagar_o_nome_volta_ao_automatico(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_nome("Outro.mkv")
    item.definir_nome("")
    assert not item.nome_editado and item.nome_final == item.nome_sugerido()


def test_plano_usa_o_nome_final_e_o_tamanho_do_arquivo(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    plano = item.plano(3_000)
    assert len(plano) == 4 and plano[0].nome == f"{item.nome_final}.part01of04"
    assert "4 partes" in item.resumo_plano(3_000)
    assert "não precisa cortar" in item.resumo_plano(50_000)


def test_legenda_e_onde_ela_vai(filme):
    item = ItemFila.criar(filme)
    assert item.legenda() == ("", True) and item.descricao_da_legenda() == ""
    item.escolher_filme(DADOS)
    texto, cabe = item.legenda()
    assert texto.startswith("Título: Duna: Parte 2") and "Áudio: Dual" in texto and "Qualidade: 2160p, WEB-DL, DUAL" in texto
    assert cabe is True and "na legenda da parte 1" in item.descricao_da_legenda()
    item.escolher_filme({**DADOS, "sinopse": "palavra " * 400})
    assert item.legenda()[1] is False and "separada" in item.descricao_da_legenda()


def test_pendencias(filme):
    item = ItemFila.criar(filme)
    limite = divisao.LIMITE_GRATIS
    assert item.pendencias(divisao.TAMANHO_PADRAO, limite) == ["Escolha o filme no TMDB", "O nome do arquivo está vazio"]
    item.escolher_filme(DADOS)
    assert item.pendencias(divisao.TAMANHO_PADRAO, limite) == []
    assert any("limite" in p for p in item.pendencias(3900 * MIB, limite))
    import os
    os.remove(filme)
    assert any("não existe" in p for p in item.pendencias(divisao.TAMANHO_PADRAO, limite))


def test_filme_ja_publicado_bloqueia_a_postagem(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.duplicado = {"titulo": "Duna", "publicado_em": "2026-10-01T10:00:00"}
    faltas = item.pendencias(divisao.TAMANHO_PADRAO, divisao.LIMITE_GRATIS)
    assert len(faltas) == 1 and "2026-10-01" in faltas[0] and "/buscar" in faltas[0]
    item.escolher_filme(DADOS)  # escolher outro filme limpa o aviso
    assert item.duplicado is None and item.aviso == ""


def test_eh_video():
    assert eh_video("a.MKV") and eh_video(r"C:\x\a.mp4") and not eh_video("a.srt") and not eh_video("a")


# --------------------------------------------------------------------------- serviço

def _servico(tmp_path, **kw):
    cfg = cfg_mod.Configuracao(
        canal_destino="-100PROD", canal_teste="-100TESTE",
        tamanho_parte=3_000, pasta_jobs=tmp_path / "jobs", **kw,
    )
    return servico.Servico(cfg)


def test_novo_job_congela_nome_canal_e_plano(tmp_path, filme):
    s = _servico(tmp_path)
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    job = s.novo_job(item)
    assert job.nome_base == item.nome_final and job.canal == "-100PROD" and job.total_partes == 4
    assert job.tmdb_id == 693134 and job.audio == "Dual" and job.qualidade == "2160p, WEB-DL, DUAL"


def test_novo_job_sem_filme_escolhido_e_erro(tmp_path, filme):
    with pytest.raises(ValueError, match="TMDB"):
        _servico(tmp_path).novo_job(ItemFila.criar(filme))


def test_postar_recusa_job_de_outro_canal(tmp_path, filme):
    s = _servico(tmp_path)
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    job = s.novo_job(item)
    s.cfg.modo_teste = True  # o canal ativo agora é o de teste
    with pytest.raises(ValueError, match="canal"):
        asyncio.run(s.postar(job, lambda e: None, lambda: False))


def test_pendentes_vem_do_repositorio(tmp_path, filme):
    s = _servico(tmp_path)
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    job = s.novo_job(item)
    s.repositorio.salvar(job)
    assert [j.id for j in s.pendentes()] == [job.id]


def test_resolver_canal_refaz_quando_o_canal_ativo_muda(tmp_path, monkeypatch):
    s = _servico(tmp_path)
    resolvidos = []

    async def falso(cliente, canal):
        resolvidos.append(canal)
        return f"entidade{canal}"

    monkeypatch.setattr(servico.telegram_envio, "resolver_canal", falso)
    s.cliente = object()
    assert asyncio.run(s.resolver_canal()) == "entidade-100PROD"
    asyncio.run(s.resolver_canal())  # mesmo canal: não resolve de novo
    s.cfg.modo_teste = True
    assert asyncio.run(s.resolver_canal()) == "entidade-100TESTE"
    assert resolvidos == ["-100PROD", "-100TESTE"]


# --------------------------------------------------------------------------- loop de fundo

def test_loop_de_fundo_roda_corrotinas_em_outra_thread():
    loop = execucao.LoopDeFundo()
    try:
        async def quem():
            await asyncio.sleep(0)
            return threading.current_thread().name

        assert loop.esperar(quem(), timeout=5) == "cortador-asyncio"
        assert loop.esperar(quem(), timeout=5) == "cortador-asyncio"
    finally:
        loop.parar()


def test_ao_terminar_entrega_resultado_e_erro_na_thread_da_interface():
    loop = execucao.LoopDeFundo()
    entregues = []
    fila_ui: list = []
    despachante = execucao.Despachante(lambda f: fila_ui.append(f))
    try:
        async def ok():
            return 42

        async def falha():
            raise RuntimeError("deu ruim")

        execucao.ao_terminar(loop.rodar(ok()), despachante, lambda r: entregues.append(("ok", r)),
                             lambda e: entregues.append(("erro", str(e))))
        execucao.ao_terminar(loop.rodar(falha()), despachante, lambda r: entregues.append(("ok", r)),
                             lambda e: entregues.append(("erro", str(e))))
        limite = time.time() + 5
        while len(fila_ui) < 2 and time.time() < limite:
            time.sleep(0.01)
        assert entregues == []  # nada chamado fora da "thread da interface"
        for funcao in fila_ui:
            funcao()
        assert sorted(entregues) == [("erro", "deu ruim"), ("ok", 42)]
    finally:
        loop.parar()


def test_cancelamento_e_uma_bandeira_compartilhada():
    c = execucao.Cancelamento()
    assert c() is False
    c.cancelar()
    assert c() is True
    c.zerar()
    assert c() is False


# ====================================================================== metadados do arquivo

def _info(**kw):
    video = midia.Video("HEVC", 3840, 2160, 23.976, 10, None, 7, 6, True)
    return midia.InfoMidia("mkv", 9_318.3, video, [midia.Audio("AC3", 6, "pt")], **kw)


def test_midia_preenche_a_qualidade_quando_o_nome_nao_dizia(tmp_path):
    caminho = tmp_path / "Cidade.de.Deus.2002.mkv"
    caminho.write_bytes(b"x")
    item = ItemFila.criar(str(caminho))
    item.escolher_filme({**DADOS, "titulo": "Cidade de Deus", "ano": "2002"})
    assert item.qualidade is None
    item.definir_midia(_info())
    assert item.qualidade == "2160p, HDR"
    assert item.nome_final == "Cidade de Deus (2002) [2160p].mkv"   # o nome usa só o 1º termo


def test_midia_nao_mexe_na_qualidade_do_nome_nem_no_nome_editado(filme):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_nome("Meu Nome.mkv")
    item.definir_midia(_info())
    assert item.qualidade == "2160p, WEB-DL, DUAL"      # ja vinha do nome do arquivo
    assert item.nome_final == "Meu Nome.mkv"


def test_midia_sem_filme_escolhido_so_guarda(tmp_path):
    caminho = tmp_path / "Sem.Qualidade.2020.mkv"
    caminho.write_bytes(b"x")
    item = ItemFila.criar(str(caminho))
    item.definir_midia(_info())
    assert item.qualidade == "2160p, HDR" and item.nome_final == ""


def test_avisos_e_duracao_real(filme):
    item = ItemFila.criar(filme)
    assert item.avisos_midia() == [] and item.duracao_real_s is None
    item.definir_midia(_info())
    assert item.duracao_real_s == 9_318
    assert any(a.nivel == midia.ATENCAO and "perfil 7" in a.texto for a in item.avisos_midia())


def test_servico_leva_duracao_e_dimensoes_para_o_job(filme, tmp_path):
    item = ItemFila.criar(filme)
    item.escolher_filme(DADOS)
    item.definir_midia(_info())
    cfg = cfg_mod.Configuracao(canal_destino="-100123", modo_teste=False, pasta_jobs=str(tmp_path / "jobs"))
    cfg.tamanho_parte = 3_000
    job = servico.Servico(cfg).novo_job(item)
    assert (job.duracao_s, job.largura, job.altura) == (9_318, 3840, 2160)
    sem = ItemFila.criar(filme)
    sem.escolher_filme(DADOS)
    job2 = servico.Servico(cfg).novo_job(sem)
    assert (job2.duracao_s, job2.largura, job2.altura) == (None, 0, 0)
