"""Armazenamento compartilhado com o bot: a tabela `app_state` do Postgres (ou os JSON dele).

O bot guarda o estado em `app_state(key, value JSONB)` — registro de postagens, fila de ordens,
sinal de vida dos processos. Este módulo fala o MESMO formato (ver CONTRATO.md) sem importar nada
do bot. Duas formas de acesso, conforme o bot esteja configurado:

- Postgres (`database_url`): o caso normal (docker-compose do bot).
- Arquivos (`pasta`): o bot sem banco guarda `<chave>.json` na pasta dele.

Sem nenhuma das duas, [ativo] é False e o app posta sem registrar (o monitor do bot sincroniza
o canal depois).

Escrita sempre atômica: transação com `SELECT … FOR UPDATE` no Postgres; trava de arquivo +
substituição atômica nos JSON. Nunca um "ler tudo e sobrescrever" sem trava: apagaria o que o bot
gravou ao mesmo tempo.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class ArmazenamentoIndisponivel(Exception):
    """Não foi possível falar com o banco/arquivo do bot (Docker parado, URL errada, pasta sumida)."""


class ChaveInexistente(Exception):
    """A chave ainda não existe no armazenamento do bot e [criar_se_faltar] era False.

    Importante para o registro de postagens: se o app o criasse vazio, o bot deixaria de migrar o
    histórico dele (ele só migra quando a chave não existe)."""


class Armazenamento:
    def __init__(self, database_url: str = "", pasta: str | os.PathLike | None = None):
        self.database_url = (database_url or "").strip()
        self.pasta = Path(pasta) if pasta else None
        self._tabelas_prontas = False

    @property
    def usa_banco(self) -> bool:
        return bool(self.database_url)

    @property
    def ativo(self) -> bool:
        return self.usa_banco or self.pasta is not None

    @property
    def descricao(self) -> str:
        if self.usa_banco:
            return "Postgres do bot"
        if self.pasta is not None:
            return f"arquivos em {self.pasta}"
        return "não configurado"

    # ------------------------------------------------------------------ API

    def carregar(self, chave: str, padrao: Any) -> Any:
        """Valor da chave, ou [padrao] se não existir (ou for de outro tipo)."""
        self._exigir_ativo()
        if self.usa_banco:
            return self._banco_carregar(chave, padrao)
        return self._arquivo_ler(chave, padrao)

    def atualizar(
        self, chave: str, padrao: Any, alterar: Callable[[Any], Any], *, criar_se_faltar: bool = False
    ) -> Any:
        """Lê, aplica [alterar] e grava, tudo sob trava. Exceções de [alterar] cancelam a escrita."""
        self._exigir_ativo()
        if self.usa_banco:
            return self._banco_atualizar(chave, padrao, alterar, criar_se_faltar)
        return self._arquivo_atualizar(chave, padrao, alterar, criar_se_faltar)

    def testar(self) -> None:
        """Levanta [ArmazenamentoIndisponivel] se não der para ler (usado no indicador da tela)."""
        self._exigir_ativo()
        self.carregar("postagens_publicadas", {})

    # ------------------------------------------------------------------ Postgres

    def _exigir_ativo(self) -> None:
        if not self.ativo:
            raise ArmazenamentoIndisponivel("Registro do bot não configurado")

    def _conectar(self):
        """Uma conexão por operação (poucas por filme; sem pool). Injetável nos testes."""
        try:
            import psycopg

            return psycopg.connect(self.database_url, connect_timeout=5, autocommit=True)
        except Exception as erro:  # noqa: BLE001
            raise ArmazenamentoIndisponivel(f"Não consegui conectar ao banco do bot: {erro}") from erro

    def _transacao(self):
        """Conexão + transação com os mesmos limites de tempo do bot."""
        from contextlib import contextmanager

        import psycopg

        @contextmanager
        def abrir():
            conexao = self._conectar()
            try:
                with conexao.transaction():
                    conexao.execute("SET LOCAL statement_timeout = '5s'")
                    conexao.execute("SET LOCAL lock_timeout = '3s'")
                    yield conexao
            except (psycopg.Error, OSError) as erro:
                # Só falhas do BANCO/rede viram "indisponível". O que a função de alteração levantar
                # (ex.: JaPublicado) passa intacto — e a transação já foi desfeita ao sair do `with`.
                raise ArmazenamentoIndisponivel(f"Falha no banco do bot: {erro}") from erro
            finally:
                conexao.close()

        return abrir()

    def _garantir_tabela(self, conexao) -> None:
        if self._tabelas_prontas:
            return
        conexao.execute(
            "CREATE TABLE IF NOT EXISTS app_state ("
            "key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        self._tabelas_prontas = True

    def _banco_carregar(self, chave: str, padrao: Any) -> Any:
        with self._transacao() as conexao:
            self._garantir_tabela(conexao)
            linha = conexao.execute("SELECT value FROM app_state WHERE key = %s", (chave,)).fetchone()
        if not linha:
            return padrao
        valor = linha[0]
        return valor if isinstance(valor, type(padrao)) else padrao

    def _banco_atualizar(self, chave: str, padrao: Any, alterar, criar_se_faltar: bool) -> Any:
        with self._transacao() as conexao:
            self._garantir_tabela(conexao)
            if criar_se_faltar:
                conexao.execute(
                    "INSERT INTO app_state (key, value) VALUES (%s, %s::jsonb) ON CONFLICT (key) DO NOTHING",
                    (chave, json.dumps(padrao, ensure_ascii=False, default=str)),
                )
            linha = conexao.execute("SELECT value FROM app_state WHERE key = %s FOR UPDATE", (chave,)).fetchone()
            if not linha:
                raise ChaveInexistente(chave)
            valor = linha[0]
            novo = alterar(valor if isinstance(valor, type(padrao)) else padrao)
            conexao.execute(
                "UPDATE app_state SET value = %s::jsonb, updated_at = NOW() WHERE key = %s",
                (json.dumps(novo, ensure_ascii=False, default=str), chave),
            )
            return novo

    # ------------------------------------------------------------------ arquivos JSON do bot

    def _caminho(self, chave: str) -> Path:
        return self.pasta / f"{chave}.json"  # type: ignore[operator]

    def _arquivo_ler(self, chave: str, padrao: Any) -> Any:
        caminho = self._caminho(chave)
        if not caminho.exists():
            return padrao
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return padrao
        return dados if isinstance(dados, type(padrao)) else padrao

    def _arquivo_atualizar(self, chave: str, padrao: Any, alterar, criar_se_faltar: bool) -> Any:
        from filelock import FileLock, Timeout

        caminho = self._caminho(chave)
        if not caminho.exists() and not criar_se_faltar:
            raise ChaveInexistente(chave)
        try:
            caminho.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(caminho) + ".lock", timeout=5):
                novo = alterar(self._arquivo_ler(chave, padrao))
                temporario = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=caminho.parent, delete=False, suffix=".tmp"
                    ) as arquivo:
                        temporario = Path(arquivo.name)
                        json.dump(novo, arquivo, ensure_ascii=False, indent=2, default=str)
                    os.replace(temporario, caminho)
                finally:
                    if temporario and temporario.exists():
                        temporario.unlink()
                return novo
        except Timeout as erro:
            raise ArmazenamentoIndisponivel(f"{caminho.name} está travado por outro processo") from erro
        except OSError as erro:
            raise ArmazenamentoIndisponivel(f"Não consegui gravar {caminho}: {erro}") from erro
