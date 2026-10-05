"""Segredos do app (hash da API do Telegram, chave do TMDB, URL do banco) protegidos no disco.

No Windows usa a DPAPI (CryptProtectData): o texto só é decifrável pelo MESMO usuário do Windows,
nesta máquina — sem senha para guardar e sem dependência extra. Fora do Windows (só desenvolvimento)
cai num esquema NÃO seguro, marcado com o prefixo `texto:`, só para o app não quebrar.
"""
from __future__ import annotations

import base64
import sys

_PREFIXO_DPAPI = "dpapi:"
_PREFIXO_TEXTO = "texto:"


class SegredoIlegivel(Exception):
    """O valor protegido não pôde ser lido (outro usuário/máquina do Windows, ou arquivo corrompido)."""


def _dpapi(dados: bytes, protegendo: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(dados, len(dados))
    entrada = _Blob(len(dados), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    saida = _Blob()
    funcao = crypt32.CryptProtectData if protegendo else crypt32.CryptUnprotectData
    if protegendo:
        ok = funcao(ctypes.byref(entrada), "NbrCortador", None, None, None, 0, ctypes.byref(saida))
    else:
        ok = funcao(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(saida))
    if not ok:
        raise SegredoIlegivel("A DPAPI não conseguiu processar o segredo")
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        kernel32.LocalFree(saida.pbData)


def proteger(texto: str) -> str:
    """Texto → valor seguro para gravar no config.json."""
    dados = texto.encode("utf-8")
    if sys.platform == "win32":
        return _PREFIXO_DPAPI + base64.b64encode(_dpapi(dados, True)).decode("ascii")
    return _PREFIXO_TEXTO + base64.b64encode(dados).decode("ascii")


def revelar(valor: str) -> str:
    """Inverso de [proteger]. Levanta [SegredoIlegivel] se não der para ler."""
    try:
        if valor.startswith(_PREFIXO_DPAPI):
            if sys.platform != "win32":
                raise SegredoIlegivel("Segredo protegido pela DPAPI do Windows; não dá para ler neste sistema")
            return _dpapi(base64.b64decode(valor[len(_PREFIXO_DPAPI):], validate=True), False).decode("utf-8")
        if valor.startswith(_PREFIXO_TEXTO):
            return base64.b64decode(valor[len(_PREFIXO_TEXTO):], validate=True).decode("utf-8")
    except SegredoIlegivel:
        raise
    except Exception as erro:  # base64/UTF-8 inválido, blob corrompido…
        raise SegredoIlegivel(f"Segredo corrompido: {erro}") from erro
    raise SegredoIlegivel("Formato de segredo desconhecido")
