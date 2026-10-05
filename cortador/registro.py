"""Registro de postagens: o mesmo `postagens_publicadas` que o bot grava.

É dele que saem o índice de busca do app Nbr PLAY, a checagem de duplicidade e as correções/exclusões
do painel do bot. Postar direto no canal sem registrar deixaria o filme fora do índice (o monitor só o
descobriria numa varredura posterior). A escrita é atômica (ver `compartilhado.armazenamento`: transação
com FOR UPDATE no Postgres, ou trava de arquivo nos JSON) — nunca um "ler tudo e sobrescrever", que
apagaria o que o bot gravou ao mesmo tempo.

O esquema do registro vem do bot e é vigiado por `tests/test_contrato_bot.py` (ver CONTRATO.md).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from cortador.compartilhado import comandos, formato
from cortador.compartilhado.armazenamento import Armazenamento

CHAVE = "postagens_publicadas"
ORIGEM = "cortador"


class JaPublicado(Exception):
    """O filme já está no registro (mesma chave `tmdb:{id}`)."""

    def __init__(self, chave: str, registro: dict):
        super().__init__(f"{chave} já foi publicado")
        self.chave = chave
        self.registro = registro


def chave_filme(tmdb_id: int | str) -> str:
    """Mesma chave do bot para filmes (`bot._chave_publicacao`)."""
    return f"tmdb:{tmdb_id}"


def montar_registro(
    dados_tmdb: dict,
    nome_arquivo: str,
    message_ids: list[int],
    total_partes: int = 1,
    *,
    agora: datetime | None = None,
) -> dict:
    """Registro igual ao do bot (mesmas chaves), mais `origem`. Para filme em partes junta
    `partes/texto_separado/video_message_id`; `message_ids` deve estar em ORDEM LÓGICA:
    [texto?, parte 1, …, parte N] (não a ordem em que as mensagens foram enviadas)."""
    registro: dict[str, Any] = {
        "tipo": dados_tmdb.get("tipo") or "filme",
        "custom_id": dados_tmdb.get("custom_id"),
        "tmdb_id": dados_tmdb.get("tmdb_id"),
        "serie_tmdb_id": dados_tmdb.get("serie_tmdb_id"),
        "titulo_serie": dados_tmdb.get("titulo_serie"),
        "temporada": dados_tmdb.get("temporada"),
        "episodio": dados_tmdb.get("episodio"),
        "titulo": dados_tmdb.get("titulo"),
        "ano": dados_tmdb.get("ano"),
        "nome_arquivo": nome_arquivo,
        "file_id": None,  # não há file_id do Bot API: o upload foi pela conta de usuário
        "message_ids": list(message_ids),
        "publicado_em": (agora or datetime.now()).isoformat(timespec="seconds"),
        "origem": ORIGEM,
    }
    if total_partes > 1:
        registro.update(formato.campos_registro(total_partes, list(message_ids)))
    return registro


def ja_publicado(armazenamento: Armazenamento, tmdb_id: int | str) -> dict | None:
    """Registro existente do filme, ou None."""
    dados = armazenamento.carregar(CHAVE, {})
    return dados.get(chave_filme(tmdb_id)) if isinstance(dados, dict) else None


def registrar(
    armazenamento: Armazenamento,
    dados_tmdb: dict,
    nome_arquivo: str,
    message_ids: list[int],
    total_partes: int = 1,
    *,
    agora: datetime | None = None,
) -> tuple[str, dict]:
    """Grava o filme no registro (atômico). Levanta [JaPublicado] se a chave já existir — a
    checagem é feita dentro da trava, então dois processos não gravam o mesmo filme."""
    chave = chave_filme(dados_tmdb["tmdb_id"])
    registro = montar_registro(dados_tmdb, nome_arquivo, message_ids, total_partes, agora=agora)

    def alterar(dados: dict) -> dict:
        if chave in dados:
            raise JaPublicado(chave, dados[chave])
        return {**dados, chave: registro}

    armazenamento.atualizar(CHAVE, {}, alterar)
    return chave, registro


def enfileirar_indice(armazenamento: Armazenamento, motivo: str) -> str:
    """Pede ao bot para regenerar o índice do app. Devolve 'enfileirado' ou 'bot_parado' (a ordem
    fica na fila e roda quando o bot subir — o monitor também enfileira ao sincronizar)."""
    ativo, _ = comandos.executor_vivo(armazenamento)
    comandos.enfileirar_regerar_indice(armazenamento, motivo)
    return "enfileirado" if ativo else "bot_parado"
