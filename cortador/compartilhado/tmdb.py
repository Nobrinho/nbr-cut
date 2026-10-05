"""Cliente mínimo do TMDB para FILMES: busca de candidatos e detalhes completos.

CÓPIA dos trechos que o Cortador usa de agente_filmes/tmdb_client.py (copiado em 2026-10-04).
A fonte da verdade é o bot; o contrato é vigiado por tests/test_contrato_bot.py
(roda quando o repo do bot está ao lado e pula quando não está). Se o bot mudar, atualize aqui.
Fora da cópia: séries/episódios e o plano B por OMDb/Wikidata.
"""
import re
from urllib.parse import urlparse

import requests


TMDB_BASE_URL = "https://api.themoviedb.org/3"


TMDB_POSTER_URL = "https://image.tmdb.org/t/p/w780"


TMDB_BACKDROP_URL = "https://image.tmdb.org/t/p/w1280"


TMDB_PROFILE_URL = "https://image.tmdb.org/t/p/w185"


QTD_ELENCO = 5  # quantos atores trazer no campo "Elenco"


QTD_TAGS = 3    # quantas palavras-chave trazer no campo "Tags"


class FilmeNaoEncontrado(Exception):
    pass


def extrair_id_filme_tmdb(entrada: str | None) -> int | None:
    """Reconhece URL/ID explícito do TMDB sem confundir títulos como '1984'."""
    texto = (entrada or "").strip()
    if not texto:
        return None

    formato_explicito = re.fullmatch(r"(?i)(?:tmdb\s*:|id\s*:?)\s*(\d+)", texto)
    if formato_explicito:
        filme_id = int(formato_explicito.group(1))
        if filme_id <= 0:
            raise ValueError("O ID do TMDB deve ser maior que zero.")
        return filme_id

    # Números de até quatro dígitos podem ser títulos (por exemplo, "1984").
    if texto.isdigit() and len(texto) > 4:
        filme_id = int(texto)
        if filme_id <= 0:
            raise ValueError("O ID do TMDB deve ser maior que zero.")
        return filme_id

    url = urlparse(texto)
    if url.scheme.lower() not in {"http", "https"}:
        return None

    host = (url.hostname or "").lower()
    if host not in {"themoviedb.org", "www.themoviedb.org"}:
        return None

    partes = [parte for parte in url.path.split("/") if parte]
    if len(partes) < 2 or partes[0].lower() != "movie":
        raise ValueError("Use uma URL de filme do TMDB no formato /movie/ID.")

    id_na_url = re.match(r"^(\d+)(?:-|$)", partes[1])
    if not id_na_url:
        raise ValueError("Não encontrei um ID de filme válido nessa URL do TMDB.")
    filme_id = int(id_na_url.group(1))
    if filme_id <= 0:
        raise ValueError("O ID do TMDB deve ser maior que zero.")
    return filme_id


def _extrair_diretor(credits: dict) -> str | None:
    for pessoa in credits.get("crew", []):
        if pessoa.get("job") == "Director":
            return pessoa.get("name")
    return None


def _extrair_elenco(credits: dict) -> list[tuple[str, str | None]]:
    elenco = []
    for pessoa in credits.get("cast", [])[:QTD_ELENCO]:
        foto = f"{TMDB_PROFILE_URL}{pessoa['profile_path']}" if pessoa.get("profile_path") else None
        elenco.append((pessoa["name"], foto))
    return elenco


def _extrair_classificacao(release_dates: dict) -> str | None:
    """Procura a certificação etária, priorizando Brasil (BR) e caindo pra EUA (US)."""
    paises = {p["iso_3166_1"]: p for p in release_dates.get("results", [])}
    for sigla in ("BR", "US"):
        pais = paises.get(sigla)
        if not pais:
            continue
        for release in pais.get("release_dates", []):
            cert = release.get("certification")
            if cert:
                return cert
    return None


def _extrair_trailer(videos: dict) -> str | None:
    for video in videos.get("results", []):
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None


def _extrair_tags(keywords: dict) -> list[str]:
    lista = keywords.get("keywords") or keywords.get("results") or []
    return [k["name"] for k in lista[:QTD_TAGS]]


def _detalhar_filme(filme_id: int, api_key: str) -> dict:
    """Busca os detalhes completos de um filme específico."""
    resp_detalhes = requests.get(
        f"{TMDB_BASE_URL}/movie/{filme_id}",
        params={
            "api_key": api_key,
            "language": "pt-BR",
            "append_to_response": "credits,release_dates,videos,keywords",
        },
        timeout=10,
    )
    resp_detalhes.raise_for_status()
    filme = resp_detalhes.json()

    ano = filme["release_date"][:4] if filme.get("release_date") else None
    colecao = filme.get("belongs_to_collection")
    pais = filme["production_countries"][0]["name"] if filme.get("production_countries") else None
    estudio = filme["production_companies"][0]["name"] if filme.get("production_companies") else None

    return {
        "tmdb_id": filme["id"],
        "titulo": filme.get("title"),
        "titulo_original": filme.get("original_title"),
        "ano": ano,
        "duracao": filme.get("runtime"),
        "nota": filme.get("vote_average"),
        "classificacao": _extrair_classificacao(filme.get("release_dates", {})),
        "generos": [g["name"] for g in filme.get("genres", [])],
        "colecao": colecao["name"] if colecao else None,
        "pais": pais,
        "diretor": _extrair_diretor(filme.get("credits", {})),
        "estudio": estudio,
        "elenco": _extrair_elenco(filme.get("credits", {})),
        "poster_url": f"{TMDB_POSTER_URL}{filme['poster_path']}" if filme.get("poster_path") else None,
        "backdrop_url": f"{TMDB_BACKDROP_URL}{filme['backdrop_path']}" if filme.get("backdrop_path") else None,
        "trailer_url": _extrair_trailer(filme.get("videos", {})),
        "tags": _extrair_tags(filme.get("keywords", {})),
        "sinopse": filme.get("overview") or "Sinopse não disponível.",
    }


def detalhar_filme(filme_id: int, api_key: str) -> dict:
    """Busca os detalhes completos de um filme pelo ID do TMDB."""
    try:
        return _detalhar_filme(int(filme_id), api_key)
    except requests.HTTPError as erro:
        if erro.response is not None and erro.response.status_code == 404:
            raise FilmeNaoEncontrado(f"Filme TMDB {filme_id} não encontrado.") from erro
        raise


def buscar_resultados_filmes(titulo: str, api_key: str, ano: str | None = None) -> list[dict]:
    """Candidatos leves do TMDB (uma chamada, sem detalhar cada um).

    DIVERGÊNCIA INTENCIONAL do bot: sem o plano B por OMDb/Wikidata quando o TMDB não devolve nada
    (o Cortador tem busca manual por título/ano/`tmdb:ID` para esse caso)."""
    params = {"api_key": api_key, "query": titulo, "language": "pt-BR"}
    if ano:
        params["year"] = ano
    resposta = requests.get(f"{TMDB_BASE_URL}/search/movie", params=params, timeout=10)
    resposta.raise_for_status()
    return resposta.json().get("results", [])
