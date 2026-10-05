"""Configuração do NBR Cut — própria do app, sem depender do `.env` do bot.

Tudo fica em `%APPDATA%\\NbrCut\\config.json`. Os SEGREDOS (hash da API do Telegram, chave do
TMDB, URL do banco) são gravados protegidos pela DPAPI do Windows (ver `segredos`); o resto é texto
simples. A sessão do Telegram é própria do app (`telegram.session`): a do monitor do bot não pode ser
compartilhada, porque duas conexões na mesma sessão derrubam uma à outra.

Para quem já tem o bot configurado há o atalho [importar_do_env]: lê um `.env` UMA vez e preenche os
campos — depois disso o app não olha mais para o arquivo.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from cortador import divisao, segredos
from cortador.compartilhado.armazenamento import Armazenamento

# Campos gravados protegidos.
SEGREDOS = ("api_hash", "tmdb_key", "database_url")


def pasta_dados() -> Path:
    base = os.getenv("APPDATA") or str(Path.home() / ".config")
    nova = Path(base) / "NbrCut"
    antiga = Path(base) / "NbrCortador"
    if antiga.is_dir() and not nova.exists():  # app antes se chamava Nbr Cortador: leva config, jobs e sessão
        try:
            antiga.rename(nova)
        except OSError:
            return antiga
    return nova


def arquivo_config() -> Path:
    return pasta_dados() / "config.json"


def canal_como_entidade(canal: str | int) -> int | str:
    """`-1001234` vira int (o Telethon só resolve id numérico assim); `@nome` fica texto."""
    texto = str(canal).strip()
    return int(texto) if re.fullmatch(r"-?\d+", texto) else texto


@dataclass
class Configuracao:
    # Telegram (sessão de usuário) e TMDB.
    api_id: int = 0
    api_hash: str = ""
    tmdb_key: str = ""
    # Canais: produção e teste.
    canal_destino: str = ""
    canal_teste: str = ""
    modo_teste: bool = False
    # Registro do bot (para checar duplicidade, registrar e pedir o índice). Postgres do bot
    # (`database_url`) OU, se o bot roda sem banco, a pasta onde ele guarda os JSON (`pasta_registro`).
    database_url: str = ""
    pasta_registro: str = ""
    # Envio.
    premium: bool = False
    tamanho_parte: int = divisao.TAMANHO_PADRAO
    # Otimizar para streaming (ffmpeg + placa NVIDIA). `ffmpeg` vazio = procurar no PATH/winget.
    ffmpeg: str = ""
    pasta_otimizados: str = ""          # vazio = %APPDATA%\\NbrCut\\otimizados
    perfil_otimizacao: str = "4k18"
    apagar_otimizado: bool = True       # apaga a cópia otimizada depois de postar
    # Arquivos do app.
    sessao: Path = field(default_factory=lambda: pasta_dados() / "telegram.session")
    pasta_jobs: Path = field(default_factory=lambda: pasta_dados() / "jobs")
    # Avisos ao carregar (ex.: segredo que não pôde ser lido); a tela os mostra no log.
    avisos: list[str] = field(default_factory=list)

    @property
    def canal_ativo(self) -> str:
        """Onde postar agora: o canal de teste (se o modo teste está ligado) ou o de produção."""
        return self.canal_teste if self.modo_teste else self.canal_destino

    @property
    def pasta_de_otimizados(self) -> Path:
        return Path(self.pasta_otimizados) if self.pasta_otimizados.strip() else pasta_dados() / "otimizados"

    @property
    def limite(self) -> int:
        return divisao.limite_da_conta(self.premium)

    def armazenamento(self) -> Armazenamento:
        return Armazenamento(self.database_url, self.pasta_registro or None)

    def problemas(self) -> list[str]:
        """O que falta para postar (mensagens da tela de configurações)."""
        faltas = []
        if not self.api_id or not self.api_hash:
            faltas.append("Informe o api_id e o api_hash do Telegram (my.telegram.org → API development tools)")
        if not self.tmdb_key:
            faltas.append("Informe a chave da API do TMDB")
        if not self.canal_ativo:
            faltas.append("Informe o canal de teste (o modo teste está ligado)" if self.modo_teste
                          else "Informe o canal de destino (id -100… ou @nome)")
        if not 0 < self.tamanho_parte <= self.limite:
            faltas.append(
                f"Tamanho da parte ({divisao.humano(self.tamanho_parte)}) passa do limite da conta "
                f"({divisao.humano(self.limite)})"
            )
        return faltas


# --------------------------------------------------------------------------- disco

def carregar(arquivo: Path | None = None) -> Configuracao:
    """Lê o config.json (ausente/corrompido = configuração vazia). Nunca levanta."""
    caminho = arquivo or arquivo_config()
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}

    cfg = Configuracao()
    for campo in SEGREDOS:
        valor = dados.get(campo) or ""
        if not valor:
            continue
        try:
            setattr(cfg, campo, segredos.revelar(valor))
        except segredos.SegredoIlegivel as erro:
            cfg.avisos.append(f"Não consegui ler o campo salvo '{campo}' ({erro}). Informe de novo.")
    cfg.api_id = int(dados["api_id"]) if str(dados.get("api_id") or "").isdigit() else 0
    cfg.canal_destino = str(dados.get("canal_destino") or "").strip()
    cfg.canal_teste = str(dados.get("canal_teste") or "").strip()
    cfg.modo_teste = bool(dados.get("modo_teste", False))
    cfg.pasta_registro = str(dados.get("pasta_registro") or "").strip()
    cfg.premium = bool(dados.get("premium", False))
    cfg.ffmpeg = str(dados.get("ffmpeg") or "").strip()
    cfg.pasta_otimizados = str(dados.get("pasta_otimizados") or "").strip()
    cfg.perfil_otimizacao = str(dados.get("perfil_otimizacao") or "4k18").strip() or "4k18"
    cfg.apagar_otimizado = bool(dados.get("apagar_otimizado", True))
    tamanho = dados.get("tamanho_parte")
    cfg.tamanho_parte = tamanho if isinstance(tamanho, int) and tamanho > 0 else divisao.tamanho_padrao(cfg.premium)
    return cfg


def salvar(cfg: Configuracao, arquivo: Path | None = None) -> None:
    """Grava o config.json (segredos protegidos). Escrita atômica."""
    destino = arquivo or arquivo_config()
    destino.parent.mkdir(parents=True, exist_ok=True)
    dados: dict = {
        "api_id": cfg.api_id,
        "canal_destino": cfg.canal_destino,
        "canal_teste": cfg.canal_teste,
        "modo_teste": cfg.modo_teste,
        "pasta_registro": cfg.pasta_registro,
        "premium": cfg.premium,
        "tamanho_parte": cfg.tamanho_parte,
        "ffmpeg": cfg.ffmpeg,
        "pasta_otimizados": cfg.pasta_otimizados,
        "perfil_otimizacao": cfg.perfil_otimizacao,
        "apagar_otimizado": cfg.apagar_otimizado,
    }
    for campo in SEGREDOS:
        valor = getattr(cfg, campo)
        dados[campo] = segredos.proteger(valor) if valor else ""
    temporario = destino.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporario, destino)


# --------------------------------------------------------------------------- importar do bot

def importar_do_env(caminho: Path | str) -> dict:
    """Lê um `.env` (o do bot) e devolve só os campos encontrados, no formato do app:
    {api_id, api_hash, tmdb_key, canal_destino, database_url}. Não altera nada."""
    env = dotenv_values(caminho)
    achados: dict = {}
    api_id = str(env.get("TELEGRAM_API_ID") or "").strip()
    if api_id.isdigit() and int(api_id) > 0:
        achados["api_id"] = int(api_id)
    for destino, origens in {
        "api_hash": ("TELEGRAM_API_HASH",),
        "tmdb_key": ("TMDB_API_KEY",),
        "canal_destino": ("CANAL_DESTINO_ID",),
        "database_url": ("DATABASE_URL", "NEON_DATABASE_URL"),
    }.items():
        for origem in origens:
            valor = str(env.get(origem) or "").strip()
            if valor:
                achados[destino] = valor
                break
    return achados


def aplicar(cfg: Configuracao, dados: dict) -> Configuracao:
    """Copia [dados] (ex.: de [importar_do_env]) para [cfg]."""
    for campo, valor in dados.items():
        if hasattr(cfg, campo):
            setattr(cfg, campo, valor)
    return cfg
