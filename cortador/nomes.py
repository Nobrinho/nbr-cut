"""Padronização do nome do arquivo: `Título (Ano) [Qualidade].ext`.

O nome vira o nome do arquivo no Telegram (e a base de `.partNNofMM`). O app e o bot tiram o
título da legenda quando existe; o nome só importa de fallback e para humanos — então vale ser
legível e seguro em qualquer sistema de arquivos.
"""
from __future__ import annotations

import os
import re
import unicodedata

from cortador.compartilhado.extrator import extrair_ano, extrair_audio, extrair_qualidade, extrair_titulo

# Caracteres que o Windows não aceita em nome de arquivo (e controles).
_INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVADOS = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

# Limite seguro para o nome completo (o sufixo `.part01of11` e a extensão entram depois).
MAX_TITULO = 120

_RESOLUCOES = ("2160p", "4k", "1080p", "720p", "480p")
_FONTES = ("remux", "bluray", "blu-ray", "web-dl", "webdl", "webrip", "hdrip", "dvdrip", "hdtv")


def sanitizar(texto: str) -> str:
    """Tira o que não pode ir num nome de arquivo, mantendo acentos. Dois-pontos viram ' -'
    ('Duna: Parte 2' → 'Duna - Parte 2')."""
    t = unicodedata.normalize("NFC", texto or "")
    t = re.sub(r"\s*:\s*", " - ", t)
    t = _INVALIDOS.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if t.lower() in _RESERVADOS:
        t = f"_{t}"
    return t


def qualidade_curta(qualidade: str | None) -> str | None:
    """De 'Dublado, 1080p, BluRay, AC3' fica '1080p BluRay' (resolução + origem). Termos de áudio,
    codec e idioma ficam de fora: vão na legenda, não no nome do arquivo."""
    if not qualidade or qualidade == "Qualidade não informada":
        return None
    termos = [t.strip() for t in qualidade.split(",") if t.strip()]
    por_chave = {t.lower(): t for t in termos}
    resolucao = next((por_chave[r] for r in _RESOLUCOES if r in por_chave), None)
    fonte = next((por_chave[f] for f in _FONTES if f in por_chave), None)
    partes = [p for p in (resolucao, fonte) if p]
    return " ".join(partes) or None


def extensao_de(caminho: str) -> str:
    """Extensão em minúsculas, com ponto ('.mkv'); vazia se não houver."""
    return os.path.splitext(caminho)[1].lower()


def nome_padronizado(titulo: str, ano: str | int | None, qualidade: str | None, extensao: str) -> str:
    """`Título (Ano) [Qualidade].ext`; o que faltar (ano, qualidade) simplesmente não aparece."""
    base = sanitizar(titulo)[:MAX_TITULO].strip(" .") or "Filme"
    if ano:
        base += f" ({str(ano).strip()})"
    curta = qualidade_curta(qualidade)
    if curta:
        base += f" [{sanitizar(curta)}]"
    ext = (extensao or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return base + ext


def sugestao_do_arquivo(caminho: str) -> dict:
    """O que dá para tirar só do nome do arquivo (ponto de partida da busca no TMDB)."""
    nome = os.path.basename(caminho)
    return {
        "titulo": extrair_titulo(None, nome),
        "ano": extrair_ano(nome),
        "qualidade": extrair_qualidade(nome),
        "audio": extrair_audio(nome),
        "extensao": extensao_de(nome),
    }
