"""Nome `.partNNofMM` das partes e campos do registro de um filme em partes.

CÓPIA dos trechos que o Cortador usa de agente_filmes/partes.py (copiado em 2026-10-04).
A fonte da verdade é o bot; o contrato é vigiado por tests/test_contrato_bot.py
(roda quando o repo do bot está ao lado e pula quando não está). Se o bot mudar, atualize aqui.
Mesmo formato do app Android (PartName.kt) e do cortador de linha de comando.
"""
import re
from dataclasses import dataclass


# Menos que isso não é um arquivo dividido.
MIN_PARTES = 2


# Mesma regra de PartName.parse (Kotlin) e de tools/ntv2_split.py: índice 1..total, total >= 2.
_RE_PARTE = re.compile(r"(.+)\.part([0-9]+)of([0-9]+)", re.IGNORECASE)


@dataclass(frozen=True)
class NomeParte:
    base: str    # nome do arquivo inteiro, com a extensão (Filme.2020.mkv)
    indice: int  # 1..total, como no nome
    total: int


def interpretar_nome(nome: str | None) -> NomeParte | None:
    """Interpreta `Filme.mkv.part03of11`; None se não for nome de parte ou for inválido."""
    encontrado = _RE_PARTE.fullmatch((nome or "").strip())
    if not encontrado:
        return None
    indice, total = int(encontrado.group(2)), int(encontrado.group(3))
    if total < MIN_PARTES or not 1 <= indice <= total:
        return None
    return NomeParte(encontrado.group(1), indice, total)


def formatar_nome(base: str, indice: int, total: int) -> str:
    largura = max(2, len(str(total)))
    return f"{base}.part{indice:0{largura}d}of{total:0{largura}d}"


def campos_registro(total_partes: int, message_ids: list[int]) -> dict:
    """Campos extras de `postagens_publicadas` para um post em partes.

    `message_ids` = [texto separado?] + as partes em ordem (todos precisam estar lá: é o que
    apagar/duplicados usam). `video_message_id` é a parte 1, que carrega a legenda e é o que o
    app resolve; `texto_separado` diz se há mensagem de texto antes (o layout de 2 mensagens).
    """
    if total_partes < 1 or len(message_ids) < total_partes:
        return {}
    return {
        "partes": total_partes,
        "texto_separado": len(message_ids) > total_partes,
        "video_message_id": message_ids[len(message_ids) - total_partes],
    }


def tem_texto_separado(registro: dict) -> bool:
    """O post tem uma mensagem de texto com os metadados antes do vídeo (layout texto+vídeo)?

    Antes das partes bastava contar: 2 ids = texto + vídeo. Com partes há N ids no layout de uma
    mensagem, então quem diz é `texto_separado`.
    """
    if registro.get("partes"):
        return bool(registro.get("texto_separado"))
    return len(registro.get("message_ids") or []) >= 2


def id_do_video(registro: dict) -> int | None:
    """Mensagem que o app abre: a parte 1 num filme em partes, senão a mais recente do post."""
    explicito = registro.get("video_message_id")
    if explicito:
        return explicito
    ids = registro.get("message_ids") or []
    return max(ids) if ids else None
