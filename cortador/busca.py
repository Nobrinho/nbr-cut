"""Busca do filme no TMDB para identificar o arquivo (a "procura" do Cortador).

Fluxo: o nome do arquivo gera uma sugestão (título + ano); `buscar` devolve candidatos baratos
(1 chamada à API, sem detalhes); ao escolher um, `detalhar` traz os dados completos (1 chamada)
que alimentam a legenda e o registro. O usuário também pode digitar `tmdb:ID` ou colar a URL
do filme — `resolver_entrada` reconhece.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from cortador.compartilhado import tmdb as tmdb_client
from cortador.compartilhado.tmdb import FilmeNaoEncontrado

TMDB_MINIATURA = "https://image.tmdb.org/t/p/w92"

Buscador = Callable[..., list]
Detalhador = Callable[..., dict]


@dataclass(frozen=True)
class Candidato:
    tmdb_id: int
    titulo: str
    titulo_original: str | None
    ano: str | None
    poster_path: str | None

    @property
    def miniatura_url(self) -> str | None:
        return f"{TMDB_MINIATURA}{self.poster_path}" if self.poster_path else None

    @property
    def rotulo(self) -> str:
        """'Duna: Parte Dois (2024)' — o que a lista mostra."""
        ano = f" ({self.ano})" if self.ano else ""
        original = ""
        if self.titulo_original and self.titulo_original != self.titulo:
            original = f" — {self.titulo_original}"
        return f"{self.titulo}{ano}{original}"


def _candidato(bruto: dict) -> Optional[Candidato]:
    try:
        tmdb_id = int(bruto["id"])
    except (KeyError, TypeError, ValueError):
        return None
    data = (bruto.get("release_date") or "").strip()
    return Candidato(
        tmdb_id=tmdb_id,
        titulo=bruto.get("title") or bruto.get("original_title") or f"Filme {tmdb_id}",
        titulo_original=bruto.get("original_title"),
        ano=data[:4] if len(data) >= 4 and data[:4].isdigit() else None,
        poster_path=bruto.get("poster_path"),
    )


def resolver_entrada(entrada: str | None) -> int | None:
    """Id do TMDB se o texto for `tmdb:123`, uma URL `/movie/123` ou um número longo; senão None
    (então é um título para buscar)."""
    return tmdb_client.extrair_id_filme_tmdb(entrada)


def buscar(
    termo: str,
    api_key: str,
    ano: str | None = None,
    *,
    buscador: Buscador = tmdb_client.buscar_resultados_filmes,
    limite: int = 12,
) -> list[Candidato]:
    """Candidatos para [termo] (e [ano], se houver; sem resultado com o ano, tenta sem ele).
    Lista vazia se nada for encontrado ou o termo estiver em branco."""
    termo = (termo or "").strip()
    if not termo:
        return []
    brutos = buscador(termo, api_key, ano) if ano else buscador(termo, api_key)
    if not brutos and ano:
        brutos = buscador(termo, api_key)
    vistos: set[int] = set()
    candidatos: list[Candidato] = []
    for bruto in brutos or []:
        c = _candidato(bruto)
        if c and c.tmdb_id not in vistos:
            vistos.add(c.tmdb_id)
            candidatos.append(c)
    return candidatos[:limite]


def detalhar(tmdb_id: int, api_key: str, *, detalhador: Detalhador = tmdb_client.detalhar_filme) -> dict:
    """Dados completos do filme (as chaves que `legenda.montar_legenda_filme` espera)."""
    return detalhador(tmdb_id, api_key)


__all__ = ["Candidato", "FilmeNaoEncontrado", "buscar", "detalhar", "resolver_entrada"]
