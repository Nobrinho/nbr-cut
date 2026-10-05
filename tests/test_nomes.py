"""Cortador: nome padronizado do arquivo e sugestão a partir do nome original."""
import pytest

from cortador import nomes


def test_nome_padronizado_completo():
    assert nomes.nome_padronizado("Duna: Parte 2", "2024", "2160p, WEB-DL, DUAL", ".MKV") == \
        "Duna - Parte 2 (2024) [2160p WEB-DL].mkv"


def test_nome_padronizado_sem_ano_e_sem_qualidade():
    assert nomes.nome_padronizado("Cidade de Deus", None, None, ".mp4") == "Cidade de Deus.mp4"
    assert nomes.nome_padronizado("Cidade de Deus", "2002", "Qualidade não informada", "mp4") == \
        "Cidade de Deus (2002).mp4"


def test_nome_padronizado_sem_titulo_usa_filme():
    assert nomes.nome_padronizado("???", "2020", None, ".mkv") == "Filme (2020).mkv"


def test_sanitizar_remove_o_que_o_windows_nao_aceita():
    assert nomes.sanitizar('A/B\\C:D*E?F"G<H>I|J') == "A B C - D E F G H I J"


def test_sanitizar_dois_pontos_viram_traco():
    assert nomes.sanitizar("Duna: Parte 2") == "Duna - Parte 2"
    assert nomes.sanitizar("Mission:Impossible") == "Mission - Impossible"


def test_sanitizar_mantem_acentos_e_tira_pontos_e_espacos_das_pontas():
    assert nomes.sanitizar("  Ação & Emoção...  ") == "Ação & Emoção"


def test_sanitizar_nomes_reservados_do_windows():
    assert nomes.sanitizar("CON") == "_CON"
    assert nomes.sanitizar("lpt1") == "_lpt1"
    assert nomes.sanitizar("Console") == "Console"


def test_titulo_muito_longo_e_cortado():
    nome = nomes.nome_padronizado("A" * 500, "2020", None, ".mkv")
    assert len(nome) <= nomes.MAX_TITULO + len(" (2020).mkv")


@pytest.mark.parametrize("entrada,esperado", [
    ("Dublado, 1080p, BluRay, AC3", "1080p BluRay"),
    ("2160p, WEB-DL, DUAL, HDR", "2160p WEB-DL"),
    ("4K, REMUX", "4K REMUX"),
    ("720p", "720p"),
    ("BluRay", "BluRay"),
    ("Dublado, DUAL, AC3", None),
    ("Qualidade não informada", None),
    ("", None),
    (None, None),
])
def test_qualidade_curta(entrada, esperado):
    assert nomes.qualidade_curta(entrada) == esperado


def test_extensao_em_minusculas():
    assert nomes.extensao_de(r"C:\Filmes\Filme.MKV") == ".mkv"
    assert nomes.extensao_de("sem_extensao") == ""


def test_sugestao_de_um_nome_de_release():
    s = nomes.sugestao_do_arquivo(r"C:\Filmes\Duna.Parte.2.2024.2160p.WEB-DL.DUAL.mkv")
    assert s["titulo"] == "Duna Parte 2"
    assert s["ano"] == "2024"
    assert s["qualidade"] == "2160p, WEB-DL, DUAL"
    assert s["audio"] == "Dual"
    assert s["extensao"] == ".mkv"


def test_sugestao_com_audio_dublado():
    s = nomes.sugestao_do_arquivo("Homem-Aranha.Sem.Volta.Para.Casa.2021.1080p.BluRay.x264.Dublado.mp4")
    assert s["titulo"] == "Homem-Aranha Sem Volta Para Casa"
    assert s["ano"] == "2021"
    assert s["audio"] == "Dublado"
    assert nomes.qualidade_curta(s["qualidade"]) == "1080p BluRay"
