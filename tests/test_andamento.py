"""Andamento do envio: velocidade, tempo ativo, previsão, parada e textos (relógio falso, sem esperar)."""
from datetime import datetime

import pytest

from cortador import andamento
from cortador.andamento import Andamento, formatar_tempo, formatar_velocidade

MIB = 1024 * 1024
GIB = 1024 * MIB


class Relogio:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def passar(self, s):
        self.t += s


@pytest.fixture
def rel():
    return Relogio()


def _novo(rel, **kw):
    a = Andamento(relogio=rel, hora=lambda: datetime(2026, 10, 5, 14, 0, 0))
    a.iniciar(**kw)
    return a


def test_formatadores():
    assert formatar_tempo(45) == "45s" and formatar_tempo(125) == "2min05s" and formatar_tempo(3725) == "1h02min"
    assert formatar_tempo(-5) == "0s"
    assert formatar_velocidade(6.5 * MIB) == "6.5 MB/s"


def test_antes_de_qualquer_byte_diz_que_esta_calculando(rel):
    a = _novo(rel, total_bytes=10 * GIB)
    i = a.instantaneo()
    assert i.recente is None and i.media is None and i.faltam_s is None and not i.parado
    assert "calculando a velocidade" in a.linha_metricas(i) and "ativo há 0s" in a.linha_metricas(i)


def test_velocidade_recente_media_e_previsao(rel):
    a = _novo(rel, total_bytes=100 * MIB)
    for _ in range(20):                       # 20 s a 2 MiB/s
        rel.passar(1)
        a.registrar(a._enviado + 2 * MIB)
    i = a.instantaneo()
    assert i.recente == pytest.approx(2 * MIB, rel=0.01) and i.media == pytest.approx(2 * MIB, rel=0.01)
    assert i.ativo_s == 20 and i.fracao == pytest.approx(0.4)
    assert i.faltam_s == pytest.approx(30, rel=0.02)                       # 60 MiB a 2 MiB/s
    assert i.termina_as == "14:00"                                          # hora falsa 14:00:00 + 30 s
    texto = a.linha_metricas(i)
    assert "2.0 MB/s (média 2.0 MB/s)" in texto and "ativo há 20s" in texto and "faltam ~30s" in texto
    assert "termina por volta das 14:00" in texto


def test_previsao_depois_da_meia_noite_diz_amanha(rel):
    a = Andamento(relogio=rel, hora=lambda: datetime(2026, 10, 5, 23, 30, 0))
    a.iniciar(total_bytes=1 * GIB)
    rel.passar(10)
    a.registrar(1 * MIB)                                                   # 0,1 MiB/s → faltam ~2h50
    i = a.instantaneo()
    assert i.faltam_s == pytest.approx((1 * GIB - 1 * MIB) / (0.1 * MIB), rel=0.01)
    assert i.termina_as == "02:20 (amanhã)"


def test_retomada_conta_o_que_ja_subiu_mas_a_velocidade_so_da_sessao(rel):
    a = _novo(rel, total_bytes=12 * GIB, ja_enviado=4 * GIB, partes_total=12, partes_feitas=4)
    rel.passar(10)
    a.registrar(4 * GIB + 20 * MIB)
    i = a.instantaneo()
    assert i.fracao == pytest.approx(4 / 12, abs=0.01) and i.partes_feitas == 4
    assert i.media == pytest.approx(2 * MIB, rel=0.01)                      # 20 MiB em 10 s, não 4 GiB
    assert a.linha_total(i) == f"Total: 4.02 GiB de 12.00 GiB (33%) · 4 de 12 partes"


def test_recente_cai_para_zero_quando_nada_chega(rel):
    a = _novo(rel, total_bytes=1 * GIB)
    for _ in range(10):
        rel.passar(1)
        a.registrar(a._enviado + 2 * MIB)
    rel.passar(30)                                                          # silêncio de 30 s
    i = a.instantaneo()
    assert i.recente == 0 and i.media == pytest.approx(20 * MIB / 40, rel=0.01)
    assert i.faltam_s == pytest.approx((1 * GIB - 20 * MIB) / i.media, rel=0.01)   # a previsão usa a média


def test_parado_depois_de_15_s_e_mostra_o_aviso_do_telegram(rel):
    a = _novo(rel, total_bytes=1 * GIB)
    rel.passar(2)
    a.registrar(5 * MIB)
    rel.passar(14)
    assert not a.instantaneo().parado
    a.avisar("O Telegram pediu para esperar 16 s")
    rel.passar(2)                                                           # 16 s sem byte novo
    i = a.instantaneo()
    assert i.parado and i.sem_progresso_s == pytest.approx(16)
    texto = a.linha_metricas(i)
    assert texto.startswith("⏳ sem progresso há 16s — O Telegram pediu para esperar 16 s")
    assert "ativo há 18s" in texto


def test_voltou_a_andar_limpa_o_estado_de_parado(rel):
    a = _novo(rel, total_bytes=1 * GIB)
    rel.passar(30)
    assert a.instantaneo().parado
    a.registrar(1 * MIB)
    assert not a.instantaneo().parado


def test_aviso_antigo_some(rel):
    a = _novo(rel, total_bytes=1 * GIB)
    a.avisar("rede instável")
    rel.passar(andamento.AVISO_VALE_S + 1)
    assert a.instantaneo().aviso == ""


def test_aviso_recente_aparece_na_linha_enquanto_nao_anda(rel):
    a = _novo(rel, total_bytes=1 * GIB)
    for _ in range(5):
        rel.passar(1)
        a.registrar(a._enviado + MIB)
    a.avisar("Falha de rede (ConnectionError); nova tentativa em 2 s (1/5)")
    rel.passar(4)                                                           # 4 s sem byte: ainda não é "parado"
    i = a.instantaneo()
    assert not i.parado and "Falha de rede" in a.linha_metricas(i)


def test_encerrar_congela_o_tempo_e_gera_o_resumo(rel):
    a = _novo(rel, total_bytes=100 * MIB, partes_total=2)
    rel.passar(50)
    a.registrar(100 * MIB)
    a.encerrar()
    rel.passar(3600)                                                        # o relógio segue, o resumo não
    i = a.instantaneo()
    assert not i.rodando and i.ativo_s == 50 and i.faltam_s is None and not i.parado
    assert a.resumo_final() == "Concluído em 50s (média 2.0 MB/s)"
    a.encerrar()                                                            # idempotente
    assert a.instantaneo().ativo_s == 50


def test_total_zero_e_envio_nao_iniciado_nao_quebram(rel):
    a = Andamento(relogio=rel)
    i = a.instantaneo()
    assert i.fracao == 0 and not i.rodando and i.ativo_s == 0
    b = _novo(rel, total_bytes=0)
    assert b.instantaneo().fracao == 0 and "0%" in b.linha_total(b.instantaneo())


def test_partes_concluidas_sobem_um_a_um(rel):
    a = _novo(rel, total_bytes=12 * GIB, partes_total=12, partes_feitas=4)
    a.parte_concluida()
    a.parte_concluida()
    assert a.instantaneo().partes_feitas == 6
    assert "6 de 12 partes" in a.linha_total(a.instantaneo())
    assert "partes" not in _novo(rel, total_bytes=GIB, partes_total=1).linha_total(
        _novo(rel, total_bytes=GIB, partes_total=1).instantaneo())            # arquivo único: sem "n de n partes"
