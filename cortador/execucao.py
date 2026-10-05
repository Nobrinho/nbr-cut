"""Ponte entre a interface Tk (thread principal) e o Telethon (asyncio).

Telethon precisa de UM loop asyncio vivo durante toda a sessão. Aqui esse loop roda numa thread
própria; a interface agenda corrotinas nele com [LoopDeFundo.rodar] e recebe o resultado por
callback na thread da interface (via [Despachante], que a UI implementa com `after()`).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Awaitable, Callable


class LoopDeFundo:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._executar, name="cortador-asyncio", daemon=True)
        self._thread.start()

    def _executar(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def rodar(self, corotina: Awaitable) -> concurrent.futures.Future:
        """Agenda [corotina] no loop de fundo; devolve um Future (thread-safe)."""
        return asyncio.run_coroutine_threadsafe(corotina, self._loop)  # type: ignore[arg-type]

    def esperar(self, corotina: Awaitable, timeout: float | None = None) -> Any:
        """Bloqueante — só para scripts/CLI e testes, nunca na thread da interface."""
        return self.rodar(corotina).result(timeout)

    def parar(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()


class Despachante:
    """Entrega funções para a thread da interface. A UI passa `lambda f: janela.after(0, f)`."""

    def __init__(self, entregar: Callable[[Callable[[], None]], Any]):
        self._entregar = entregar

    def __call__(self, funcao: Callable[[], None]) -> None:
        self._entregar(funcao)


def ao_terminar(futuro: concurrent.futures.Future, despachante: Despachante,
                sucesso: Callable[[Any], None], falha: Callable[[BaseException], None]) -> None:
    """Quando [futuro] acabar, chama [sucesso] ou [falha] NA THREAD DA INTERFACE."""

    def pronto(f: concurrent.futures.Future) -> None:
        try:
            resultado = f.result()
        except BaseException as erro:  # noqa: BLE001 — repassada para a interface
            # `erro` deixa de existir ao sair do except: o callback roda depois, na thread da UI.
            despachante(lambda erro=erro: falha(erro))
        else:
            despachante(lambda: sucesso(resultado))

    futuro.add_done_callback(pronto)


class Cancelamento:
    """Bandeira de cancelamento compartilhada entre a interface e o upload (`cancelado()`)."""

    def __init__(self) -> None:
        self._evento = threading.Event()

    def cancelar(self) -> None:
        self._evento.set()

    def zerar(self) -> None:
        self._evento.clear()

    def __call__(self) -> bool:
        return self._evento.is_set()
