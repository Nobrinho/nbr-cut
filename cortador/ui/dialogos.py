"""Janelas auxiliares: configurações, login do Telegram, confirmação de postagem e retomada."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import tkinter as tk
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from cortador import config as config_mod
from cortador import divisao
from cortador.config import Configuracao
from cortador.jobs import Job
from cortador.ui import tema


def botao(pai, texto: str, comando: Callable[[], None], *, primario: bool = False, perigo: bool = False,
          largura: int = 140) -> ctk.CTkButton:
    if primario:
        cores = dict(fg_color=tema.DESTAQUE, hover_color=tema.DESTAQUE_HOVER, text_color=tema.SOBRE_DESTAQUE)
    elif perigo:
        cores = dict(fg_color=tema.ERRO_FUNDO, hover_color=tema.ERRO_FUNDO_HOVER, text_color=tema.ERRO)
    else:
        cores = dict(fg_color=tema.PAINEL_2, hover_color=tema.BORDA, text_color=tema.TEXTO)
    return ctk.CTkButton(pai, text=texto, command=comando, width=largura, corner_radius=8, **cores)


class _Janela(ctk.CTkToplevel):
    """Base dos diálogos: modal, centralizado sobre a janela principal, fecha com Esc."""

    def __init__(self, pai, titulo: str, largura: int, altura: int):
        super().__init__(pai)
        self.title(titulo)
        self.configure(fg_color=tema.FUNDO)
        self.geometry(f"{largura}x{altura}")
        self.resizable(False, False)
        self.transient(pai)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(50, self._centralizar, pai, largura, altura)

    def _centralizar(self, pai, largura: int, altura: int) -> None:
        try:
            x = pai.winfo_rootx() + (pai.winfo_width() - largura) // 2
            y = pai.winfo_rooty() + (pai.winfo_height() - altura) // 3
            self.geometry(f"{largura}x{altura}+{max(x, 0)}+{max(y, 0)}")
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass  # janela já fechada


def _titulo(pai, texto: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(pai, text=texto, font=ctk.CTkFont(size=18, weight="bold"), text_color=tema.TEXTO)


def _suave(pai, texto: str, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(pai, text=texto, text_color=tema.TEXTO_SUAVE, justify="left", anchor="w", **kw)


# ============================================================================ configurações

class DialogoConfiguracoes(_Janela):
    TAMANHOS = ["1900M", "1500M", "1000M", "500M"]
    TAMANHOS_PREMIUM = ["3900M", "3000M", "1900M"]

    def __init__(self, pai, cfg: Configuracao, ao_salvar: Callable[[Configuracao], None]):
        super().__init__(pai, "Configurações", 700, 720)
        self.cfg = cfg
        self.ao_salvar = ao_salvar

        rolavel = ctk.CTkScrollableFrame(self, fg_color="transparent")
        rolavel.pack(fill="both", expand=True, padx=14, pady=(12, 0))
        corpo = ctk.CTkFrame(rolavel, fg_color="transparent")
        corpo.pack(fill="both", expand=True, padx=6)

        _titulo(corpo, "Configurações").pack(anchor="w")
        _suave(corpo, "Segredos (hash do Telegram, chave do TMDB, URL do banco) ficam protegidos pelo Windows, "
                      "só legíveis por este usuário.", wraplength=620).pack(anchor="w", pady=(2, 6))
        linha = ctk.CTkFrame(corpo, fg_color="transparent")
        linha.pack(fill="x", pady=(0, 6))
        botao(linha, "Importar do .env do bot…", self._importar_env, largura=190).pack(side="left")
        self.rotulo_importacao = _suave(linha, "")
        self.rotulo_importacao.pack(side="left", padx=10)

        self.var_api_id = self._campo(corpo, "Telegram — api_id", str(cfg.api_id or ""),
                                      "Em my.telegram.org → API development tools")
        self.var_api_hash = self._campo(corpo, "Telegram — api_hash", cfg.api_hash, mascarar=True)
        self.var_tmdb = self._campo(corpo, "Chave da API do TMDB", cfg.tmdb_key, mascarar=True)
        self.var_canal = self._campo(corpo, "Canal de destino (produção) — id -100… ou @nome", cfg.canal_destino)

        self.var_teste = tk.BooleanVar(value=cfg.modo_teste)
        ctk.CTkSwitch(corpo, text="Modo teste (posta no canal de teste, não no de produção)",
                      variable=self.var_teste, progress_color=tema.ALERTA).pack(anchor="w", pady=(8, 4))
        self.var_canal_teste = self._campo(corpo, "Canal de teste — id -100… ou @nome", cfg.canal_teste)

        _suave(corpo, "Registro do bot (opcional) — para checar duplicidade, registrar o filme e pedir o índice. "
                      "Sem isto o app posta e o monitor do bot registra depois.", wraplength=620).pack(
            anchor="w", pady=(12, 2))
        self.var_banco = self._campo(corpo, "URL do Postgres do bot (postgresql://…)", cfg.database_url, mascarar=True)
        linha = ctk.CTkFrame(corpo, fg_color="transparent")
        linha.pack(fill="x")
        self.var_pasta = tk.StringVar(value=cfg.pasta_registro)
        _suave(corpo, "…ou a pasta dos JSON do bot (se ele roda sem banco)").pack(anchor="w", pady=(6, 0))
        linha = ctk.CTkFrame(corpo, fg_color="transparent")
        linha.pack(fill="x", pady=(2, 6))
        ctk.CTkEntry(linha, textvariable=self.var_pasta).pack(side="left", fill="x", expand=True)
        botao(linha, "Procurar…", self._procurar_pasta, largura=90).pack(side="left", padx=(8, 0))

        self.var_premium = tk.BooleanVar(value=cfg.premium)
        ctk.CTkSwitch(corpo, text="Conta Telegram Premium (partes de até 4 GB)", variable=self.var_premium,
                      command=self._premium_mudou, progress_color=tema.DESTAQUE).pack(anchor="w", pady=(10, 6))
        _suave(corpo, "Tamanho máximo de cada parte").pack(anchor="w")
        self.var_tamanho = tk.StringVar(value=self._rotulo_tamanho(cfg.tamanho_parte))
        self.menu_tamanho = ctk.CTkOptionMenu(
            corpo, variable=self.var_tamanho, values=self._opcoes(), width=160,
            fg_color=tema.PAINEL_2, button_color=tema.BORDA, button_hover_color=tema.DESTAQUE_HOVER,
        )
        self.menu_tamanho.pack(anchor="w", pady=(2, 8))

        self.rotulo_problemas = ctk.CTkLabel(corpo, text="", text_color=tema.ALERTA, justify="left", anchor="w",
                                             wraplength=620)
        self.rotulo_problemas.pack(anchor="w", pady=(6, 10), fill="x")
        self._mostrar_problemas(self._montar())

        rodape = ctk.CTkFrame(self, fg_color="transparent")
        rodape.pack(fill="x", padx=20, pady=12)
        botao(rodape, "Cancelar", self.destroy, largura=100).pack(side="right")
        botao(rodape, "Salvar", self._salvar, primario=True, largura=100).pack(side="right", padx=8)

    # ----------------------------------------------------------------------------------
    def _campo(self, pai, rotulo: str, valor: str, dica: str = "", mascarar: bool = False) -> tk.StringVar:
        _suave(pai, rotulo).pack(anchor="w", pady=(6, 0))
        variavel = tk.StringVar(value=valor)
        ctk.CTkEntry(pai, textvariable=variavel, show="•" if mascarar else "").pack(fill="x", pady=(2, 0))
        if dica:
            _suave(pai, dica).pack(anchor="w")
        return variavel

    def _opcoes(self) -> list[str]:
        return self.TAMANHOS_PREMIUM if self.var_premium.get() else self.TAMANHOS

    @staticmethod
    def _rotulo_tamanho(tamanho: int) -> str:
        return f"{tamanho // divisao.MIB}M"

    def _premium_mudou(self) -> None:
        opcoes = self._opcoes()
        self.menu_tamanho.configure(values=opcoes)
        self.var_tamanho.set(opcoes[0])

    def _procurar_pasta(self) -> None:
        escolhida = filedialog.askdirectory(parent=self, title="Pasta dos JSON do bot")
        if escolhida:
            self.var_pasta.set(escolhida)

    def _importar_env(self) -> None:
        escolhido = filedialog.askopenfilename(
            parent=self, title="Escolha o .env do bot", filetypes=[("Arquivo .env", "*.env *.*"), ("Todos", "*.*")],
            initialfile=".env",
        )
        if not escolhido:
            return
        try:
            achados = config_mod.importar_do_env(escolhido)
        except OSError as erro:
            self.rotulo_importacao.configure(text=f"Não consegui ler: {erro}", text_color=tema.ERRO)
            return
        mapa = {
            "api_id": self.var_api_id, "api_hash": self.var_api_hash, "tmdb_key": self.var_tmdb,
            "canal_destino": self.var_canal, "database_url": self.var_banco,
        }
        for campo, valor in achados.items():
            mapa[campo].set(str(valor))
        self.rotulo_importacao.configure(
            text=f"{len(achados)} campo(s) importado(s) — confira e salve", text_color=tema.SUCESSO)
        self._mostrar_problemas(self._montar())

    def _montar(self) -> Configuracao:
        novo = dataclasses.replace(self.cfg, avisos=[])
        api_id = self.var_api_id.get().strip()
        novo.api_id = int(api_id) if api_id.isdigit() else 0
        novo.api_hash = self.var_api_hash.get().strip()
        novo.tmdb_key = self.var_tmdb.get().strip()
        novo.canal_destino = self.var_canal.get().strip()
        novo.canal_teste = self.var_canal_teste.get().strip()
        novo.modo_teste = self.var_teste.get()
        novo.database_url = self.var_banco.get().strip()
        novo.pasta_registro = self.var_pasta.get().strip()
        novo.premium = self.var_premium.get()
        try:
            novo.tamanho_parte = divisao.parse_tamanho(self.var_tamanho.get())
        except divisao.ErroDivisao:
            novo.tamanho_parte = divisao.tamanho_padrao(novo.premium)
        return novo

    def _mostrar_problemas(self, cfg: Configuracao) -> None:
        problemas = cfg.problemas()
        self.rotulo_problemas.configure(text=("⚠ " + "\n⚠ ".join(problemas)) if problemas else "")

    def _salvar(self) -> None:
        novo = self._montar()
        config_mod.salvar(novo)
        self.ao_salvar(novo)
        self.destroy()


# ============================================================================ login do Telegram

class DialogoLogin(_Janela):
    """Telefone → código → senha de 2 etapas (se houver). Quem coordena (o app) chama
    `pedir_codigo()`/`pedir_senha()` de dentro da corrotina de login: devolvem Futures que se
    completam quando o usuário digita aqui."""

    def __init__(self, pai, ao_enviar_telefone: Callable[[str], None], ao_fechar: Callable[[], None]):
        super().__init__(pai, "Entrar no Telegram", 460, 330)
        self._ao_enviar_telefone = ao_enviar_telefone
        self._ao_fechar = ao_fechar
        self._futuro: concurrent.futures.Future | None = None
        self.protocol("WM_DELETE_WINDOW", self._fechar)

        corpo = ctk.CTkFrame(self, fg_color="transparent")
        corpo.pack(fill="both", expand=True, padx=22, pady=18)
        _titulo(corpo, "Entrar no Telegram").pack(anchor="w")
        _suave(corpo, "Uma sessão própria do app (não mexe na do monitor). O Telegram vai avisar de um novo "
                      "dispositivo.", wraplength=410).pack(anchor="w", pady=(2, 12))

        self.rotulo = _suave(corpo, "Seu telefone com DDI (ex.: +55 11 99999-0000)")
        self.rotulo.pack(anchor="w")
        self.var_texto = tk.StringVar()
        self.entrada = ctk.CTkEntry(corpo, textvariable=self.var_texto, width=300)
        self.entrada.pack(anchor="w", pady=(2, 8))
        self.entrada.bind("<Return>", lambda e: self._confirmar())
        self.mensagem = ctk.CTkLabel(corpo, text="", text_color=tema.ALERTA, wraplength=410, justify="left", anchor="w")
        self.mensagem.pack(anchor="w", fill="x")
        self.botao = botao(corpo, "Enviar código", self._confirmar, primario=True, largura=150)
        self.botao.pack(anchor="w", pady=(10, 0))
        self._etapa = "telefone"
        self.after(100, self.entrada.focus_set)

    def _confirmar(self) -> None:
        texto = self.var_texto.get().strip()
        if not texto:
            return
        if self._etapa == "telefone":
            self.botao.configure(state="disabled")
            self.mensagem.configure(text="Pedindo o código ao Telegram…", text_color=tema.TEXTO_SUAVE)
            self._ao_enviar_telefone(texto)
        elif self._futuro is not None and not self._futuro.done():
            self.botao.configure(state="disabled")
            self.mensagem.configure(text="Verificando…", text_color=tema.TEXTO_SUAVE)
            self._futuro.set_result(texto)

    def aguardar(self, etapa: str) -> concurrent.futures.Future:
        """Chamado pela thread da interface: troca o campo para [etapa] ('codigo' ou 'senha')."""
        self._futuro = concurrent.futures.Future()
        self._etapa = etapa
        self.var_texto.set("")
        if etapa == "codigo":
            self.rotulo.configure(text="Código que o Telegram enviou (no app, em 'Telegram')")
            self.entrada.configure(show="")
            self.botao.configure(text="Entrar", state="normal")
        else:
            self.rotulo.configure(text="Senha da verificação em duas etapas")
            self.entrada.configure(show="•")
            self.botao.configure(text="Confirmar", state="normal")
        self.mensagem.configure(text="")
        self.entrada.focus_set()
        return self._futuro

    def erro(self, texto: str) -> None:
        self.mensagem.configure(text=texto, text_color=tema.ERRO)
        self.botao.configure(state="normal", text="Enviar código")
        self._etapa = "telefone"
        self.rotulo.configure(text="Seu telefone com DDI (ex.: +55 11 99999-0000)")
        self.entrada.configure(show="")

    def fechar_ok(self) -> None:
        self._ao_fechar = lambda: None
        self.destroy()

    def _fechar(self) -> None:
        if self._futuro is not None and not self._futuro.done():
            self._futuro.cancel()
        self._ao_fechar()
        self.destroy()


# ============================================================================ confirmar postagem

class DialogoConfirmar(_Janela):
    """Última chance antes de subir qualquer byte: mostra o que será postado e ONDE."""

    def __init__(self, pai, linhas: list[tuple[str, str]], destino: str, teste: bool, ao_confirmar: Callable[[], None],
                 avisos: list[str] | None = None):
        avisos = avisos or []
        super().__init__(pai, "Confirmar postagem", 560, 120 + 28 * len(linhas) + 70 + sum(22 + 17 * (len(a) // 62 + 1) for a in avisos))
        corpo = ctk.CTkFrame(self, fg_color="transparent")
        corpo.pack(fill="both", expand=True, padx=22, pady=18)
        _titulo(corpo, "Postar este filme?").pack(anchor="w", pady=(0, 10))
        for rotulo, valor in linhas:
            linha = ctk.CTkFrame(corpo, fg_color="transparent")
            linha.pack(fill="x", pady=2)
            ctk.CTkLabel(linha, text=rotulo, width=110, anchor="w", text_color=tema.TEXTO_SUAVE).pack(side="left")
            ctk.CTkLabel(linha, text=valor, anchor="w", justify="left", wraplength=400,
                         text_color=tema.TEXTO).pack(side="left", fill="x", expand=True)
        for texto in avisos:
            ctk.CTkLabel(corpo, text="⚠ " + texto, text_color=tema.ALERTA, wraplength=500, justify="left",
                         anchor="w").pack(anchor="w", pady=(10, 0))
        aviso_cor = tema.ALERTA if teste else tema.SUCESSO
        aviso = f"Será publicado no canal de {'TESTE' if teste else 'PRODUÇÃO'}: {destino}"
        ctk.CTkLabel(corpo, text=aviso, text_color=aviso_cor, font=ctk.CTkFont(weight="bold"),
                     wraplength=500, justify="left").pack(anchor="w", pady=(12, 0))
        rodape = ctk.CTkFrame(self, fg_color="transparent")
        rodape.pack(fill="x", padx=22, pady=(0, 16))
        botao(rodape, "Cancelar", self.destroy, largura=110).pack(side="right")
        botao(rodape, "Postar agora", lambda: (self.destroy(), ao_confirmar()), primario=True, largura=130).pack(
            side="right", padx=8)


# ============================================================================ envios interrompidos

class DialogoPendentes(_Janela):
    def __init__(self, pai, pendentes: list[Job], ao_retomar: Callable[[Job], None],
                 ao_descartar: Callable[[Job], None]):
        super().__init__(pai, "Envios interrompidos", 640, 440)
        corpo = ctk.CTkFrame(self, fg_color="transparent")
        corpo.pack(fill="both", expand=True, padx=20, pady=16)
        _titulo(corpo, "Envios interrompidos").pack(anchor="w")
        _suave(corpo, "Retomar sobe só o que falta (confere o canal antes). Descartar apaga do canal o que "
                      "já subiu e esquece o envio.", wraplength=590).pack(anchor="w", pady=(2, 10))
        lista = ctk.CTkScrollableFrame(corpo, fg_color=tema.PAINEL)
        lista.pack(fill="both", expand=True)
        if not pendentes:
            _suave(lista, "Nenhum envio pendente.").pack(padx=10, pady=10)
        for job in pendentes:
            enviadas = sum(1 for p in job.partes if p.message_id)
            cartao = ctk.CTkFrame(lista, fg_color=tema.PAINEL_2, corner_radius=8)
            cartao.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(cartao, text=job.nome_base, anchor="w", font=ctk.CTkFont(weight="bold"),
                         wraplength=560, justify="left").pack(anchor="w", padx=10, pady=(8, 0))
            detalhe = f"{enviadas}/{job.total_partes} partes enviadas · canal {job.canal} · {job.criado_em}"
            if job.erro:
                detalhe += f"\nÚltimo erro: {job.erro}"
            _suave(cartao, detalhe, wraplength=560).pack(anchor="w", padx=10, pady=(0, 6))
            acoes = ctk.CTkFrame(cartao, fg_color="transparent")
            acoes.pack(anchor="e", padx=8, pady=(0, 8))
            botao(acoes, "Descartar", lambda j=job: (ao_descartar(j), self.destroy()), perigo=True, largura=100).pack(
                side="left", padx=4)
            botao(acoes, "Retomar", lambda j=job: (ao_retomar(j), self.destroy()), primario=True, largura=100).pack(
                side="left", padx=4)
