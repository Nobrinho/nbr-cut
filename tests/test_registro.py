"""Registro de postagens (mesmo schema do bot), duplicidade e fila do índice."""
from datetime import datetime

import pytest
from memoria import ArmazenamentoEmMemoria

from cortador import publicador, registro
from cortador.compartilhado import formato
from cortador.compartilhado.armazenamento import ChaveInexistente
from cortador.registro import JaPublicado

DADOS = {"tmdb_id": 693134, "titulo": "Duna: Parte 2", "ano": "2024"}
AGORA = datetime(2026, 10, 4, 12, 30, 0)


def test_chave_igual_a_do_bot():
    assert registro.chave_filme(693134) == "tmdb:693134"


def test_registro_de_arquivo_unico():
    r = registro.montar_registro(DADOS, "Duna.mkv", [10, 11], total_partes=1, agora=AGORA)
    assert r["tipo"] == "filme" and r["tmdb_id"] == 693134 and r["titulo"] == "Duna: Parte 2"
    assert r["ano"] == "2024" and r["nome_arquivo"] == "Duna.mkv"
    assert r["file_id"] is None and r["message_ids"] == [10, 11]
    assert r["publicado_em"] == "2026-10-04T12:30:00" and r["origem"] == "cortador"
    assert "partes" not in r and "video_message_id" not in r


def test_registro_de_filme_em_partes_com_texto():
    # ordem lógica: [texto, parte1, parte2, parte3]
    r = registro.montar_registro(DADOS, "Duna.mkv", [50, 41, 42, 43], total_partes=3, agora=AGORA)
    assert r["partes"] == 3 and r["texto_separado"] is True and r["video_message_id"] == 41
    assert r["message_ids"] == [50, 41, 42, 43]


def test_registro_de_filme_em_partes_sem_texto():
    r = registro.montar_registro(DADOS, "Duna.mkv", [41, 42, 43], total_partes=3, agora=AGORA)
    assert r["texto_separado"] is False and r["video_message_id"] == 41
    assert formato.tem_texto_separado(r) is False
    assert formato.id_do_video(r) == 41


def test_registro_em_partes_funciona_com_as_regras_do_resto_do_sistema():
    r = registro.montar_registro(DADOS, "Duna.mkv", [50, 41, 42], total_partes=2, agora=AGORA)
    assert formato.tem_texto_separado(r) is True
    assert formato.id_do_video(r) == 41


def test_registrar_grava_e_devolve_a_chave():
    armazenamento = ArmazenamentoEmMemoria({"postagens_publicadas": {"tmdb:1": {"titulo": "Outro"}}})
    chave, r = registro.registrar(armazenamento, DADOS, "Duna.mkv", [1, 2], 1, agora=AGORA)
    assert chave == "tmdb:693134"
    assert armazenamento.dados["postagens_publicadas"]["tmdb:693134"] == r
    assert armazenamento.dados["postagens_publicadas"]["tmdb:1"] == {"titulo": "Outro"}  # não mexe no resto


def test_registrar_recusa_duplicado_sem_alterar_nada():
    armazenamento = ArmazenamentoEmMemoria({
        "postagens_publicadas": {"tmdb:693134": {"titulo": "Duna", "publicado_em": "2026-10-01T00:00:00"}}
    })
    antes = dict(armazenamento.dados["postagens_publicadas"])
    with pytest.raises(JaPublicado) as erro:
        registro.registrar(armazenamento, DADOS, "Duna.mkv", [1, 2], 1)
    assert erro.value.chave == "tmdb:693134" and erro.value.registro["titulo"] == "Duna"
    assert armazenamento.dados["postagens_publicadas"] == antes


def test_registrar_sem_a_chave_do_bot_no_armazenamento_nao_a_cria():
    """O bot só migra o histórico dele se a chave não existir: criá-la vazia o faria perder tudo."""
    armazenamento = ArmazenamentoEmMemoria({})
    with pytest.raises(ChaveInexistente):
        registro.registrar(armazenamento, DADOS, "Duna.mkv", [1, 2], 1)
    assert "postagens_publicadas" not in armazenamento.dados


def test_ja_publicado():
    armazenamento = ArmazenamentoEmMemoria({"postagens_publicadas": {"tmdb:693134": {"titulo": "Duna"}}})
    assert registro.ja_publicado(armazenamento, 693134) == {"titulo": "Duna"}
    assert registro.ja_publicado(armazenamento, 1) is None
    assert registro.ja_publicado(ArmazenamentoEmMemoria(), 1) is None


def test_indice_enfileirado_com_o_bot_no_ar():
    armazenamento = ArmazenamentoEmMemoria({"ponto_executores": {"bot": datetime.now().isoformat()}})
    assert registro.enfileirar_indice(armazenamento, "cortador: Duna") == "enfileirado"
    (job,) = armazenamento.dados["comandos_bot"]["jobs"]
    assert job["tipo"] == "regerar_indice" and job["params"] == {"motivo": "cortador: Duna"}


def test_indice_enfileirado_mesmo_com_o_bot_parado_mas_avisa():
    armazenamento = ArmazenamentoEmMemoria()
    assert registro.enfileirar_indice(armazenamento, "x") == "bot_parado"
    assert len(armazenamento.dados["comandos_bot"]["jobs"]) == 1  # fica na fila até o bot subir


def test_registro_nulo_posta_sem_registrar():
    nulo = publicador.RegistroNulo()
    assert nulo.ja_publicado(1) is None
    assert nulo.registrar(DADOS, "Duna.mkv", [1], 1) == "tmdb:693134"
    assert nulo.enfileirar_indice("x") == "sem_registro"


def test_registro_do_bot_usa_o_armazenamento():
    armazenamento = ArmazenamentoEmMemoria({"postagens_publicadas": {}})
    api = publicador.RegistroDoBot(armazenamento)
    assert api.ja_publicado(693134) is None
    assert api.registrar(DADOS, "Duna.mkv", [1, 2], 1) == "tmdb:693134"
    assert api.ja_publicado(693134)["titulo"] == "Duna: Parte 2"
