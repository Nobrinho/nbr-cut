"""CONTRATO com o bot (agente_filmes): as cópias do app têm de produzir EXATAMENTE o que o bot produz.

Estes testes comparam o app com o código do bot e por isso só rodam quando o repositório do bot está
acessível (`NBR_BOT_DIR`, padrão `C:\\DEV\\agente_filmes`); sem ele, pulam. Se falharem, o bot mudou o
formato: atualize `cortador/compartilhado/*` (e o CONTRATO.md) — ou o app vai postar algo que o bot, o
monitor e o app Nbr PLAY não entendem.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortador import registro
from cortador.compartilhado import comandos, extrator, formato, legenda, tmdb
from cortador.publicador import legenda_ref
from memoria import ArmazenamentoEmMemoria

BOT_DIR = Path(os.getenv("NBR_BOT_DIR", r"C:\DEV\agente_filmes"))

pytestmark = pytest.mark.skipif(not (BOT_DIR / "partes.py").exists(), reason=f"repo do bot não encontrado em {BOT_DIR}")


@pytest.fixture(scope="module")
def bot_modulos():
    """Módulos puros do bot (sem importar `bot.py`). O caminho do bot entra NO FIM do sys.path: o
    `cortador` deste repo continua sendo o importado."""
    sys.path.append(str(BOT_DIR))
    os.environ.setdefault("DATABASE_URL", "")  # não deixa o db_store do bot tentar conectar
    try:
        import extrator as bot_extrator
        import legenda as bot_legenda
        import partes as bot_partes
        import tmdb_client as bot_tmdb
        yield SimpleNamespace(extrator=bot_extrator, legenda=bot_legenda, partes=bot_partes, tmdb=bot_tmdb)
    finally:
        sys.path.remove(str(BOT_DIR))


def _bot_completo():
    """O `bot.py` inteiro (heavy: pode falhar se o banco do bot estiver fora do ar no import)."""
    sys.path.append(str(BOT_DIR))
    try:
        import bot
        return bot
    except Exception as erro:  # noqa: BLE001
        pytest.skip(f"bot.py não importável aqui: {erro}")
    finally:
        sys.path.remove(str(BOT_DIR))


# ====================================================================== nome das partes e registro

NOMES = [
    "Filme.2020.mkv.part03of11", "a.mp4.part01of02", "a.mkv.part1of3", "  a.mkv.PART02OF03 ", "x.part01of02.mkv.part02of04",
    "Filme.mkv", ".part01of02", "Filme.mkv.part01", "a.mkv.part00of05", "a.mkv.part06of05", "a.mkv.part01of01",
    "a.mkv.part01of99999999999999999999", "", None,
]


@pytest.mark.parametrize("nome", NOMES)
def test_interpretar_nome_igual(bot_modulos, nome):
    nosso, do_bot = formato.interpretar_nome(nome), bot_modulos.partes.interpretar_nome(nome)
    assert (nosso is None) == (do_bot is None)
    if do_bot:
        assert (nosso.base, nosso.indice, nosso.total) == (do_bot.base, do_bot.indice, do_bot.total)


@pytest.mark.parametrize("base,indice,total", [("a.mkv", 1, 9), ("a.mkv", 3, 11), ("a.mkv", 7, 120), ("F (2020) [4K].mkv", 12, 12)])
def test_formatar_nome_igual(bot_modulos, base, indice, total):
    assert formato.formatar_nome(base, indice, total) == bot_modulos.partes.formatar_nome(base, indice, total)


@pytest.mark.parametrize("total,ids", [(3, [10, 11, 12, 13]), (3, [11, 12, 13]), (3, [11, 12]), (0, [1]), (2, [5, 6])])
def test_campos_do_registro_iguais(bot_modulos, total, ids):
    assert formato.campos_registro(total, ids) == bot_modulos.partes.campos_registro(total, ids)


@pytest.mark.parametrize("reg", [
    {"message_ids": [1, 2]}, {"message_ids": [1]}, {"message_ids": []},
    {"message_ids": list(range(1, 12)), "partes": 11, "texto_separado": False},
    {"message_ids": list(range(1, 13)), "partes": 11, "texto_separado": True, "video_message_id": 2},
])
def test_regras_do_registro_iguais(bot_modulos, reg):
    assert formato.tem_texto_separado(reg) == bot_modulos.partes.tem_texto_separado(reg)
    assert formato.id_do_video(reg) == bot_modulos.partes.id_do_video(reg)


@pytest.mark.parametrize("com_texto,total", [(False, 1), (True, 1), (False, 3), (True, 3)])
def test_registro_tem_as_mesmas_chaves_e_valores_do_bot(monkeypatch, com_texto, total):
    bot = _bot_completo()
    monkeypatch.setattr(bot, "_recarregar_postagens_publicadas", lambda: None)
    monkeypatch.setattr(bot, "_salvar_postagens_publicadas", lambda: None)
    monkeypatch.setattr(bot, "_atualizar_indice_app_async", lambda: None)
    monkeypatch.setattr(bot, "POSTAGENS_PUBLICADAS", {})
    ids = list(range(100, 100 + total + (1 if com_texto else 0)))
    dados_tmdb = {"tmdb_id": 693134, "titulo": "Duna: Parte 2", "ano": "2024"}
    dados = {"dados_tmdb": dados_tmdb, "nome_arquivo": "Duna.mkv", "file_id": None,
             "partes": [{"indice": i + 1} for i in range(total)] if total > 1 else None}
    bot._registrar_postagem_publicada(dados, [SimpleNamespace(message_id=i) for i in ids])
    do_bot = dict(bot.POSTAGENS_PUBLICADAS["tmdb:693134"])
    nosso = registro.montar_registro(dados_tmdb, "Duna.mkv", ids, total, agora=datetime(2026, 10, 4))
    assert set(nosso) - {"origem"} == set(do_bot)
    for chave in do_bot:
        if chave != "publicado_em":
            assert nosso[chave] == do_bot[chave], chave


def test_chave_do_registro_igual_a_do_bot():
    bot = _bot_completo()
    assert bot._chave_publicacao({"dados_tmdb": {"tmdb_id": 693134}}) == registro.chave_filme(693134)


def test_legenda_de_referencia_da_parte_1_igual_a_do_bot():
    bot = _bot_completo()
    dados = {"dados_tmdb": {"tmdb_id": 693134, "titulo": "Duna: Parte 2"}}
    assert legenda_ref(dados["dados_tmdb"]) == bot._legenda_ref_video(dados)


# ====================================================================== legenda

DADOS_LEGENDA = [
    {"tmdb_id": 693134, "titulo": "Duna: Parte 2", "titulo_original": "Dune: Part Two", "ano": "2024", "duracao": 166,
     "nota": 8.24, "classificacao": "14", "generos": ["Ficção científica", "Aventura"], "colecao": "Duna", "pais": "EUA",
     "diretor": "Denis Villeneuve", "estudio": "Legendary", "elenco": [("Timothée Chalamet", "https://x/tc.jpg"), ("Zendaya", None)],
     "poster_url": "https://x/p.jpg", "backdrop_url": "https://x/b.jpg", "trailer_url": "https://y/t", "tags": ["deserto", "épico"],
     "sinopse": "Resumo longo."},
    {"tmdb_id": 1, "titulo": "Mínimo"},
    {"tmdb_id": 2, "titulo": "Igual", "titulo_original": "Igual", "ano": "1990", "elenco": [], "generos": [], "tags": []},
    {"tmdb_id": 3, "titulo": "Do ano", "ano": str(datetime.now().year)},
]


@pytest.mark.parametrize("dados", DADOS_LEGENDA)
@pytest.mark.parametrize("audio,qualidade", [(None, None), ("Dual", "2160p, WEB-DL"), ("Dublado", "Qualidade não informada")])
def test_legenda_do_filme_igual(bot_modulos, dados, audio, qualidade):
    assert legenda.montar_legenda_filme(dados, audio, qualidade) == bot_modulos.legenda.montar_legenda_filme(dados, audio, qualidade)


def test_limite_da_legenda_igual(bot_modulos):
    assert legenda.LIMITE_LEGENDA_MIDIA == bot_modulos.legenda.LIMITE_LEGENDA_MIDIA


# ====================================================================== extrator

ARQUIVOS = [
    "Duna.Parte.2.2024.2160p.WEB-DL.DUAL.mkv", "Homem-Aranha.Sem.Volta.Para.Casa.2021.1080p.BluRay.x264.Dublado.mp4",
    "Cidade.de.Deus.2002.mkv", "Toy Story (1995) [1080p] Dual 5.1.mkv", "1917.2019.1080p.mkv", "Matrix 1999 REMUX 4K HDR.mkv",
    "filme_sem_ano_nenhum.mp4", "Blade.Runner.2049.2017.720p.WEBRip.Legendado.mkv",
]


@pytest.mark.parametrize("nome", ARQUIVOS)
def test_extrator_igual(bot_modulos, nome):
    b = bot_modulos.extrator
    assert extrator.extrair_titulo(None, nome) == b.extrair_titulo(None, nome)
    assert extrator.extrair_ano(nome) == b.extrair_ano(nome)
    assert extrator.extrair_qualidade(nome) == b.extrair_qualidade(nome)
    assert extrator.extrair_audio(nome) == b.extrair_audio(nome)


def test_extrator_titulo_a_partir_da_legenda_igual(bot_modulos):
    for legenda_texto in ["Duna (2021) [MKV]\n@canal | @outro", "Título limpo\nresto", "2019 (2019)", "Filme @canal"]:
        assert extrator.extrair_titulo(legenda_texto, "arq.mkv") == bot_modulos.extrator.extrair_titulo(legenda_texto, "arq.mkv")


# ====================================================================== TMDB

PAYLOAD_FILME = {
    "id": 693134, "title": "Duna: Parte Dois", "original_title": "Dune: Part Two", "release_date": "2024-02-27",
    "runtime": 166, "vote_average": 8.2, "overview": "Sinopse.", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
    "genres": [{"name": "Ficção científica"}, {"name": "Aventura"}],
    "belongs_to_collection": {"name": "Duna"}, "production_countries": [{"name": "Estados Unidos"}],
    "production_companies": [{"name": "Legendary"}],
    "credits": {"cast": [{"name": f"Ator {i}", "profile_path": "/f.jpg" if i % 2 else None} for i in range(8)],
                "crew": [{"job": "Producer", "name": "X"}, {"job": "Director", "name": "Denis Villeneuve"}]},
    "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
                                  {"iso_3166_1": "BR", "release_dates": [{"certification": "14"}]}]},
    "videos": {"results": [{"site": "Vimeo", "type": "Trailer", "key": "no"}, {"site": "YouTube", "type": "Trailer", "key": "abc"}]},
    "keywords": {"keywords": [{"name": f"tag{i}"} for i in range(6)]},
}


class _Resposta:
    def __init__(self, dados, status=200):
        self._dados, self.status_code = dados, status

    def json(self):
        return self._dados

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            erro = requests.HTTPError(f"HTTP {self.status_code}")
            erro.response = self
            raise erro


def test_detalhes_do_filme_iguais(bot_modulos, monkeypatch):
    for modulo in (tmdb, bot_modulos.tmdb):
        monkeypatch.setattr(modulo.requests, "get", lambda *a, **k: _Resposta(PAYLOAD_FILME))
    assert tmdb.detalhar_filme(693134, "K") == bot_modulos.tmdb.detalhar_filme(693134, "K")


def test_filme_inexistente_levanta_a_mesma_excecao(bot_modulos, monkeypatch):
    for modulo in (tmdb, bot_modulos.tmdb):
        monkeypatch.setattr(modulo.requests, "get", lambda *a, **k: _Resposta({}, 404))
    with pytest.raises(tmdb.FilmeNaoEncontrado):
        tmdb.detalhar_filme(1, "K")
    with pytest.raises(bot_modulos.tmdb.FilmeNaoEncontrado):
        bot_modulos.tmdb.detalhar_filme(1, "K")


def test_busca_de_candidatos_faz_a_mesma_chamada(bot_modulos, monkeypatch):
    # os dois módulos usam o MESMO `requests`: um patch só, e as chamadas de cada lado são separadas por fase
    chamadas: list = []
    resultados = {"results": [{"id": 1, "title": "A"}]}
    monkeypatch.setattr(tmdb.requests, "get", lambda *a, **k: chamadas.append((a, k)) or _Resposta(resultados))
    nosso = tmdb.buscar_resultados_filmes("Duna", "K", "2021")
    chamadas_nosso, chamadas[:] = list(chamadas), []
    do_bot = bot_modulos.tmdb.buscar_resultados_filmes("Duna", "K", "2021")
    assert nosso == do_bot
    assert chamadas_nosso == chamadas and len(chamadas) == 1


@pytest.mark.parametrize("entrada", [
    "tmdb:693134", "TMDB: 5", "id:7", "id 8", "693134", "1984", "https://www.themoviedb.org/movie/693134-duna", "Duna",
    "https://www.themoviedb.org/tv/1", "https://outro.com/movie/1", "", None,
])
def test_id_do_tmdb_igual(bot_modulos, entrada):
    def avaliar(funcao):
        try:
            return ("ok", funcao(entrada))
        except ValueError as erro:
            return ("erro", str(erro))

    assert avaliar(tmdb.extrair_id_filme_tmdb) == avaliar(bot_modulos.tmdb.extrair_id_filme_tmdb)


# ====================================================================== fila de ordens

def test_ordem_regerar_indice_igual_a_do_bot(monkeypatch, bot_modulos):
    sys.path.append(str(BOT_DIR))
    try:
        import fila_comandos
    finally:
        sys.path.remove(str(BOT_DIR))
    memoria: dict = {}

    def carregar(chave, padrao, caminho_fallback=None):
        valor = memoria.get(chave, padrao)
        return valor if isinstance(valor, type(padrao)) else padrao

    def atualizar(chave, padrao, alterar, caminho_fallback=None):
        memoria[chave] = alterar(carregar(chave, padrao))
        return memoria[chave]

    monkeypatch.setattr(fila_comandos, "carregar_json", carregar)
    monkeypatch.setattr(fila_comandos, "atualizar_json", atualizar)
    monkeypatch.setattr(fila_comandos, "registrar_evento", lambda *a, **k: None)
    job_bot = fila_comandos.enfileirar("regerar_indice", {"motivo": "x"}, origem="cortador")
    status_bot = memoria["status_comandos"][job_bot["id"]]

    nosso_armazenamento = ArmazenamentoEmMemoria()
    job_nosso = comandos.enfileirar_regerar_indice(nosso_armazenamento, "x", origem="cortador")
    status_nosso = nosso_armazenamento.dados["status_comandos"][job_nosso["id"]]

    assert set(job_nosso) == set(job_bot)
    assert {k: v for k, v in job_nosso.items() if k not in ("id", "criado_em")} == \
           {k: v for k, v in job_bot.items() if k not in ("id", "criado_em")}
    assert {k: v for k, v in status_nosso.items() if k != "atualizado_em"} == \
           {k: v for k, v in status_bot.items() if k != "atualizado_em"}
    assert list(nosso_armazenamento.dados["comandos_bot"]) == list(memoria["comandos_bot"]) == ["jobs"]
    # o bot tem de reconhecer o tipo como dele
    assert fila_comandos.EXECUTOR_POR_TIPO[comandos.TIPO_REGERAR_INDICE] == fila_comandos.FILA_BOT == comandos.FILA_BOT
    assert (fila_comandos.CHAVE_STATUS, fila_comandos.CHAVE_PONTO) == (comandos.CHAVE_STATUS, comandos.CHAVE_PONTO)
    assert fila_comandos.TOLERANCIA_PONTO_SEGUNDOS == comandos.TOLERANCIA_PONTO_SEGUNDOS


@pytest.mark.parametrize("segundos", [0, 10, 39, 41, 300])
def test_sinal_de_vida_igual_ao_do_bot(monkeypatch, segundos):
    sys.path.append(str(BOT_DIR))
    try:
        import fila_comandos
    finally:
        sys.path.remove(str(BOT_DIR))
    ponto = {"bot": (datetime.now() - timedelta(seconds=segundos)).isoformat(timespec="seconds")}
    monkeypatch.setattr(fila_comandos, "carregar_json", lambda chave, padrao, caminho_fallback=None: ponto)
    esperado = fila_comandos.executor_vivo("bot")[0]
    assert comandos.executor_vivo(ArmazenamentoEmMemoria({"ponto_executores": ponto}), "bot")[0] == esperado
