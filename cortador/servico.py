"""Cola entre a interface (ou a CLI) e o núcleo: conecta ao Telegram, monta o publicador e posta.

Uma única conexão Telethon por processo, usada sempre no MESMO loop asyncio (ver `execucao`).
Os métodos são corrotinas: a interface os agenda no loop do Telegram e recebe os eventos de
progresso por callback.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from cortador import config as config_mod
from cortador import jobs, telegram_envio
from cortador.config import Configuracao
from cortador.fila import ItemFila
from cortador.jobs import Job, RepositorioDeJobs
from cortador.publicador import Evento, Publicador, RegistroDoBot, RegistroNulo


class Servico:
    def __init__(self, cfg: Configuracao):
        self.cfg = cfg
        self.cliente = None
        self.entidade = None
        self.armazenamento = cfg.armazenamento()
        self.repositorio = RepositorioDeJobs(cfg.pasta_jobs)
        self._publicador: Publicador | None = None
        self._canal_resolvido: str | None = None

    # ------------------------------------------------------------------ registro do bot

    def reconfigurar(self, cfg: Configuracao) -> None:
        """Aplica uma configuração nova (a conexão com o Telegram é refeita por quem chama)."""
        self.cfg = cfg
        self.armazenamento = cfg.armazenamento()
        self.repositorio = RepositorioDeJobs(cfg.pasta_jobs)
        self._publicador = None

    def registro(self):
        """Registro do bot se estiver configurado; senão um registro nulo (posta sem registrar)."""
        return RegistroDoBot(self.armazenamento) if self.armazenamento.ativo else RegistroNulo()

    def ja_publicado(self, tmdb_id: int) -> dict | None:
        return self.registro().ja_publicado(tmdb_id)

    def testar_registro(self) -> None:
        """Levanta se o registro está configurado mas inacessível (Docker parado, URL errada…)."""
        self.armazenamento.testar()

    # ------------------------------------------------------------------ Telegram

    async def conectar(self) -> bool:
        """Conecta com a sessão do app. False = ainda é preciso entrar (telefone + código)."""
        if self.cliente is None:
            self.cliente = telegram_envio.criar_cliente(self.cfg.api_id, self.cfg.api_hash, self.cfg.sessao)
        return await telegram_envio.conectar(self.cliente)

    async def entrar(self, telefone: str, pedir_codigo: Callable[[], Awaitable[str]],
                     pedir_senha: Callable[[], Awaitable[str]]) -> None:
        if self.cliente is None:
            await self.conectar()
        await telegram_envio.entrar(self.cliente, telefone, pedir_codigo, pedir_senha)

    async def nome_do_usuario(self) -> str:
        eu = await self.cliente.get_me()
        return " ".join(p for p in (getattr(eu, "first_name", None), getattr(eu, "last_name", None)) if p) or "conta"

    async def desconectar(self) -> None:
        if self.cliente is not None:
            await self.cliente.disconnect()
            self.cliente = None
            self.entidade = None
            self._publicador = None

    async def resolver_canal(self):
        """Entidade do canal ATIVO (teste ou produção). Refaz se o canal ativo mudou."""
        canal = self.cfg.canal_ativo
        if self.entidade is None or self._canal_resolvido != canal:
            self.entidade = await telegram_envio.resolver_canal(self.cliente, config_mod.canal_como_entidade(canal))
            self._canal_resolvido = canal
            self._publicador = None
        return self.entidade

    # ------------------------------------------------------------------ postagem

    def novo_job(self, item: ItemFila) -> Job:
        """Job (plano + dados) do item atual. O nome final e o canal ficam congelados no job."""
        if not item.dados:
            raise ValueError("Escolha o filme no TMDB antes de postar.")
        return Job.novo(
            item.caminho,
            nome_base=item.nome_final,
            dados_tmdb=item.dados,
            audio=item.audio,
            qualidade=item.qualidade,
            canal=self.cfg.canal_ativo,
            maximo=self.cfg.tamanho_parte,
            duracao_s=item.duracao_real_s,
            largura=item.midia.video.largura if item.midia and item.midia.video else 0,
            altura=item.midia.video.altura if item.midia and item.midia.video else 0,
        )

    async def publicador(self, emitir: Callable[[Evento], None]) -> Publicador:
        entidade = await self.resolver_canal()
        enviador = telegram_envio.EnviadorTelethon(self.cliente, entidade)
        return Publicador(enviador, self.registro(), self.repositorio, emitir)

    async def postar(self, job: Job, emitir: Callable[[Evento], None], cancelado: Callable[[], bool]) -> str:
        if job.canal != self.cfg.canal_ativo:
            raise ValueError(
                f"Este job foi criado para o canal {job.canal}, mas o canal ativo agora é {self.cfg.canal_ativo}. "
                "Volte o canal (modo teste) ou descarte o job."
            )
        publicador = await self.publicador(emitir)
        return await publicador.executar(job, cancelado)

    async def descartar(self, job: Job) -> None:
        publicador = await self.publicador(lambda e: None)
        await publicador.descartar(job)

    def pendentes(self) -> list[Job]:
        """Postagens interrompidas, para oferecer 'Retomar' ao abrir o app."""
        return self.repositorio.pendentes()
