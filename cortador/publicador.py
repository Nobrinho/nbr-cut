"""Orquestra a postagem de um filme no canal, sem saber nada de Telegram nem de banco.

Fala com o mundo por duas interfaces pequenas (`Enviador`, `Registro`), o que permite testar toda a
ordem de envio, retomada, cancelamento e registro com falsos.

ORDEM DE ENVIO (de propósito): partes N…2, depois o texto de metadados (se a legenda não couber em
1024 caracteres) e por ÚLTIMO a parte 1. O app só mostra o card da parte 1, então o filme só aparece
quando está completo; uma falha no meio deixa partes órfãs invisíveis (e o monitor ignora grupos
incompletos). O texto fica logo antes da parte 1 — é assim que o app o casa com o vídeo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from cortador import jobs, registro as registro_mod
from cortador.jobs import Job, ParteJob, RepositorioDeJobs
from cortador.compartilhado.armazenamento import Armazenamento
from cortador.compartilhado.legenda import LIMITE_LEGENDA_MIDIA, montar_legenda_filme

Progresso = Callable[[int, int], None]


class PublicacaoCancelada(Exception):
    """O usuário cancelou; o estado fica salvo e dá para retomar."""


class Enviador(Protocol):
    async def enviar_parte(
        self,
        parte: ParteJob,
        caminho: str,
        legenda: str | None,
        como_video: bool,
        progresso: Progresso,
        duracao_s: int | None,
    ) -> int:
        """Sobe a parte (lida do original) e devolve o id da mensagem."""

    async def enviar_texto(self, texto: str) -> int: ...

    async def apagar(self, ids: list[int]) -> None: ...

    async def existem(self, ids: list[int]) -> set[int]:
        """Dos [ids], quais ainda existem no canal."""


class Registro(Protocol):
    def ja_publicado(self, tmdb_id: int) -> dict | None: ...

    def registrar(self, dados_tmdb: dict, nome_arquivo: str, message_ids: list[int], total_partes: int) -> str: ...

    def enfileirar_indice(self, motivo: str) -> str: ...


class RegistroDoBot:
    """Implementação real: o mesmo registro e a mesma fila de comandos do bot."""

    def __init__(self, armazenamento: Armazenamento):
        self.armazenamento = armazenamento

    def ja_publicado(self, tmdb_id: int) -> dict | None:
        return registro_mod.ja_publicado(self.armazenamento, tmdb_id)

    def registrar(self, dados_tmdb: dict, nome_arquivo: str, message_ids: list[int], total_partes: int) -> str:
        chave, _ = registro_mod.registrar(self.armazenamento, dados_tmdb, nome_arquivo, message_ids, total_partes)
        return chave

    def enfileirar_indice(self, motivo: str) -> str:
        return registro_mod.enfileirar_indice(self.armazenamento, motivo)


class RegistroNulo:
    """Sem acesso ao registro do bot: posta normalmente, mas não checa duplicidade nem registra (o
    monitor do bot sincroniza o canal depois e o filme entra no registro e no índice por lá)."""

    def ja_publicado(self, tmdb_id: int) -> dict | None:
        return None

    def registrar(self, dados_tmdb: dict, nome_arquivo: str, message_ids: list[int], total_partes: int) -> str:
        return registro_mod.chave_filme(dados_tmdb["tmdb_id"])

    def enfileirar_indice(self, motivo: str) -> str:
        return "sem_registro"


@dataclass
class Evento:
    tipo: str            # inicio | parte_inicio | progresso | parte_ok | texto_ok | registrado | indice | concluido | erro
    parte: int | None = None
    total_partes: int | None = None
    feito: int = 0
    total: int = 0
    mensagem: str = ""
    dados: dict = field(default_factory=dict)


def legenda_ref(dados_tmdb: dict) -> str | None:
    """Legenda mínima da parte 1 quando os metadados vão num texto separado: Título + TMDB id
    (o app amarra o texto ao vídeo por esse id). Igual a `bot._legenda_ref_video` para filmes."""
    tmdb_id = dados_tmdb.get("serie_tmdb_id") or dados_tmdb.get("tmdb_id")
    if not tmdb_id:
        return None
    linhas = []
    if dados_tmdb.get("titulo"):
        linhas.append(f"Título: {dados_tmdb['titulo']}")
    linhas.append(f"TMDB: {tmdb_id}")
    return "\n".join(linhas)


def montar_legenda(job: Job) -> tuple[str, bool]:
    """(legenda completa, cabe como legenda de mídia?)"""
    texto = montar_legenda_filme(job.dados_tmdb, job.audio, job.qualidade)
    return texto, len(texto) <= LIMITE_LEGENDA_MIDIA


def ordem_de_envio(job: Job, texto_separado: bool) -> list[tuple[str, ParteJob | None]]:
    """[('parte', N), …, ('parte', 2), ('texto', None)?, ('parte', 1)] — só o que ainda falta."""
    por_indice = sorted(job.partes, key=lambda p: p.indice, reverse=True)
    passos: list[tuple[str, ParteJob | None]] = []
    for parte in por_indice:
        if parte.indice == 1:
            if texto_separado and not job.texto_message_id:
                passos.append(("texto", None))
        if not parte.message_id:
            passos.append(("parte", parte))
    return passos


class Publicador:
    def __init__(
        self,
        enviador: Enviador,
        registro: Registro,
        repositorio: RepositorioDeJobs,
        emitir: Callable[[Evento], None] = lambda e: None,
    ):
        self.enviador = enviador
        self.registro = registro
        self.repositorio = repositorio
        self.emitir = emitir

    async def executar(self, job: Job, cancelado: Callable[[], bool] = lambda: False) -> str:
        """Sobe tudo o que falta, registra e pede o índice. Devolve a chave do registro.

        Levanta [PublicacaoCancelada] (estado salvo), [registro.JaPublicado] (antes de subir um
        byte) ou a falha do envio (job fica em ERRO, retomável)."""
        if job.estado == jobs.CONCLUIDO:
            raise ValueError("Este job já foi concluído.")
        if job.arquivo_mudou():
            raise ValueError("O arquivo original mudou desde o plano; refaça a postagem.")

        if not job.tem_envios:
            existente = self.registro.ja_publicado(job.tmdb_id)
            if existente:
                raise registro_mod.JaPublicado(registro_mod.chave_filme(job.tmdb_id), existente)

        await self._descartar_envios_perdidos(job)
        # O enviador também precisa saber do cancelamento: um upload de 2 GB não espera a próxima parte.
        definir_cancelamento = getattr(self.enviador, "definir_cancelamento", None)
        if definir_cancelamento:
            definir_cancelamento(cancelado)
        legenda, cabe = montar_legenda(job)
        texto_separado = not cabe
        job.estado = jobs.ENVIANDO
        job.erro = None
        self.repositorio.salvar(job)
        self.emitir(Evento("inicio", total_partes=job.total_partes, mensagem=job.nome_base))

        try:
            for passo, parte in ordem_de_envio(job, texto_separado):
                if cancelado():
                    raise PublicacaoCancelada()
                if passo == "texto":
                    job.texto_message_id = await self.enviador.enviar_texto(legenda)
                    self.emitir(Evento("texto_ok"))
                else:
                    await self._enviar_parte(job, parte, legenda, texto_separado)
                self.repositorio.salvar(job)
            if cancelado():
                raise PublicacaoCancelada()
            chave = self.registro.registrar(
                job.dados_tmdb, job.nome_base, job.ids_em_ordem_logica(), job.total_partes
            )
            self.emitir(Evento("registrado", mensagem=chave))
            situacao = self.registro.enfileirar_indice(f"cortador: {job.nome_base}")
            self.emitir(Evento("indice", mensagem=situacao))
        except PublicacaoCancelada:
            job.estado = jobs.ENVIANDO
            self.repositorio.salvar(job)
            raise
        except Exception as erro:
            job.estado = jobs.ERRO
            job.erro = str(erro)[:500]
            self.repositorio.salvar(job)
            self.emitir(Evento("erro", mensagem=job.erro))
            raise

        job.estado = jobs.CONCLUIDO
        self.repositorio.remover(job.id)
        self.emitir(Evento("concluido", mensagem=chave))
        return chave

    async def descartar(self, job: Job) -> None:
        """Cancelar de vez: apaga do canal o que já subiu e esquece o job."""
        ids = job.message_ids_enviados
        if ids:
            await self.enviador.apagar(ids)
        job.estado = jobs.CANCELADO
        self.repositorio.remover(job.id)

    async def _descartar_envios_perdidos(self, job: Job) -> None:
        """Ao retomar, confere o que o job acha que enviou: mensagem apagada no canal volta a ser pendente."""
        ids = job.message_ids_enviados
        if not ids:
            return
        existentes = await self.enviador.existem(ids)
        for parte in job.partes:
            if parte.message_id and parte.message_id not in existentes:
                parte.message_id = None
        if job.texto_message_id and job.texto_message_id not in existentes:
            job.texto_message_id = None

    async def _enviar_parte(self, job: Job, parte: ParteJob, legenda: str, texto_separado: bool) -> None:
        unica = job.total_partes == 1
        legenda_da_parte = None
        if parte.indice == 1:
            legenda_da_parte = legenda_ref(job.dados_tmdb) if texto_separado else legenda
        self.emitir(Evento("parte_inicio", parte=parte.indice, total_partes=job.total_partes,
                           total=parte.tamanho, mensagem=parte.nome))

        def progresso(feito: int, total: int) -> None:
            self.emitir(Evento("progresso", parte=parte.indice, total_partes=job.total_partes,
                               feito=feito, total=total))

        duracao = int(job.dados_tmdb.get("duracao") or 0) * 60 or None
        parte.message_id = await self.enviador.enviar_parte(
            parte, job.origem, legenda_da_parte, unica, progresso, duracao
        )
        self.emitir(Evento("parte_ok", parte=parte.indice, total_partes=job.total_partes,
                           mensagem=parte.nome, dados={"message_id": parte.message_id}))
