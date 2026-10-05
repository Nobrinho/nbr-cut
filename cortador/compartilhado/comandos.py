"""Fila de ordens do bot: o único pedido que o Cortador faz é `regerar_indice`.

Quem tem a capacidade de regenerar o índice e publicá-lo no GitHub Pages é o processo do bot; os
demais enfileiram uma ordem em `comandos_bot` e ele executa (formato em CONTRATO.md, igual ao de
agente_filmes/fila_comandos.py). O bot sinaliza que está vivo em `ponto_executores`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from cortador.compartilhado.armazenamento import Armazenamento

FILA_BOT = "comandos_bot"
CHAVE_STATUS = "status_comandos"
CHAVE_PONTO = "ponto_executores"
TIPO_REGERAR_INDICE = "regerar_indice"
# O bot considera o executor vivo se bateu ponto há menos que isto (ele bate a cada 5-10 s).
TOLERANCIA_PONTO_SEGUNDOS = 40


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def executor_vivo(armazenamento: Armazenamento, executor: str = "bot") -> tuple[bool, str | None]:
    pontos = armazenamento.carregar(CHAVE_PONTO, {})
    visto = pontos.get(executor) if isinstance(pontos, dict) else None
    if not visto:
        return False, None
    try:
        quando = datetime.fromisoformat(visto)
    except ValueError:
        return False, visto
    return (datetime.now() - quando).total_seconds() <= TOLERANCIA_PONTO_SEGUNDOS, visto


def enfileirar_regerar_indice(armazenamento: Armazenamento, motivo: str, origem: str = "cortador") -> dict:
    """Cria a ordem `regerar_indice` (status inicial antes da fila, como o bot faz: um "pendente"
    tardio nunca sobrescreve o que o executor já escreveu)."""
    job = {
        "id": uuid.uuid4().hex[:12],
        "tipo": TIPO_REGERAR_INDICE,
        "criado_em": _agora(),
        "origem": origem,
        "criado_por": None,
        "params": {"motivo": motivo},
        "retorno": None,
    }

    def status_inicial(status: dict) -> dict:
        atual = {
            "estado": "pendente", "tipo": TIPO_REGERAR_INDICE, "origem": origem, "chave": None,
            "total": 0, "cursor": 0, "feitos": 0, "falhas": 0, "atualizado_em": _agora(),
        }
        return {**status, job["id"]: atual}

    armazenamento.atualizar(CHAVE_STATUS, {}, status_inicial, criar_se_faltar=True)
    armazenamento.atualizar(
        FILA_BOT, {}, lambda dados: {"jobs": [*dados.get("jobs", []), job]}, criar_se_faltar=True
    )
    return job
