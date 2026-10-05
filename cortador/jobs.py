"""Estado de uma postagem em andamento, salvo em disco para poder RETOMAR depois de uma queda.

Um job é autossuficiente: guarda o plano de corte (offset/tamanho/nome de cada parte), os dados
do TMDB (para a legenda e o registro) e, a cada mensagem enviada, o `message_id` dela. Reabrir o
app e mandar retomar pula o que já está no canal. Gravação atômica (tmp + replace).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from cortador import divisao

NOVO, ENVIANDO, CONCLUIDO, CANCELADO, ERRO = "novo", "enviando", "concluido", "cancelado", "erro"


def pasta_padrao() -> Path:
    from cortador.config import pasta_dados
    return pasta_dados() / "jobs"


@dataclass
class ParteJob:
    indice: int
    total_partes: int
    offset: int
    tamanho: int
    nome: str
    message_id: int | None = None


@dataclass
class Job:
    id: str
    origem: str                 # caminho do arquivo original
    tamanho_origem: int
    mtime_origem: float
    nome_base: str              # nome padronizado do arquivo inteiro (com extensão)
    tmdb_id: int
    dados_tmdb: dict
    audio: str | None
    qualidade: str | None
    canal: str                  # destino em texto (retomar em outro canal seria um erro)
    partes: list[ParteJob]
    texto_message_id: int | None = None
    estado: str = NOVO
    erro: str | None = None
    criado_em: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def total_partes(self) -> int:
        return len(self.partes)

    @property
    def message_ids_enviados(self) -> list[int]:
        ids = [p.message_id for p in self.partes if p.message_id]
        if self.texto_message_id:
            ids.append(self.texto_message_id)
        return ids

    @property
    def tem_envios(self) -> bool:
        return bool(self.message_ids_enviados)

    def ids_em_ordem_logica(self) -> list[int]:
        """[texto?, parte 1, …, parte N] — a ordem que o registro e `partes.campos_registro` esperam
        (não a ordem em que as mensagens foram enviadas)."""
        ids = [self.texto_message_id] if self.texto_message_id else []
        ids += [p.message_id for p in sorted(self.partes, key=lambda p: p.indice)]
        return [i for i in ids if i]

    @classmethod
    def novo(
        cls,
        caminho: str,
        *,
        nome_base: str,
        dados_tmdb: dict,
        audio: str | None,
        qualidade: str | None,
        canal: str,
        maximo: int,
    ) -> "Job":
        info = os.stat(caminho)
        plano = divisao.montar_partes(nome_base, info.st_size, maximo)
        return cls(
            id=uuid.uuid4().hex[:12],
            origem=os.fspath(caminho),
            tamanho_origem=info.st_size,
            mtime_origem=info.st_mtime,
            nome_base=nome_base,
            tmdb_id=int(dados_tmdb["tmdb_id"]),
            dados_tmdb=dados_tmdb,
            audio=audio,
            qualidade=qualidade,
            canal=str(canal),
            partes=[ParteJob(p.indice, p.total_partes, p.offset, p.tamanho, p.nome) for p in plano],
        )

    def arquivo_mudou(self) -> bool:
        """O original foi alterado desde o plano (as partes enviadas não casariam com o resto)."""
        try:
            info = os.stat(self.origem)
        except OSError:
            return True
        return info.st_size != self.tamanho_origem or abs(info.st_mtime - self.mtime_origem) > 1.0


class RepositorioDeJobs:
    def __init__(self, pasta: Path | None = None):
        self.pasta = Path(pasta) if pasta else pasta_padrao()

    def _caminho(self, job_id: str) -> Path:
        return self.pasta / f"{job_id}.json"

    def salvar(self, job: Job) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True)
        destino = self._caminho(job.id)
        tmp = destino.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, destino)

    def carregar(self, job_id: str) -> Job | None:
        caminho = self._caminho(job_id)
        if not caminho.is_file():
            return None
        return self._de_dict(json.loads(caminho.read_text(encoding="utf-8")))

    def pendentes(self) -> list[Job]:
        """Jobs que não terminaram (para oferecer 'Retomar' ao abrir o app), do mais antigo ao mais novo."""
        if not self.pasta.is_dir():
            return []
        jobs = []
        for arquivo in sorted(self.pasta.glob("*.json")):
            try:
                job = self._de_dict(json.loads(arquivo.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                continue  # arquivo corrompido: ignora em vez de travar a abertura do app
            if job.estado in (NOVO, ENVIANDO, ERRO):
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.criado_em)

    def remover(self, job_id: str) -> None:
        self._caminho(job_id).unlink(missing_ok=True)

    @staticmethod
    def _de_dict(dados: dict) -> Job:
        partes = [ParteJob(**p) for p in dados.pop("partes")]
        return Job(partes=partes, **dados)
