"""Legenda rica do post de FILME (o app Nbr PLAY lê estes rótulos).

CÓPIA dos trechos que o Cortador usa de agente_filmes/legenda.py (copiado em 2026-10-04).
A fonte da verdade é o bot; o contrato é vigiado por tests/test_contrato_bot.py
(roda quando o repo do bot está ao lado e pula quando não está). Se o bot mudar, atualize aqui.
Fora da cópia: legenda de episódio.
"""
from datetime import date
from typing import Optional


# Telegram limita legenda de MÍDIA (foto/vídeo) a 1024 caracteres; mensagem de
# TEXTO aceita 4096. A legenda NÃO é mais cortada: montamos ela inteira e
# sinalizamos em `cabe_na_legenda_de_midia` se dá pra postar como caption do
# vídeo (1 mensagem) ou se o bot deve mandar em 2 mensagens (texto + vídeo).
LIMITE_LEGENDA_MIDIA = 1024


def _formatar_elenco(elenco: list[tuple[str, Optional[str]]]) -> str:
    if not elenco:
        return "Não informado"
    partes = [f"{nome}::{foto}" if foto else nome for nome, foto in elenco]
    return "; ".join(partes)


def _categoria_por_ano(ano: Optional[str]) -> str:
    """A TMDB não tem um campo de 'categoria' de catálogo — isso é uma decisão
    de negócio (o que é destaque/lançamento no SEU canal). Como aproximação
    automática: filme do ano corrente ou do ano anterior vira "Lançamentos",
    o resto vira "Catálogo". Dá pra trocar por uma lista fixa ou uma opção
    manual mais pra frente se essa regra não bater com o que você quer.
    """
    try:
        ano_int = int(ano)
    except (TypeError, ValueError):
        return "Catálogo"
    return "Lançamentos" if ano_int >= date.today().year - 1 else "Catálogo"


def montar_legenda_filme(dados: dict, audio: Optional[str] = None, qualidade: Optional[str] = None) -> str:
    # Só entram linhas com valor real — campos sem dado são OMITIDOS (o app esconde
    # o que faltar; "Não informado" na tela ficaria feio).
    linhas: list[str] = [f"Título: {dados['titulo']}"]

    def add(rotulo: str, valor) -> None:
        if valor not in (None, "", []):
            linhas.append(f"{rotulo} {valor}")

    original = dados.get("titulo_original")
    if original and original != dados.get("titulo"):
        add("Original:", original)
    eh_serie = dados.get("tipo") == "serie"
    linhas.append("Tipo: Série" if eh_serie else "Tipo: Filme")
    add("Ano:", dados.get("ano"))
    add("Duração:", dados.get("duracao"))
    add("Nota:", f"{dados['nota']:.1f}" if dados.get("nota") else None)
    add("Classificação:", dados.get("classificacao"))
    add("Gêneros:", ", ".join(dados["generos"]) if dados.get("generos") else None)
    linhas.append("Categoria: Séries" if eh_serie else f"Categoria: {_categoria_por_ano(dados.get('ano'))}")
    add("Coleção:", dados.get("colecao"))
    add("País:", dados.get("pais"))
    add("Áudio:", audio)
    if qualidade and qualidade != "Qualidade não informada":
        add("Qualidade:", qualidade)
    add("Diretor:", dados.get("diretor"))
    add("Estúdio:", dados.get("estudio"))
    add("Elenco:", _formatar_elenco(dados["elenco"]) if dados.get("elenco") else None)
    add("Pôster:", dados.get("poster_url"))
    add("Fundo:", dados.get("backdrop_url"))
    add("Trailer:", dados.get("trailer_url"))
    add("TMDB:", dados.get("tmdb_id"))
    add("Tags:", ", ".join(dados["tags"]) if dados.get("tags") else None)
    add("Sinopse:", dados.get("sinopse"))
    return "\n".join(linhas)
