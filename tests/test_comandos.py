"""Fila de ordens do bot (`regerar_indice`) e sinal de vida do executor."""
from datetime import datetime, timedelta

from memoria import ArmazenamentoEmMemoria

from cortador.compartilhado import comandos


def test_enfileira_a_ordem_com_o_formato_do_bot():
    a = ArmazenamentoEmMemoria()
    job = comandos.enfileirar_regerar_indice(a, "cortador: Duna")
    assert set(job) == {"id", "tipo", "criado_em", "origem", "criado_por", "params", "retorno"}
    assert job["tipo"] == "regerar_indice" and job["origem"] == "cortador"
    assert job["params"] == {"motivo": "cortador: Duna"} and job["criado_por"] is None and job["retorno"] is None
    assert a.dados["comandos_bot"] == {"jobs": [job]}


def test_status_inicial_pendente_antes_de_entrar_na_fila():
    a = ArmazenamentoEmMemoria()
    job = comandos.enfileirar_regerar_indice(a, "x")
    status = a.dados["status_comandos"][job["id"]]
    assert status["estado"] == "pendente" and status["tipo"] == "regerar_indice"
    assert (status["total"], status["cursor"], status["feitos"], status["falhas"]) == (0, 0, 0, 0)
    assert status["chave"] is None and status["origem"] == "cortador" and "atualizado_em" in status
    # o status é escrito ANTES da fila (um "pendente" tardio nunca sobrescreve o do executor)
    assert a.escritas.index("status_comandos") < a.escritas.index("comandos_bot")


def test_nao_mexe_nas_ordens_nem_nos_status_que_ja_existem():
    existente = {"id": "abc", "tipo": "apagar_post"}
    a = ArmazenamentoEmMemoria({
        "comandos_bot": {"jobs": [existente]},
        "status_comandos": {"abc": {"estado": "executando"}},
    })
    novo = comandos.enfileirar_regerar_indice(a, "x")
    assert a.dados["comandos_bot"]["jobs"] == [existente, novo]
    assert a.dados["status_comandos"]["abc"] == {"estado": "executando"}


def test_cada_ordem_tem_id_proprio():
    a = ArmazenamentoEmMemoria()
    ids = {comandos.enfileirar_regerar_indice(a, str(i))["id"] for i in range(20)}
    assert len(ids) == 20


def test_executor_vivo():
    agora = datetime.now()
    a = ArmazenamentoEmMemoria({"ponto_executores": {
        "bot": (agora - timedelta(seconds=10)).isoformat(timespec="seconds"),
        "monitor": (agora - timedelta(seconds=300)).isoformat(timespec="seconds"),
        "quebrado": "ontem",
    }})
    vivo, visto = comandos.executor_vivo(a, "bot")
    assert vivo is True and visto
    assert comandos.executor_vivo(a, "monitor")[0] is False
    assert comandos.executor_vivo(a, "quebrado") == (False, "ontem")
    assert comandos.executor_vivo(a, "ninguem") == (False, None)
    assert comandos.executor_vivo(ArmazenamentoEmMemoria(), "bot") == (False, None)


def test_tolerancia_do_sinal_de_vida_e_a_do_bot():
    agora = datetime.now()
    quase = (agora - timedelta(seconds=comandos.TOLERANCIA_PONTO_SEGUNDOS - 5)).isoformat()
    passou = (agora - timedelta(seconds=comandos.TOLERANCIA_PONTO_SEGUNDOS + 5)).isoformat()
    assert comandos.executor_vivo(ArmazenamentoEmMemoria({"ponto_executores": {"bot": quase}}))[0] is True
    assert comandos.executor_vivo(ArmazenamentoEmMemoria({"ponto_executores": {"bot": passou}}))[0] is False
