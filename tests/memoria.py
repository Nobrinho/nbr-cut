"""Armazenamento em memória para os testes: mesma interface e semântica do `Armazenamento` real."""
from __future__ import annotations

import copy
from typing import Any, Callable

from cortador.compartilhado.armazenamento import Armazenamento, ChaveInexistente


class ArmazenamentoEmMemoria(Armazenamento):
    def __init__(self, inicial: dict[str, Any] | None = None):
        super().__init__(database_url="postgresql://em-memoria")  # só para `ativo` ser True
        self.dados: dict[str, Any] = copy.deepcopy(inicial or {})
        self.escritas: list[str] = []

    def carregar(self, chave: str, padrao: Any) -> Any:
        valor = self.dados.get(chave, padrao)
        return copy.deepcopy(valor if isinstance(valor, type(padrao)) else padrao)

    def atualizar(self, chave: str, padrao: Any, alterar: Callable[[Any], Any], *, criar_se_faltar: bool = False) -> Any:
        if chave not in self.dados:
            if not criar_se_faltar:
                raise ChaveInexistente(chave)
            self.dados[chave] = copy.deepcopy(padrao)
        atual = self.dados[chave]
        novo = alterar(copy.deepcopy(atual if isinstance(atual, type(padrao)) else padrao))
        self.dados[chave] = novo  # só chega aqui se `alterar` não levantou
        self.escritas.append(chave)
        return copy.deepcopy(novo)

    def testar(self) -> None:
        return None
