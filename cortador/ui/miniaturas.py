"""Pôsteres da lista de candidatos: baixa em segundo plano e guarda em memória.

O download roda fora da thread da interface (nunca trava a janela); a criação da imagem do Tk
acontece de volta na thread da interface, por quem chama.
"""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import requests
from PIL import Image

_cache: dict[str, Image.Image | None] = {}
_trava = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="miniaturas")


def obter(url: str | None, quando_pronta: Callable[[Image.Image | None], None]) -> None:
    """Chama [quando_pronta] (em OUTRA thread) com a imagem — ou None se não deu para baixar.
    Quem usa o resultado em widgets precisa devolvê-lo à thread da interface (ex.: `after`)."""
    if not url:
        quando_pronta(None)
        return
    with _trava:
        if url in _cache:
            imagem = _cache[url]
            pronta = True
        else:
            pronta = False
    if pronta:
        quando_pronta(imagem)
        return

    def baixar() -> None:
        try:
            resposta = requests.get(url, timeout=10)
            resposta.raise_for_status()
            imagem = Image.open(io.BytesIO(resposta.content)).convert("RGB")
        except Exception:  # noqa: BLE001 — sem pôster o card só fica sem imagem
            imagem = None
        with _trava:
            _cache[url] = imagem
        quando_pronta(imagem)

    _pool.submit(baixar)
