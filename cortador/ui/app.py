"""Janela principal do NBR Cut.

Esquerda: a fila de arquivos. Direita: o filme selecionado — busca no TMDB, nome padronizado, plano
de corte, prévia da legenda e a postagem com progresso. Nada de regra de negócio aqui: ela vive em
`cortador.fila` (item), `cortador.servico` (Telegram/postagem) e no núcleo.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import gc
import os
import queue
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from cortador import __version__, busca, divisao, execucao, registro
from cortador import config as config_mod
from cortador.config import Configuracao
from cortador.fila import (CANCELADO, CONCLUIDO, ENVIANDO, ERRO, NOVO, PRONTO, ItemFila, eh_video)
from cortador.jobs import Job
from cortador.publicador import Evento, PublicacaoCancelada
from cortador.servico import Servico
from cortador.ui import miniaturas, tema
from cortador.ui.dialogos import (DialogoConfiguracoes, DialogoConfirmar, DialogoLogin, DialogoPendentes, botao)

LARGURA_FILA = 330


def _txt(pai, texto="", **kw) -> ctk.CTkLabel:
    kw.setdefault("text_color", tema.TEXTO)
    kw.setdefault("anchor", "w")
    kw.setdefault("justify", "left")
    return ctk.CTkLabel(pai, text=texto, **kw)


def _secao(pai, titulo: str) -> ctk.CTkFrame:
    """Cartão com título, devolvendo o frame interno onde entra o conteúdo."""
    cartao = ctk.CTkFrame(pai, fg_color=tema.PAINEL, corner_radius=tema.RAIO)
    cartao.pack(fill="x", padx=tema.ESPACO, pady=(0, tema.ESPACO))
    _txt(cartao, titulo, font=ctk.CTkFont(size=13, weight="bold"), text_color=tema.TEXTO_SUAVE).pack(
        anchor="w", padx=14, pady=(10, 4))
    interno = ctk.CTkFrame(cartao, fg_color="transparent")
    interno.pack(fill="x", padx=14, pady=(0, 12))
    return cartao, interno


def formatar_velocidade(bytes_por_s: float) -> str:
    return f"{bytes_por_s / divisao.MIB:.1f} MB/s"


def formatar_tempo(segundos: float) -> str:
    segundos = int(max(segundos, 0))
    horas, resto = divmod(segundos, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}h{minutos:02d}min"
    if minutos:
        return f"{minutos}min{seg:02d}s"
    return f"{seg}s"


class Aplicativo(ctk.CTk):
    def __init__(self, cfg: Configuracao):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(f"NBR Cut {__version__}")
        self.geometry("1260x820")
        self.minsize(1040, 700)
        self.configure(fg_color=tema.FUNDO)
        self._icone()

        self.cfg = cfg
        self.servico = Servico(cfg)
        self.loop = execucao.LoopDeFundo()
        # Threads de fundo NUNCA chamam o Tk: entregam funções nesta fila, e a thread da interface a
        # esvazia a cada poucos ms (`after` chamado de outra thread não é seguro e exige o mainloop).
        self._fila_ui: queue.SimpleQueue = queue.SimpleQueue()
        self._encerrando = False
        self.despachar = execucao.Despachante(self._fila_ui.put)
        # O coletor de lixo roda na thread que estiver alocando memória — inclusive a do upload. Se ele
        # finalizar ali um objeto do Tk (Font, StringVar…), a chamada Tcl trava a thread para sempre
        # (visto: upload parado em `tkinter.font.Font.__del__`). Por isso o GC automático fica desligado
        # e a coleta é feita pela própria thread da interface, a cada poucos segundos.
        gc.disable()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cortador-io")
        self.cancelamento = execucao.Cancelamento()
        self.itens: list[ItemFila] = []
        self.sel: ItemFila | None = None
        self.postando: ItemFila | None = None
        self._job_atual: Job | None = None
        self._fila_envio: list[ItemFila] = []
        self._telegram_ok = False
        self._banco_ok: bool | None = None
        self._login: DialogoLogin | None = None
        self._miniaturas_refs: list = []
        self._trocando = False  # True = lista de candidatos aberta mesmo com um filme já escolhido
        self._progresso_fixo = False  # mantém o resumo do último envio na tela até trocar de item
        self._reiniciar_progresso()

        self._montar()
        for aviso in cfg.avisos:
            self.log(f"⚠ {aviso}")
        self.protocol("WM_DELETE_WINDOW", self._sair)
        self.after(40, self._drenar_fila_ui)
        self.after(3000, self._coletar_lixo)
        self.after(150, self._iniciar)

    # ================================================================================== montagem

    def _icone(self) -> None:
        for candidato in (Path(__file__).resolve().parent.parent / "assets" / "icone.ico",
                          Path(getattr(__import__("sys"), "_MEIPASS", ".")) / "icone.ico"):
            if candidato.is_file():
                try:
                    self.iconbitmap(str(candidato))
                except tk.TclError:
                    pass
                return

    def _carregar_logo(self):
        """Marca NBR Play (PNG transparente do brand kit) no topo; sem ela o app segue só com o texto."""
        for candidato in (Path(__file__).resolve().parent.parent / "assets" / "logo.png",
                          Path(getattr(__import__("sys"), "_MEIPASS", ".")) / "logo.png"):
            if candidato.is_file():
                try:
                    from PIL import Image
                    return ctk.CTkImage(Image.open(candidato), size=(44, 44))
                except Exception:
                    return None
        return None

    def _montar(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._montar_topo()
        self._montar_fila()
        self._montar_detalhe()
        self._montar_log()

    def _montar_topo(self) -> None:
        topo = ctk.CTkFrame(self, fg_color=tema.PAINEL, corner_radius=0, height=56)
        topo.grid(row=0, column=0, columnspan=2, sticky="ew")
        topo.grid_propagate(False)
        self._logo = self._carregar_logo()
        if self._logo:
            ctk.CTkLabel(topo, text="", image=self._logo).pack(side="left", padx=(18, 6))
        _txt(topo, "NBR Cut", font=ctk.CTkFont(size=19, weight="bold"), text_color=tema.DESTAQUE).pack(
            side="left", padx=(0, 24))
        self.chip_telegram = _txt(topo, "● Telegram: desconectado", text_color=tema.TEXTO_SUAVE)
        self.chip_telegram.pack(side="left", padx=10)
        self.chip_tmdb = _txt(topo, "● TMDB", text_color=tema.TEXTO_SUAVE)
        self.chip_tmdb.pack(side="left", padx=10)
        self.chip_banco = _txt(topo, "● Banco", text_color=tema.TEXTO_SUAVE)
        self.chip_banco.pack(side="left", padx=10)
        botao(topo, "⚙ Configurações", self._abrir_configuracoes, largura=140).pack(side="right", padx=(6, 14), pady=10)
        self.botao_pendentes = botao(topo, "Envios interrompidos", self._abrir_pendentes, largura=170)
        self.botao_pendentes.pack(side="right", padx=6, pady=10)
        self.botao_conectar = botao(topo, "Conectar ao Telegram", self._conectar, largura=170)
        self.botao_conectar.pack(side="right", padx=6, pady=10)

    def _montar_fila(self) -> None:
        lateral = ctk.CTkFrame(self, fg_color=tema.PAINEL, corner_radius=tema.RAIO, width=LARGURA_FILA)
        lateral.grid(row=1, column=0, sticky="nsw", padx=(tema.ESPACO, 6), pady=tema.ESPACO)
        lateral.grid_propagate(False)
        lateral.grid_rowconfigure(1, weight=1)
        _txt(lateral, "Fila", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 4))
        self.lista_fila = ctk.CTkScrollableFrame(lateral, fg_color="transparent", width=LARGURA_FILA - 40)
        self.lista_fila.grid(row=1, column=0, sticky="nsew", padx=6)
        rodape = ctk.CTkFrame(lateral, fg_color="transparent")
        rodape.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        botao(rodape, "+ Adicionar filmes", self._adicionar, primario=True, largura=150).pack(side="left")
        botao(rodape, "Remover", self._remover, largura=80).pack(side="right")
        self.botao_postar_todos = botao(lateral, "Postar todos os prontos", self._postar_todos, largura=300)
        self.botao_postar_todos.grid(row=3, column=0, padx=10, pady=(0, 12))

    def _montar_detalhe(self) -> None:
        coluna = ctk.CTkFrame(self, fg_color="transparent")
        coluna.grid(row=1, column=1, sticky="nsew", padx=(6, tema.ESPACO), pady=tema.ESPACO)
        coluna.grid_columnconfigure(0, weight=1)
        coluna.grid_rowconfigure(0, weight=1)
        self.detalhe = ctk.CTkScrollableFrame(coluna, fg_color="transparent")
        self.detalhe.grid(row=0, column=0, sticky="nsew")
        # Barra de ação FIXA embaixo: postar e o progresso nunca ficam escondidos pela rolagem.
        self.painel_acao = ctk.CTkFrame(coluna, fg_color=tema.PAINEL, corner_radius=tema.RAIO)
        self.painel_acao.grid(row=1, column=0, sticky="ew", pady=(tema.ESPACO, 0))

        self.vazio = _txt(self.detalhe, "Adicione um ou mais filmes na fila para começar.",
                          font=ctk.CTkFont(size=15), text_color=tema.TEXTO_SUAVE)
        self.vazio.pack(pady=80)

        self.conteudo = ctk.CTkFrame(self.detalhe, fg_color="transparent")
        # --- arquivo
        _, interno = _secao(self.conteudo, "ARQUIVO")
        self.lbl_arquivo = _txt(interno, "", font=ctk.CTkFont(size=14, weight="bold"), wraplength=780)
        self.lbl_arquivo.pack(anchor="w")
        self.lbl_arquivo_info = _txt(interno, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_arquivo_info.pack(anchor="w")

        # --- busca no TMDB
        _, interno = _secao(self.conteudo, "FILME NO TMDB")
        linha = ctk.CTkFrame(interno, fg_color="transparent")
        linha.pack(fill="x")
        self.var_titulo = tk.StringVar()
        self.var_ano = tk.StringVar()
        self.entrada_titulo = ctk.CTkEntry(linha, textvariable=self.var_titulo, placeholder_text="Título, tmdb:123 ou URL do TMDB")
        self.entrada_titulo.pack(side="left", fill="x", expand=True)
        self.entrada_titulo.bind("<Return>", lambda e: self._buscar())
        ctk.CTkEntry(linha, textvariable=self.var_ano, width=70, placeholder_text="Ano").pack(side="left", padx=8)
        botao(linha, "Buscar", self._buscar, primario=True, largura=90).pack(side="left")
        self.lbl_busca = _txt(interno, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_busca.pack(anchor="w", pady=(6, 0))
        self.lista_candidatos = ctk.CTkFrame(interno, fg_color="transparent", height=1)
        self.lista_candidatos.pack(fill="x", pady=(4, 0))
        self.cartao_escolhido = ctk.CTkFrame(interno, fg_color=tema.PAINEL_2, corner_radius=8)

        # --- arquivo final
        _, interno = _secao(self.conteudo, "ARQUIVO FINAL")
        _txt(interno, "Nome padronizado", text_color=tema.TEXTO_SUAVE).pack(anchor="w")
        self.var_nome = tk.StringVar()
        self.entrada_nome = ctk.CTkEntry(interno, textvariable=self.var_nome)
        self.entrada_nome.pack(fill="x", pady=(2, 8))
        self.entrada_nome.bind("<FocusOut>", lambda e: self._nome_editado())
        self.entrada_nome.bind("<Return>", lambda e: self._nome_editado())
        linha = ctk.CTkFrame(interno, fg_color="transparent")
        linha.pack(fill="x")
        _txt(linha, "Qualidade", text_color=tema.TEXTO_SUAVE, width=70).pack(side="left")
        self.var_qualidade = tk.StringVar()
        self.entrada_qualidade = ctk.CTkEntry(linha, textvariable=self.var_qualidade, width=240)
        self.entrada_qualidade.pack(side="left", padx=(0, 18))
        self.entrada_qualidade.bind("<FocusOut>", lambda e: self._qualidade_editada())
        self.entrada_qualidade.bind("<Return>", lambda e: self._qualidade_editada())
        _txt(linha, "Áudio", text_color=tema.TEXTO_SUAVE, width=50).pack(side="left")
        self.var_audio = tk.StringVar()
        self.entrada_audio = ctk.CTkEntry(linha, textvariable=self.var_audio, width=160)
        self.entrada_audio.pack(side="left")
        self.entrada_audio.bind("<FocusOut>", lambda e: self._audio_editado())
        self.lbl_plano = _txt(interno, "", font=ctk.CTkFont(weight="bold"))
        self.lbl_plano.pack(anchor="w", pady=(10, 0))

        # --- legenda
        _, interno = _secao(self.conteudo, "LEGENDA DO CANAL")
        self.lbl_legenda = _txt(interno, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_legenda.pack(anchor="w")
        self.texto_legenda = ctk.CTkTextbox(interno, height=150, fg_color=tema.PAINEL_2, text_color=tema.TEXTO_SUAVE,
                                            wrap="word")
        self.texto_legenda.pack(fill="x", pady=(4, 0))
        self.texto_legenda.configure(state="disabled")

        # --- ações e progresso (barra fixa)
        interno = ctk.CTkFrame(self.painel_acao, fg_color="transparent")
        interno.pack(fill="x", padx=14, pady=10)
        self.lbl_aviso = _txt(interno, "", text_color=tema.ALERTA, wraplength=780)
        self.lbl_aviso.pack(anchor="w", pady=(0, 4))
        linha = ctk.CTkFrame(interno, fg_color="transparent")
        linha.pack(fill="x")
        self.botao_postar = botao(linha, "Postar no canal…", self._postar_selecionado, primario=True, largura=170)
        self.botao_postar.pack(side="left")
        self.botao_cortar = botao(linha, "Só cortar…", self._so_cortar, largura=120)
        self.botao_cortar.pack(side="left", padx=8)
        self.botao_cancelar = botao(linha, "Cancelar envio", self._cancelar, perigo=True, largura=140)
        self.botao_cancelar.pack(side="left")
        self.lbl_destino = _txt(linha, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_destino.pack(side="right")
        # Progresso: só aparece durante um envio (parada, a barra em zero vira um pontinho estranho).
        self.area_progresso = ctk.CTkFrame(interno, fg_color="transparent")
        self.lbl_parte = _txt(self.area_progresso, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_parte.pack(anchor="w", pady=(8, 2))
        self.barra_parte = ctk.CTkProgressBar(self.area_progresso, progress_color=tema.DESTAQUE, fg_color=tema.PAINEL_2,
                                              height=8)
        self.barra_parte.pack(fill="x")
        self.barra_parte.set(0)
        self.lbl_total = _txt(self.area_progresso, "", text_color=tema.TEXTO_SUAVE)
        self.lbl_total.pack(anchor="w", pady=(6, 2))
        self.barra_total = ctk.CTkProgressBar(self.area_progresso, progress_color=tema.DESTAQUE, fg_color=tema.PAINEL_2,
                                              height=8)
        self.barra_total.pack(fill="x")
        self.barra_total.set(0)

    def _montar_log(self) -> None:
        self.caixa_log = ctk.CTkTextbox(self, height=92, fg_color=tema.PAINEL, text_color=tema.TEXTO_SUAVE,
                                        font=ctk.CTkFont(family="Consolas", size=12))
        self.caixa_log.grid(row=2, column=0, columnspan=2, sticky="ew", padx=tema.ESPACO, pady=(0, tema.ESPACO))
        self.caixa_log.configure(state="disabled")

    # ================================================================================== utilidades

    def _coletar_lixo(self) -> None:
        """Coleta de lixo SEMPRE na thread do Tk (ver o `gc.disable()` no construtor)."""
        try:
            gc.collect()
        finally:
            if not self._encerrando:
                self.after(3000, self._coletar_lixo)

    def _drenar_fila_ui(self) -> None:
        """Executa, na thread da interface, o que as threads de fundo entregaram. Sempre se reagenda:
        um callback com erro não pode parar a entrega de todos os próximos."""
        try:
            while True:
                try:
                    funcao = self._fila_ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    funcao()
                except Exception:  # noqa: BLE001 — um callback ruim não pode parar a fila
                    import traceback
                    traceback.print_exc()
                    try:
                        self.log(f"⚠ Erro interno: {traceback.format_exc().strip().splitlines()[-1]}")
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if not self._encerrando:
                self.after(40, self._drenar_fila_ui)

    def log(self, texto: str, cor: str | None = None) -> None:
        self.caixa_log.configure(state="normal")
        self.caixa_log.insert("end", f"{time.strftime('%H:%M:%S')}  {texto}\n")
        self.caixa_log.see("end")
        self.caixa_log.configure(state="disabled")

    def _foco_em(self, entrada: ctk.CTkEntry) -> bool:
        """O usuário está digitando neste campo? (não sobrescrever o que ele escreve)"""
        try:
            return self.focus_get() is getattr(entrada, "_entry", entrada)
        except KeyError:
            return False

    def _status(self, chip: ctk.CTkLabel, texto: str, cor: str) -> None:
        chip.configure(text=f"● {texto}", text_color=cor)

    def _em_segundo_plano(self, tarefa, sucesso, falha=None) -> None:
        """Roda [tarefa] numa thread; [sucesso]/[falha] voltam na thread da interface."""
        futuro = self.pool.submit(tarefa)
        execucao.ao_terminar(futuro, self.despachar, sucesso, falha or (lambda e: self.log(f"Erro: {e}")))

    # ================================================================================== inicialização

    def _iniciar(self) -> None:
        problemas = self.cfg.problemas()
        self._status(self.chip_tmdb, "TMDB: chave ok" if self.cfg.tmdb_key else "TMDB: sem chave",
                     tema.SUCESSO if self.cfg.tmdb_key else tema.ERRO)
        self._atualizar_pendentes()
        if problemas:
            for p in problemas:
                self.log(f"⚠ {p}")
            self.after(300, self._abrir_configuracoes)
            return
        self._checar_banco()
        self._conectar()

    def _checar_banco(self) -> None:
        armazenamento = self.servico.armazenamento
        if not armazenamento.ativo:
            self._banco_ok = None
            self._status(self.chip_banco, "Registro: não configurado", tema.TEXTO_SUAVE)
            self.log("Registro do bot não configurado: o app posta sem checar duplicidade nem registrar; "
                     "o monitor do bot sincroniza o canal depois (Configurações → Registro do bot).")
            return
        self._status(self.chip_banco, "Registro: verificando…", tema.TEXTO_SUAVE)

        def tarefa():
            self.servico.testar_registro()  # leitura barata: só prova que dá para ler o registro
            return armazenamento.descricao

        def ok(descricao):
            self._banco_ok = True
            self._status(self.chip_banco, f"Registro: {descricao}", tema.SUCESSO)

        def falha(erro):
            self._banco_ok = False
            self._status(self.chip_banco, "Registro: fora do ar", tema.ERRO)
            self.log(f"⚠ Não consegui ler o registro do bot ({erro}). Dá para postar, mas sem checar "
                     "duplicidade nem registrar; o monitor sincroniza depois.")

        self._em_segundo_plano(tarefa, ok, falha)

    # ================================================================================== Telegram

    def _conectar(self) -> None:
        if self.cfg.problemas():
            self._abrir_configuracoes()
            return
        self._status(self.chip_telegram, "Telegram: conectando…", tema.ALERTA)
        self.botao_conectar.configure(state="disabled")

        async def rotina():
            if not await self.servico.conectar():
                return None
            nome = await self.servico.nome_do_usuario()
            await self.servico.resolver_canal()  # confirma que a conta enxerga o canal
            return nome

        execucao.ao_terminar(self.loop.rodar(rotina()), self.despachar, self._conectado, self._conexao_falhou)

    def _conectado(self, nome: str | None) -> None:
        self.botao_conectar.configure(state="normal")
        if nome is None:
            self._telegram_ok = False
            self._status(self.chip_telegram, "Telegram: entrar na conta", tema.ALERTA)
            self._abrir_login()
            return
        self._telegram_ok = True
        destino = "TESTE" if self.cfg.modo_teste else "produção"
        self._status(self.chip_telegram, f"Telegram: {nome} → canal de {destino}",
                     tema.ALERTA if self.cfg.modo_teste else tema.SUCESSO)
        self.log(f"Conectado como {nome}. Canal ativo: {self.cfg.canal_ativo} ({destino}).")
        if self._login is not None:
            self._login.fechar_ok()
            self._login = None
        self._atualizar_detalhe()

    def _conexao_falhou(self, erro: BaseException) -> None:
        self.botao_conectar.configure(state="normal")
        self._telegram_ok = False
        self._status(self.chip_telegram, "Telegram: falhou", tema.ERRO)
        self.log(f"⚠ Telegram: {erro}")
        messagebox.showerror("Telegram", f"Não consegui conectar ao Telegram ou ao canal.\n\n{erro}")

    def _abrir_login(self) -> None:
        if self._login is not None:
            return
        self._login = DialogoLogin(self, self._login_telefone, self._login_fechado)

    def _login_fechado(self) -> None:
        self._login = None

    def _login_telefone(self, telefone: str) -> None:
        dialogo = self._login

        async def pedir(etapa: str) -> str:
            ponte: concurrent.futures.Future = concurrent.futures.Future()
            self.despachar(lambda: ponte.set_result(dialogo.aguardar(etapa)))
            futuro_ui = await asyncio.wrap_future(ponte)
            return await asyncio.wrap_future(futuro_ui)

        async def rotina():
            await self.servico.entrar(telefone, lambda: pedir("codigo"), lambda: pedir("senha"))
            nome = await self.servico.nome_do_usuario()
            await self.servico.resolver_canal()
            return nome

        def falha(erro: BaseException) -> None:
            if dialogo is not None and self._login is dialogo:
                dialogo.erro(f"Não deu certo: {erro}")
            self.log(f"⚠ Login do Telegram: {erro}")

        execucao.ao_terminar(self.loop.rodar(rotina()), self.despachar, self._conectado, falha)

    # ================================================================================== configurações

    def _abrir_configuracoes(self) -> None:
        DialogoConfiguracoes(self, self.cfg, self._configuracao_salva)

    def _configuracao_salva(self, nova: Configuracao) -> None:
        mudou_conta = (nova.api_id, nova.api_hash) != (self.cfg.api_id, self.cfg.api_hash)
        self.cfg = nova
        self.servico.reconfigurar(nova)
        self.log(f"Configurações salvas. Canal ativo: {nova.canal_ativo or '—'}"
                 f"{' (modo teste)' if nova.modo_teste else ''}; parte de {divisao.humano(nova.tamanho_parte)}.")
        self._status(self.chip_tmdb, "TMDB: chave ok" if nova.tmdb_key else "TMDB: sem chave",
                     tema.SUCESSO if nova.tmdb_key else tema.ERRO)
        if mudou_conta and self.servico.cliente is not None:
            execucao.ao_terminar(self.loop.rodar(self.servico.desconectar()), self.despachar,
                                 lambda _: self._conectar(), lambda e: self._conectar())
        elif not nova.problemas():
            self._checar_banco()
            self._conectar()
        self._atualizar_detalhe()

    # ================================================================================== fila

    def _adicionar(self) -> None:
        caminhos = filedialog.askopenfilenames(
            parent=self, title="Escolha os filmes",
            filetypes=[("Vídeos", "*.mkv *.mp4 *.m4v *.avi *.mov *.webm *.wmv *.ts *.mpg *.mpeg"), ("Todos", "*.*")],
        )
        novos = []
        for caminho in caminhos:
            if not eh_video(caminho):
                self.log(f"Ignorado (não parece vídeo): {os.path.basename(caminho)}")
                continue
            if any(i.caminho == caminho for i in self.itens):
                continue
            try:
                novos.append(ItemFila.criar(caminho))
            except OSError as erro:
                self.log(f"Não consegui ler {caminho}: {erro}")
        self.itens.extend(novos)
        self._atualizar_fila()
        if novos:
            self._selecionar(novos[0])
            for item in novos:
                self._buscar(item, automatico=True)

    def _remover(self) -> None:
        item = self.sel
        if item is None or item is self.postando:
            return
        self.itens.remove(item)
        self.sel = self.itens[0] if self.itens else None
        self._atualizar_fila()
        self._atualizar_detalhe()

    def _selecionar(self, item: ItemFila) -> None:
        self.sel = item
        self._trocando = False
        self._progresso_fixo = False
        self._atualizar_fila()
        self._atualizar_detalhe()

    def _atualizar_fila(self) -> None:
        for filho in self.lista_fila.winfo_children():
            filho.destroy()
        for item in self.itens:
            ativo = item is self.sel
            linha = ctk.CTkFrame(self.lista_fila, fg_color=tema.PAINEL_2 if ativo else "transparent",
                                 corner_radius=8, border_width=1 if ativo else 0, border_color=tema.DESTAQUE)
            linha.pack(fill="x", pady=2, padx=2)
            nome = item.nome_final or item.nome_original
            titulo = _txt(linha, nome if len(nome) <= 38 else nome[:35] + "…", font=ctk.CTkFont(weight="bold"))
            titulo.pack(anchor="w", padx=10, pady=(6, 0))
            estado = item.mensagem or tema.ROTULO_ESTADO.get(item.estado, item.estado)
            sub = _txt(linha, f"{divisao.humano(item.tamanho)} · {estado}",
                       text_color=tema.COR_ESTADO.get(item.estado, tema.TEXTO_SUAVE))
            sub.pack(anchor="w", padx=10, pady=(0, 6))
            for widget in (linha, titulo, sub):
                widget.bind("<Button-1>", lambda e, i=item: self._selecionar(i))
        prontos = sum(1 for i in self.itens if i.estado == PRONTO)
        self.botao_postar_todos.configure(
            text=f"Postar todos os prontos ({prontos})", state="normal" if prontos and not self.postando else "disabled")

    # ================================================================================== busca no TMDB

    def _buscar(self, item: ItemFila | None = None, *, automatico: bool = False) -> None:
        item = item or self.sel
        if item is None:
            return
        if not self.cfg.tmdb_key:
            self.log("⚠ Sem chave do TMDB (TMDB_API_KEY no .env do bot).")
            return
        if not automatico and item is self.sel:
            item.termo_busca = self.var_titulo.get().strip()
            item.ano_busca = self.var_ano.get().strip() or None
            self._trocando = True  # buscou de novo: mostra os resultados mesmo com filme escolhido
        termo, ano = item.termo_busca, item.ano_busca
        id_explicito = None
        try:
            id_explicito = busca.resolver_entrada(termo)
        except ValueError as erro:
            self.log(f"⚠ {erro}")
            return
        if id_explicito:
            self._escolher_por_id(item, id_explicito)
            return
        if item is self.sel:
            self.lbl_busca.configure(text="Buscando…")

        def tarefa():
            return busca.buscar(termo, self.cfg.tmdb_key, ano)

        def pronto(candidatos):
            item.candidatos = candidatos
            if item is self.sel:
                self._atualizar_candidatos()
            if automatico:
                self.log(f"{item.nome_original}: {len(candidatos)} candidato(s) no TMDB para “{termo}”.")

        def falha(erro):
            if item is self.sel:
                self.lbl_busca.configure(text=f"Não consegui buscar no TMDB: {erro}", text_color=tema.ERRO)
            self.log(f"⚠ TMDB: {erro}")

        self._em_segundo_plano(tarefa, pronto, falha)

    def _escolher_por_id(self, item: ItemFila, tmdb_id: int) -> None:
        self._escolher(item, busca.Candidato(tmdb_id, f"TMDB {tmdb_id}", None, None, None))

    def _escolher(self, item: ItemFila, candidato: busca.Candidato) -> None:
        self.lbl_busca.configure(text=f"Carregando “{candidato.titulo}”…", text_color=tema.TEXTO_SUAVE)

        def tarefa():
            dados = busca.detalhar(candidato.tmdb_id, self.cfg.tmdb_key)
            duplicado, aviso = None, ""
            try:
                duplicado = self.servico.ja_publicado(dados["tmdb_id"])
            except Exception as erro:  # noqa: BLE001 — sem banco só perdemos a checagem
                aviso = f"Não deu para checar duplicidade (banco indisponível): {erro}"
            return dados, duplicado, aviso

        def pronto(resultado):
            dados, duplicado, aviso = resultado
            self._trocando = False
            item.escolher_filme(dados)
            item.duplicado, item.aviso = duplicado, aviso
            if item is self.sel:
                self._atualizar_fila()
                self._atualizar_detalhe()
            self.log(f"{item.nome_original} → {dados.get('titulo')} ({dados.get('ano')}), TMDB {dados.get('tmdb_id')}"
                     f"{'  ⚠ JÁ PUBLICADO' if duplicado else ''}")

        def falha(erro):
            self.lbl_busca.configure(text=f"Não consegui carregar o filme: {erro}", text_color=tema.ERRO)
            self.log(f"⚠ TMDB: {erro}")

        self._em_segundo_plano(tarefa, pronto, falha)

    def _atualizar_candidatos(self) -> None:
        for filho in self.lista_candidatos.winfo_children():
            filho.destroy()
        item = self.sel
        if item is None:
            return
        if item.dados is not None and not self._trocando:
            self.lbl_busca.configure(text="Filme escolhido. Para trocar, edite a busca e clique em Buscar.",
                                     text_color=tema.TEXTO_SUAVE)
            return
        if not item.candidatos:
            self.lbl_busca.configure(text="Nenhum filme encontrado. Ajuste o título/ano ou use tmdb:ID.",
                                     text_color=tema.ALERTA)
            return
        self.lbl_busca.configure(text=f"{len(item.candidatos)} resultado(s) — escolha o certo:", text_color=tema.TEXTO_SUAVE)
        for candidato in item.candidatos[:6]:
            escolhido = item.dados is not None and item.dados.get("tmdb_id") == candidato.tmdb_id
            linha = ctk.CTkFrame(self.lista_candidatos, fg_color=tema.PAINEL_2, corner_radius=8,
                                 border_width=1 if escolhido else 0, border_color=tema.DESTAQUE)
            linha.pack(fill="x", pady=3)
            poster = ctk.CTkLabel(linha, text="", width=46, height=69)
            poster.pack(side="left", padx=8, pady=6)
            self._carregar_poster(poster, candidato.miniatura_url, (46, 69))
            _txt(linha, candidato.rotulo, font=ctk.CTkFont(weight="bold"), wraplength=560).pack(
                side="left", fill="x", expand=True)
            botao(linha, "✓ Escolhido" if escolhido else "Escolher",
                  lambda c=candidato: self._escolher(item, c), primario=not escolhido, largura=110).pack(
                side="right", padx=10)

    def _carregar_poster(self, rotulo: ctk.CTkLabel, url: str | None, tamanho: tuple[int, int]) -> None:
        def pronta(imagem) -> None:
            if imagem is None:
                return

            def aplicar() -> None:
                try:
                    ctk_img = ctk.CTkImage(light_image=imagem, dark_image=imagem, size=tamanho)
                    self._miniaturas_refs.append(ctk_img)  # o Tk não segura referência sozinho
                    rotulo.configure(image=ctk_img)
                except tk.TclError:
                    pass  # a linha já foi destruída (lista refeita)

            self.despachar(aplicar)

        miniaturas.obter(url, pronta)

    # ================================================================================== detalhe

    def _atualizar_detalhe(self) -> None:
        item = self.sel
        if item is None:
            self.conteudo.pack_forget()
            self.painel_acao.grid_remove()
            self.vazio.pack(pady=80)
            return
        self.vazio.pack_forget()
        self.conteudo.pack(fill="x")
        self.painel_acao.grid()

        self.lbl_arquivo.configure(text=item.nome_original)
        self.lbl_arquivo_info.configure(text=f"{divisao.humano(item.tamanho)} · {os.path.dirname(item.caminho)}")
        if not self._foco_em(self.entrada_titulo):
            self.var_titulo.set(item.termo_busca)
            self.var_ano.set(item.ano_busca or "")
        self._atualizar_candidatos()
        self._atualizar_cartao_escolhido(item)

        if not self._foco_em(self.entrada_nome):
            self.var_nome.set(item.nome_final)
        self.var_qualidade.set(item.qualidade or "")
        self.var_audio.set(item.audio or "")
        self.lbl_plano.configure(text=item.resumo_plano(self.cfg.tamanho_parte) if item.nome_final else
                                 "Escolha o filme para ver o plano de corte")
        texto, _ = item.legenda()
        self.lbl_legenda.configure(text=item.descricao_da_legenda() or "A legenda aparece depois de escolher o filme.")
        self.texto_legenda.configure(state="normal")
        self.texto_legenda.delete("1.0", "end")
        self.texto_legenda.insert("1.0", texto)
        self.texto_legenda.configure(state="disabled")

        avisos = []
        if item.duplicado:
            avisos.append("⛔ " + next((p for p in item.pendencias(self.cfg.tamanho_parte, self.cfg.limite)
                                      if "já foi publicado" in p), "Filme já publicado"))
        if item.aviso:
            avisos.append("⚠ " + item.aviso)
        if self._banco_ok is False and not item.aviso:
            avisos.append("⚠ Registro do bot fora do ar: não dá para checar duplicidade nem registrar o filme agora.")
        elif self._banco_ok is None and not item.aviso and not self.servico.armazenamento.ativo:
            avisos.append("⚠ Registro do bot não configurado: não checa duplicidade; o monitor registra depois.")
        self.lbl_aviso.configure(text="\n".join(avisos), text_color=tema.ERRO if item.duplicado else tema.ALERTA)

        enviando = self.postando is not None
        pode = (not enviando and self._telegram_ok and not item.pendencias(self.cfg.tamanho_parte, self.cfg.limite))
        self.botao_postar.configure(state="normal" if pode else "disabled",
                                    text="Retomar envio…" if item.job_id and item.estado in (ERRO, CANCELADO) else "Postar no canal…")
        self.botao_cortar.configure(state="normal" if item.dados and not enviando and len(item.plano(self.cfg.tamanho_parte)) > 1
                                    else "disabled")
        self.botao_cancelar.configure(state="normal" if enviando else "disabled")
        self._mostrar_progresso(enviando or self._progresso_fixo)
        destino = f"{'TESTE' if self.cfg.modo_teste else 'produção'}: {self.cfg.canal_ativo or '—'}"
        self.lbl_destino.configure(text=f"Destino → {destino}",
                                   text_color=tema.ALERTA if self.cfg.modo_teste else tema.TEXTO_SUAVE)

    def _atualizar_cartao_escolhido(self, item: ItemFila) -> None:
        for filho in self.cartao_escolhido.winfo_children():
            filho.destroy()
        if not item.dados:
            self.cartao_escolhido.pack_forget()
            return
        self.cartao_escolhido.pack(fill="x", pady=(8, 0))
        d = item.dados
        if d.get("poster_url"):  # sem pôster não reserva o espaço em branco
            poster = ctk.CTkLabel(self.cartao_escolhido, text="", width=92, height=138)
            poster.pack(side="left", padx=10, pady=10)
            self._carregar_poster(poster, d["poster_url"].replace("/w780/", "/w185/"), (92, 138))
        corpo = ctk.CTkFrame(self.cartao_escolhido, fg_color="transparent")
        corpo.pack(side="left", fill="both", expand=True, padx=(14 if not d.get("poster_url") else 0, 10), pady=10)
        _txt(corpo, f"✓ {d.get('titulo')} ({d.get('ano') or '—'})", font=ctk.CTkFont(size=15, weight="bold"),
             text_color=tema.SUCESSO).pack(anchor="w")
        meta = " · ".join(str(x) for x in (
            f"{d.get('duracao')} min" if d.get("duracao") else None,
            ", ".join(d.get("generos") or []) or None,
            f"Dir. {d['diretor']}" if d.get("diretor") else None,
            f"TMDB {d.get('tmdb_id')}",
        ) if x)
        _txt(corpo, meta, text_color=tema.TEXTO_SUAVE, wraplength=620).pack(anchor="w", pady=(2, 4))
        sinopse = (d.get("sinopse") or "").strip()
        _txt(corpo, (sinopse[:280] + "…") if len(sinopse) > 280 else sinopse, wraplength=620,
             text_color=tema.TEXTO).pack(anchor="w")

    def _mostrar_progresso(self, visivel: bool) -> None:
        if visivel:
            self.area_progresso.pack(fill="x")
        else:
            self.area_progresso.pack_forget()

    def _nome_editado(self) -> None:
        item = self.sel
        if item and item.dados and self.var_nome.get() != item.nome_final:
            item.definir_nome(self.var_nome.get())
            self._atualizar_fila()
            self._atualizar_detalhe()

    def _qualidade_editada(self) -> None:
        item = self.sel
        if item and (self.var_qualidade.get().strip() or None) != item.qualidade:
            item.definir_qualidade(self.var_qualidade.get())
            self._atualizar_fila()
            self._atualizar_detalhe()

    def _audio_editado(self) -> None:
        item = self.sel
        if item:
            item.audio = self.var_audio.get().strip() or None
            self._atualizar_detalhe()

    # ================================================================================== só cortar

    def _so_cortar(self) -> None:
        item = self.sel
        if item is None or not item.dados:
            return
        plano = item.plano(self.cfg.tamanho_parte)
        if len(plano) < 2:
            messagebox.showinfo("Só cortar", "O arquivo cabe numa parte só: não precisa cortar.")
            return
        pasta = filedialog.askdirectory(parent=self, title="Pasta onde gravar as partes",
                                        initialdir=os.path.dirname(item.caminho))
        if not pasta:
            return
        destino = os.path.join(pasta, item.nome_final + ".partes")
        self.log(f"Cortando {item.nome_final} em {len(plano)} partes → {destino}")
        self._reiniciar_progresso()
        self._progresso_fixo = True
        self._mostrar_progresso(True)
        self.barra_parte.set(0)
        self.barra_total.set(0)
        self.lbl_parte.configure(text="Gravando as partes…")

        ultimo = [0.0]

        def progresso(feito: int, total: int) -> None:
            agora = time.monotonic()  # a tela só precisa de ~5 atualizações por segundo
            if feito < total and agora - ultimo[0] < 0.2:
                return
            ultimo[0] = agora
            self.despachar(lambda: self._mostrar_progresso_corte(feito, total))

        def tarefa():
            return divisao.gravar_partes(item.caminho, plano, destino, progresso=progresso)

        def pronto(manifesto):
            self.barra_total.set(1)
            self.lbl_parte.configure(text="")
            self.lbl_total.configure(text=f"Pronto: {len(manifesto['parts'])} partes gravadas")
            self.log(f"✔ Partes gravadas em {destino}. Guarde o .manifest.json; suba só os .partNNofMM.")
            messagebox.showinfo("Só cortar", f"{len(manifesto['parts'])} partes gravadas em:\n{destino}")

        def falha(erro):
            self.lbl_parte.configure(text="")
            self.log(f"⚠ Não consegui cortar: {erro}")
            messagebox.showerror("Só cortar", str(erro))

        self._em_segundo_plano(tarefa, pronto, falha)

    def _mostrar_progresso_corte(self, feito: int, total: int) -> None:
        fracao = feito / total if total else 0
        self.barra_total.set(fracao)
        self.lbl_total.configure(text=f"{divisao.humano(feito)} de {divisao.humano(total)} ({fracao:.0%})")

    # ================================================================================== postagem

    def _reiniciar_progresso(self) -> None:
        self._bytes_total = 0
        self._bytes_concluidos = 0
        self._amostras: collections.deque = collections.deque(maxlen=64)

    def _postar_selecionado(self) -> None:
        item = self.sel
        if item is None:
            return
        retomando = bool(item.job_id and item.estado in (ERRO, CANCELADO))
        problemas = self._problemas_para_postar(item, ignorar_duplicidade=retomando)
        if problemas:
            messagebox.showwarning("Ainda não dá para postar", "\n".join(f"• {p}" for p in problemas))
            return
        d = item.dados or {}
        linhas = [
            ("Filme", f"{d.get('titulo')} ({d.get('ano')}) — TMDB {d.get('tmdb_id')}"),
            ("Arquivo", item.nome_final),
            ("Plano", item.resumo_plano(self.cfg.tamanho_parte)),
            ("Legenda", item.descricao_da_legenda()),
        ]
        if retomando:
            linhas.append(("Retomada", "Só sobe o que falta (confere o canal antes)."))
        DialogoConfirmar(self, linhas, str(self.cfg.canal_ativo), self.cfg.modo_teste,
                         lambda: self._iniciar_postagem([item]))

    def _postar_todos(self) -> None:
        prontos = [i for i in self.itens if i.estado == PRONTO]
        if not prontos:
            return
        for item in prontos:
            problemas = self._problemas_para_postar(item)
            if problemas:
                messagebox.showwarning("Ainda não dá para postar",
                                       f"{item.nome_original}:\n" + "\n".join(f"• {p}" for p in problemas))
                return
        linhas = [("Filmes", f"{len(prontos)} filmes na fila"),
                  ("Primeiro", prontos[0].nome_final), ("Ordem", "um de cada vez, na ordem da fila")]
        DialogoConfirmar(self, linhas, str(self.cfg.canal_ativo), self.cfg.modo_teste,
                         lambda: self._iniciar_postagem(prontos))

    def _problemas_para_postar(self, item: ItemFila, ignorar_duplicidade: bool = False) -> list[str]:
        problemas = list(self.cfg.problemas())
        if not self._telegram_ok:
            problemas.append("Conecte ao Telegram (botão no topo)")
        if self.postando is not None:
            problemas.append("Já há um envio em andamento")
        faltas = item.pendencias(self.cfg.tamanho_parte, self.cfg.limite)
        if ignorar_duplicidade:
            faltas = [f for f in faltas if "já foi publicado" not in f]
        return problemas + faltas

    def _iniciar_postagem(self, itens: list[ItemFila]) -> None:
        self._fila_envio = list(itens)
        self._proximo_envio()

    def _proximo_envio(self) -> None:
        if not self._fila_envio:
            self.postando = None
            self._atualizar_fila()
            self._atualizar_detalhe()
            return
        item = self._fila_envio.pop(0)
        try:
            job = self.servico.repositorio.carregar(item.job_id) if item.job_id and item.estado in (ERRO, CANCELADO) else None
            job = job or self.servico.novo_job(item)
        except Exception as erro:  # noqa: BLE001
            item.estado, item.mensagem = ERRO, str(erro)
            self.log(f"⚠ {item.nome_original}: {erro}")
            self._fila_envio.clear()
            self._atualizar_fila()
            self._atualizar_detalhe()
            return
        self._rodar_job(item, job)

    def _rodar_job(self, item: ItemFila, job: Job) -> None:
        self.postando, self._job_atual = item, job
        item.job_id, item.estado, item.mensagem = job.id, ENVIANDO, ""
        self.cancelamento.zerar()
        self._reiniciar_progresso()
        self._bytes_total = sum(p.tamanho for p in job.partes if not p.message_id)
        self.barra_parte.set(0)
        self.barra_total.set(0)
        self.log(f"▶ Postando {job.nome_base} ({job.total_partes} parte(s)) no canal {job.canal}")
        self._atualizar_fila()
        self._atualizar_detalhe()

        ultimo = [0.0]

        def emitir(evento: Evento) -> None:
            if evento.tipo == "progresso":  # centenas por segundo: a tela só precisa de ~5
                agora = time.monotonic()
                if evento.feito < evento.total and agora - ultimo[0] < 0.2:
                    return
                ultimo[0] = agora
            self.despachar(lambda: self._evento(item, job, evento))

        futuro = self.loop.rodar(self.servico.postar(job, emitir, self.cancelamento))
        execucao.ao_terminar(futuro, self.despachar,
                             lambda chave: self._postou(item, chave), lambda erro: self._falhou(item, job, erro))

    def _evento(self, item: ItemFila, job: Job, ev: Evento) -> None:
        if ev.tipo == "parte_inicio":
            self.lbl_parte.configure(text=f"Enviando parte {ev.parte}/{ev.total_partes}: {ev.mensagem}")
            self.barra_parte.set(0)
        elif ev.tipo == "progresso":
            self.barra_parte.set(ev.feito / ev.total if ev.total else 0)
            self._atualizar_total(self._bytes_concluidos + ev.feito)
        elif ev.tipo == "parte_ok":
            tamanho = next((p.tamanho for p in job.partes if p.indice == ev.parte), 0)
            self._bytes_concluidos += tamanho
            self._atualizar_total(self._bytes_concluidos)
            self.log(f"  ✔ parte {ev.parte}/{ev.total_partes} no canal (mensagem {ev.dados.get('message_id')})")
        elif ev.tipo == "texto_ok":
            self.log("  ✔ texto com os metadados no canal")
        elif ev.tipo == "registrado":
            self.log(f"  ✔ registrado no bot ({ev.mensagem})")
        elif ev.tipo == "indice":
            if ev.mensagem == "sem_registro":
                self.log("  ⚠ Registro do bot não configurado: o monitor do bot registra o filme na próxima varredura.")
            elif ev.mensagem == "bot_parado":
                self.log("  ⚠ O bot não está rodando: o índice de busca do app só atualiza quando ele subir.")
            else:
                self.log("  ✔ pedido de atualização do índice enviado ao bot")
        elif ev.tipo == "erro":
            self.log(f"  ⚠ {ev.mensagem}")

    def _atualizar_total(self, enviado: int) -> None:
        agora = time.monotonic()
        self._amostras.append((agora, enviado))
        fracao = enviado / self._bytes_total if self._bytes_total else 0
        self.barra_total.set(min(fracao, 1))
        texto = f"{divisao.humano(enviado)} de {divisao.humano(self._bytes_total)} ({fracao:.0%})"
        recentes = [(t, b) for t, b in self._amostras if agora - t <= 10]
        if len(recentes) >= 2 and recentes[-1][0] - recentes[0][0] >= 1.0:
            velocidade = (recentes[-1][1] - recentes[0][1]) / (recentes[-1][0] - recentes[0][0])
            if velocidade > 0:
                faltam = (self._bytes_total - enviado) / velocidade
                texto += f" · {formatar_velocidade(velocidade)} · faltam ~{formatar_tempo(faltam)}"
        self.lbl_total.configure(text=texto)

    def _postou(self, item: ItemFila, chave: str) -> None:
        item.estado, item.mensagem = CONCLUIDO, ""
        item.job_id = None
        self.barra_parte.set(1)
        self.barra_total.set(1)
        self.lbl_parte.configure(text="")
        self.lbl_total.configure(text="Filme postado e registrado")
        self._progresso_fixo = True
        self.log(f"✔ {item.nome_final} postado ({chave}).")
        self._proximo_envio()
        if not self._fila_envio and self.postando is None:
            messagebox.showinfo("Postado", f"{item.nome_final}\n\nPostado no canal e registrado no bot.")

    def _falhou(self, item: ItemFila, job: Job, erro: BaseException) -> None:
        self._fila_envio.clear()
        self.postando = None
        self.lbl_parte.configure(text="")
        if isinstance(erro, PublicacaoCancelada):
            item.estado, item.mensagem = CANCELADO, "cancelado — dá para retomar"
            self.log("■ Envio cancelado.")
            self._atualizar_fila()
            self._atualizar_detalhe()
            if job.tem_envios and messagebox.askyesno(
                    "Envio cancelado",
                    "Algumas partes já estão no canal.\n\nApagar o que já foi enviado? (Não = guardar para retomar depois)"):
                execucao.ao_terminar(self.loop.rodar(self.servico.descartar(job)), self.despachar,
                                     lambda _: self._descartado(item), lambda e: self.log(f"⚠ Não consegui apagar: {e}"))
            return
        if isinstance(erro, registro.JaPublicado):
            item.estado, item.mensagem, item.duplicado = NOVO, "já publicado", erro.registro
            self.log(f"⛔ {item.nome_final}: já publicado ({erro.chave}). Nada foi enviado.")
        else:
            item.estado, item.mensagem = ERRO, str(erro)[:80]
            self.log(f"⚠ Falha no envio: {erro}. As partes já enviadas ficam salvas: use Retomar.")
        self._atualizar_fila()
        self._atualizar_detalhe()
        messagebox.showerror("Falha na postagem", str(erro))

    def _descartado(self, item: ItemFila) -> None:
        item.estado, item.mensagem, item.job_id = PRONTO, "", None
        self.log("O que já tinha subido foi apagado do canal.")
        self._atualizar_fila()
        self._atualizar_detalhe()

    def _cancelar(self) -> None:
        if self.postando is not None:
            self.cancelamento.cancelar()
            self.log("Cancelando… (termina o bloco atual)")

    # ================================================================================== envios interrompidos

    def _atualizar_pendentes(self) -> None:
        try:
            n = len(self.servico.pendentes())
        except Exception:  # noqa: BLE001
            n = 0
        self.botao_pendentes.configure(text=f"Envios interrompidos ({n})" if n else "Envios interrompidos")

    def _abrir_pendentes(self) -> None:
        DialogoPendentes(self, self.servico.pendentes(), self._retomar_job, self._descartar_job)

    def _retomar_job(self, job: Job) -> None:
        if not self._telegram_ok:
            messagebox.showwarning("Retomar", "Conecte ao Telegram primeiro.")
            return
        if self.postando is not None:
            messagebox.showwarning("Retomar", "Já há um envio em andamento.")
            return
        if job.arquivo_mudou():
            messagebox.showerror("Retomar", "O arquivo original mudou ou sumiu; não dá para retomar.")
            return
        item = next((i for i in self.itens if i.job_id == job.id or i.caminho == job.origem), None)
        if item is None:
            item = ItemFila.criar(job.origem)
            self.itens.append(item)
        item.dados, item.nome_final, item.qualidade, item.audio = job.dados_tmdb, job.nome_base, job.qualidade, job.audio
        self._selecionar(item)
        self._rodar_job(item, job)

    def _descartar_job(self, job: Job) -> None:
        if not self._telegram_ok:
            messagebox.showwarning("Descartar", "Conecte ao Telegram primeiro (para apagar do canal o que já subiu).")
            return
        execucao.ao_terminar(self.loop.rodar(self.servico.descartar(job)), self.despachar,
                             lambda _: (self.log(f"Descartado: {job.nome_base}"), self._atualizar_pendentes()),
                             lambda e: self.log(f"⚠ Não consegui descartar: {e}"))

    # ================================================================================== sair

    def _sair(self) -> None:
        if self.postando is not None and not messagebox.askyesno(
                "Sair", "Há um envio em andamento. Sair agora o interrompe (dá para retomar depois).\n\nSair mesmo?"):
            return
        self._encerrando = True
        self.cancelamento.cancelar()
        try:
            self.loop.esperar(self.servico.desconectar(), timeout=5)
        except Exception:  # noqa: BLE001 — fechando de qualquer jeito
            pass
        self.loop.parar()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()
        gc.enable()
