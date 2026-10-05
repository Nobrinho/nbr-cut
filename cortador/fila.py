"""Fila de filmes da tela: um [ItemFila] por arquivo, com toda a regra de negócio (sem Tk).

O item nasce do caminho do arquivo (sugestão de título/ano/qualidade pelo nome), recebe o filme
escolhido no TMDB e dali tira o nome padronizado, o plano de corte e a prévia da legenda. A interface
só lê e escreve campos daqui.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from cortador import divisao, midia as midia_mod, nomes
from cortador.busca import Candidato
from cortador.compartilhado.legenda import LIMITE_LEGENDA_MIDIA, montar_legenda_filme

NOVO, PRONTO, ENVIANDO, CONCLUIDO, ERRO, CANCELADO, OTIMIZANDO = (
    "novo", "pronto", "enviando", "concluido", "erro", "cancelado", "otimizando",
)

EXTENSOES_DE_VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".mpg", ".mpeg")


def eh_video(caminho: str) -> bool:
    return nomes.extensao_de(caminho) in EXTENSOES_DE_VIDEO


@dataclass
class ItemFila:
    caminho: str
    tamanho: int
    sugestao: dict
    termo_busca: str
    ano_busca: str | None
    candidatos: list[Candidato] = field(default_factory=list)
    dados: dict | None = None          # detalhes completos do TMDB do filme escolhido
    nome_final: str = ""
    nome_editado: bool = False         # o usuário mexeu no nome: não sobrescrever
    qualidade: str | None = None
    audio: str | None = None
    estado: str = NOVO
    mensagem: str = ""
    job_id: str | None = None
    # Registro existente do mesmo filme (já publicado): bloqueia a postagem.
    duplicado: dict | None = None
    # Aviso informativo (ex.: não deu para checar a duplicidade porque o banco está fora do ar).
    aviso: str = ""
    # Metadados lidos do arquivo (codec, HDR/Dolby Vision, áudios...); None até a leitura terminar.
    midia: midia_mod.InfoMidia | None = None
    midia_erro: str = ""
    # Cópia otimizada para streaming (otimizar.py): `caminho` passa a apontar para ela; o original fica aqui.
    caminho_original: str | None = None
    tamanho_original: int = 0
    perfil_otimizado: str | None = None
    estado_antes_de_otimizar: str = NOVO

    @classmethod
    def criar(cls, caminho: str) -> "ItemFila":
        info = os.stat(caminho)
        sugestao = nomes.sugestao_do_arquivo(caminho)
        qualidade = sugestao["qualidade"]
        return cls(
            caminho=os.fspath(caminho),
            tamanho=info.st_size,
            sugestao=sugestao,
            termo_busca=sugestao["titulo"],
            ano_busca=sugestao["ano"],
            qualidade=None if qualidade == "Qualidade não informada" else qualidade,
            audio=sugestao["audio"],
        )

    @property
    def nome_original(self) -> str:
        return os.path.basename(self.caminho_original or self.caminho)

    @property
    def extensao(self) -> str:
        return nomes.extensao_de(self.caminho)

    # ------------------------------------------------------------------ metadados do arquivo

    def definir_midia(self, info: midia_mod.InfoMidia) -> None:
        """Guarda o que foi lido do arquivo. A qualidade só é preenchida se o nome do arquivo não informava."""
        self.midia = info
        self.midia_erro = ""
        if self.qualidade is None and info.qualidade_sugerida:
            self.qualidade = info.qualidade_sugerida
            if self.dados and not self.nome_editado:
                self.nome_final = self.nome_sugerido()

    def avisos_midia(self) -> list[midia_mod.Aviso]:
        return midia_mod.avaliar(self.midia, self.tamanho) if self.midia else []

    @property
    def duracao_real_s(self) -> int | None:
        return int(self.midia.duracao_s) if self.midia and self.midia.duracao_s else None

    # ------------------------------------------------------------------ cópia otimizada

    @property
    def otimizado(self) -> bool:
        return self.caminho_original is not None

    def aplicar_otimizado(self, caminho: str, perfil_id: str) -> None:
        """A cópia otimizada passa a ser o arquivo a cortar e postar (o original fica guardado)."""
        if self.caminho_original is None:
            self.caminho_original, self.tamanho_original = self.caminho, self.tamanho
        self.caminho = os.fspath(caminho)
        self.tamanho = os.stat(caminho).st_size
        self.perfil_otimizado = perfil_id
        self.midia, self.midia_erro = None, ""              # a tela lê de novo (bitrate, trilhas)
        self._acertar_nome_pela_extensao()

    def desfazer_otimizado(self) -> str | None:
        """Volta ao original. Devolve o caminho da cópia (para quem for apagá-la), ou None se não havia."""
        if self.caminho_original is None:
            return None
        copia = self.caminho
        self.caminho, self.tamanho = self.caminho_original, self.tamanho_original
        self.caminho_original, self.tamanho_original, self.perfil_otimizado = None, 0, None
        self.midia, self.midia_erro = None, ""
        self._acertar_nome_pela_extensao()
        return copia

    def _acertar_nome_pela_extensao(self) -> None:
        """A cópia é sempre .mkv: o nome final acompanha a extensão do arquivo que vai subir."""
        if not self.dados or not self.nome_final:
            return
        if self.nome_editado:
            self.nome_final = os.path.splitext(self.nome_final)[0] + self.extensao
        else:
            self.nome_final = self.nome_sugerido()

    # ------------------------------------------------------------------ escolha do filme

    def escolher_filme(self, dados: dict) -> None:
        """O usuário confirmou o filme: guarda os detalhes e propõe o nome padronizado."""
        self.dados = dados
        self.duplicado = None
        self.aviso = ""
        if not self.nome_editado:
            self.nome_final = self.nome_sugerido()
        self.estado = PRONTO
        self.mensagem = ""

    def nome_sugerido(self) -> str:
        if not self.dados:
            return ""
        return nomes.nome_padronizado(
            self.dados.get("titulo") or self.termo_busca, self.dados.get("ano"), self.qualidade, self.extensao
        )

    def definir_nome(self, nome: str) -> None:
        """Edição manual do nome. Em branco ou igual à sugestão volta ao automático."""
        limpo = nomes.sanitizar(os.path.splitext(nome)[0]) + (self.extensao if nome.strip() else "")
        sugerido = self.nome_sugerido()
        self.nome_editado = bool(nome.strip()) and limpo != sugerido
        self.nome_final = limpo if nome.strip() else sugerido

    def definir_qualidade(self, qualidade: str | None) -> None:
        self.qualidade = (qualidade or "").strip() or None
        if self.dados and not self.nome_editado:
            self.nome_final = self.nome_sugerido()

    # ------------------------------------------------------------------ plano e legenda

    def plano(self, maximo: int) -> list[divisao.Parte]:
        return divisao.montar_partes(self.nome_final or self.nome_original, self.tamanho, maximo)

    def resumo_plano(self, maximo: int) -> str:
        plano = self.plano(maximo)
        if len(plano) == 1:
            return f"Arquivo único ({divisao.humano(self.tamanho)}) — não precisa cortar"
        return f"{len(plano)} partes de ~{divisao.humano(plano[0].tamanho)} ({divisao.humano(self.tamanho)} no total)"

    def legenda(self) -> tuple[str, bool]:
        """(texto da legenda, cabe como legenda de mídia?). Vazio se ainda não há filme escolhido."""
        if not self.dados:
            return "", True
        texto = montar_legenda_filme(self.dados, self.audio, self.qualidade)
        return texto, len(texto) <= LIMITE_LEGENDA_MIDIA

    def descricao_da_legenda(self) -> str:
        texto, cabe = self.legenda()
        if not texto:
            return ""
        onde = "na legenda da parte 1" if cabe else "em mensagem de texto separada (passa de 1024)"
        return f"{len(texto)} caracteres — vai {onde}"

    # ------------------------------------------------------------------ pronto para postar?

    def pendencias(self, maximo: int, limite_conta: int) -> list[str]:
        """O que impede de postar agora (lista vazia = pode)."""
        faltas = []
        if self.estado == OTIMIZANDO:
            faltas.append("Aguarde a otimização para streaming terminar")
        if not os.path.isfile(self.caminho):
            faltas.append("O arquivo não existe mais")
        if not self.dados:
            faltas.append("Escolha o filme no TMDB")
        if not (self.nome_final or "").strip():
            faltas.append("O nome do arquivo está vazio")
        if self.duplicado:
            quando = str(self.duplicado.get("publicado_em") or "data desconhecida")[:10]
            faltas.append(
                f"Este filme já foi publicado em {quando}. Apague a postagem antiga pelo /buscar do bot antes de postar de novo"
            )
        if maximo > limite_conta:
            faltas.append(f"A parte ({divisao.humano(maximo)}) passa do limite da conta ({divisao.humano(limite_conta)})")
        return faltas
