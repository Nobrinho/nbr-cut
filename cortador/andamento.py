"""Andamento de um envio: velocidade, há quanto tempo está ativo, quanto falta e se parou.

Sem Tk e com relógio injetável (os testes não esperam de verdade). A tela chama [Andamento.registrar] a cada
evento de progresso e relê o [Andamento.instantaneo] uma vez por segundo, mesmo sem evento nenhum: assim a
velocidade cai para zero e o aviso de "sem progresso" aparece quando o Telegram manda esperar ou a rede cai.

`enviado` é sempre o total ACUMULADO do envio, já contando o que subiu antes (numa retomada o total é o filme
inteiro, não só o que falta); a velocidade, porém, só considera o que subiu nesta sessão.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from cortador import divisao

JANELA_S = 10.0         # velocidade "de agora": média dos últimos segundos
PARADO_S = 15.0         # sem nenhum byte novo por tanto tempo = parado
AVISO_VALE_S = 120.0    # um aviso do Telegram só é mostrado enquanto for recente


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


@dataclass(frozen=True)
class Instantaneo:
    enviado: int
    total: int
    fracao: float
    partes_feitas: int
    partes_total: int
    recente: float | None       # bytes/s nos últimos JANELA_S segundos
    media: float | None         # bytes/s desde o começo desta sessão
    ativo_s: float
    faltam_s: float | None
    termina_as: str | None      # "18:10" (ou "18:10 (amanhã)")
    sem_progresso_s: float
    parado: bool
    aviso: str                  # último aviso do Telegram, se ainda for recente
    rodando: bool


class Andamento:
    def __init__(self, relogio: Callable[[], float] = time.monotonic,
                 hora: Callable[[], datetime] = datetime.now):
        self._relogio = relogio
        self._hora = hora
        self.total = 0
        self.partes_total = 0
        self.partes_feitas = 0
        self._base = 0
        self._enviado = 0
        self._inicio = 0.0
        self._fim: float | None = None
        self._ultimo_avanco = 0.0
        self._amostras: collections.deque[tuple[float, int]] = collections.deque(maxlen=2000)
        self._aviso: tuple[str, float] = ("", 0.0)
        self.iniciado = False

    # ------------------------------------------------------------------ alimentação

    def iniciar(self, total_bytes: int, ja_enviado: int = 0, partes_total: int = 0, partes_feitas: int = 0) -> None:
        agora = self._relogio()
        self.total = max(total_bytes, 0)
        self._base = self._enviado = max(ja_enviado, 0)
        self.partes_total, self.partes_feitas = partes_total, partes_feitas
        self._inicio = self._ultimo_avanco = agora
        self._fim = None
        self._amostras.clear()
        self._amostras.append((agora, self._enviado))
        self._aviso = ("", 0.0)
        self.iniciado = True

    def registrar(self, enviado: int) -> None:
        """[enviado]: total acumulado do envio (incluindo o que já tinha subido antes)."""
        agora = self._relogio()
        if enviado > self._enviado:
            self._ultimo_avanco = agora
        self._enviado = enviado
        self._amostras.append((agora, enviado))

    def parte_concluida(self) -> None:
        self.partes_feitas += 1

    def avisar(self, texto: str) -> None:
        self._aviso = (texto, self._relogio())

    def encerrar(self) -> None:
        if self.iniciado and self._fim is None:
            self._fim = self._relogio()

    @property
    def rodando(self) -> bool:
        return self.iniciado and self._fim is None

    # ------------------------------------------------------------------ leitura

    def _recente(self, agora: float) -> float | None:
        """Média dos últimos JANELA_S s, contada até AGORA (se nada chega, cai para 0 em vez de ficar parada no último valor)."""
        corte = agora - JANELA_S
        base_t, base_b = self._amostras[0]
        for t, b in self._amostras:
            if t <= corte:
                base_t, base_b = t, b
            else:
                break
        span = agora - base_t
        if span < 1.0:
            return None
        return max(self._enviado - base_b, 0) / span

    def instantaneo(self) -> Instantaneo:
        agora = self._fim if self._fim is not None else self._relogio()
        ativo = max(agora - self._inicio, 0.0) if self.iniciado else 0.0
        sessao = max(self._enviado - self._base, 0)
        media = sessao / ativo if ativo >= 1.0 and sessao > 0 else None
        recente = self._recente(agora) if self.iniciado and self.rodando else media
        velocidade = recente if recente and recente >= 1024 else media
        restante = max(self.total - self._enviado, 0)
        faltam = restante / velocidade if velocidade and self.rodando else None
        termina = None
        if faltam is not None:
            quando = self._hora() + timedelta(seconds=faltam)
            termina = quando.strftime("%H:%M") + (" (amanhã)" if quando.date() > self._hora().date() else "")
        sem_progresso = max(agora - self._ultimo_avanco, 0.0) if self.iniciado else 0.0
        texto, quando_aviso = self._aviso
        aviso = texto if texto and agora - quando_aviso <= AVISO_VALE_S else ""
        return Instantaneo(
            enviado=self._enviado, total=self.total, fracao=min(self._enviado / self.total, 1.0) if self.total else 0.0,
            partes_feitas=self.partes_feitas, partes_total=self.partes_total, recente=recente, media=media,
            ativo_s=ativo, faltam_s=faltam, termina_as=termina, sem_progresso_s=sem_progresso,
            parado=self.rodando and sem_progresso >= PARADO_S, aviso=aviso, rodando=self.rodando,
        )

    # ------------------------------------------------------------------ textos

    @staticmethod
    def linha_total(i: Instantaneo) -> str:
        texto = f"Total: {divisao.humano(i.enviado)} de {divisao.humano(i.total)} ({i.fracao:.0%})"
        if i.partes_total > 1:
            texto += f" · {i.partes_feitas} de {i.partes_total} partes"
        return texto

    @staticmethod
    def linha_metricas(i: Instantaneo) -> str:
        """Velocidade, tempo ativo e previsão; com o envio parado, o alerta (e o aviso do Telegram) no lugar."""
        if i.parado:
            texto = f"⏳ sem progresso há {formatar_tempo(i.sem_progresso_s)}"
            if i.aviso:
                texto += f" — {i.aviso}"
            return texto + f" · ativo há {formatar_tempo(i.ativo_s)}"
        partes = []
        if i.recente is not None:
            velocidade = formatar_velocidade(i.recente)
            if i.media is not None:
                velocidade += f" (média {formatar_velocidade(i.media)})"
            partes.append(velocidade)
        else:
            partes.append("calculando a velocidade…")
        partes.append(f"ativo há {formatar_tempo(i.ativo_s)}")
        if i.faltam_s is not None:
            partes.append(f"faltam ~{formatar_tempo(i.faltam_s)}")
            if i.termina_as:
                partes.append(f"termina por volta das {i.termina_as}")
        if i.aviso and i.sem_progresso_s >= 3:
            partes.append(i.aviso)
        return " · ".join(partes)

    def resumo_final(self) -> str:
        i = self.instantaneo()
        media = f" (média {formatar_velocidade(i.media)})" if i.media else ""
        return f"Concluído em {formatar_tempo(i.ativo_s)}{media}"
