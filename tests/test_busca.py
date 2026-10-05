"""Cortador: busca do filme no TMDB (com a API simulada)."""
from cortador import busca


def _bruto(i, titulo, data="2021-12-15", original=None, poster="/p.jpg"):
    return {"id": i, "title": titulo, "original_title": original or titulo,
            "release_date": data, "poster_path": poster}


def test_buscar_devolve_candidatos_com_ano_e_miniatura():
    chamadas = []

    def falso(titulo, chave, ano=None):
        chamadas.append((titulo, chave, ano))
        return [_bruto(1, "Duna", "2021-10-22", "Dune"), _bruto(2, "Duna: Parte Dois", "2024-02-27")]

    achados = busca.buscar("duna", "KEY", buscador=falso)
    assert [c.tmdb_id for c in achados] == [1, 2]
    assert achados[0].ano == "2021"
    assert achados[0].miniatura_url == "https://image.tmdb.org/t/p/w92/p.jpg"
    assert achados[0].rotulo == "Duna (2021) — Dune"
    assert achados[1].rotulo == "Duna: Parte Dois (2024)"
    assert chamadas == [("duna", "KEY", None)]


def test_buscar_passa_o_ano_quando_ha():
    chamadas = []
    busca.buscar("duna", "K", "2024", buscador=lambda t, k, a=None: chamadas.append((t, a)) or [_bruto(1, "Duna")])
    assert chamadas == [("duna", "2024")]


def test_buscar_sem_resultado_com_ano_tenta_sem_ano():
    chamadas = []

    def falso(titulo, chave, ano=None):
        chamadas.append(ano)
        return [] if ano else [_bruto(7, "Duna")]

    achados = busca.buscar("duna", "K", "1999", buscador=falso)
    assert [c.tmdb_id for c in achados] == [7]
    assert chamadas == ["1999", None]


def test_buscar_ignora_repetidos_e_registros_sem_id():
    achados = busca.buscar(
        "x", "K",
        buscador=lambda t, k, a=None: [_bruto(1, "A"), _bruto(1, "A de novo"), {"title": "sem id"}, _bruto(2, "B")],
    )
    assert [c.tmdb_id for c in achados] == [1, 2]


def test_buscar_respeita_o_limite():
    achados = busca.buscar("x", "K", buscador=lambda t, k, a=None: [_bruto(i, f"F{i}") for i in range(1, 40)], limite=5)
    assert len(achados) == 5


def test_buscar_termo_em_branco_nao_chama_a_api():
    def nao_deve_chamar(*args, **kwargs):
        raise AssertionError("chamou a API")

    assert busca.buscar("   ", "K", buscador=nao_deve_chamar) == []
    assert busca.buscar("", "K", buscador=nao_deve_chamar) == []


def test_candidato_sem_data_ou_poster():
    c = busca.buscar("x", "K", buscador=lambda t, k, a=None: [_bruto(3, "Sem data", data="", poster=None)])[0]
    assert c.ano is None and c.miniatura_url is None
    assert c.rotulo == "Sem data"


def test_resolver_entrada_reconhece_id_e_url():
    assert busca.resolver_entrada("tmdb:693134") == 693134
    assert busca.resolver_entrada("https://www.themoviedb.org/movie/693134-duna-parte-dois") == 693134
    assert busca.resolver_entrada("Duna") is None
    assert busca.resolver_entrada("1984") is None  # título, não id


def test_detalhar_repassa_para_o_cliente_do_tmdb():
    assert busca.detalhar(5, "K", detalhador=lambda i, k: {"tmdb_id": i, "titulo": "X", "chave": k}) == \
        {"tmdb_id": 5, "titulo": "X", "chave": "K"}
