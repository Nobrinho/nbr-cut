"""Proteção dos segredos no disco (DPAPI no Windows)."""
import sys

import pytest

from cortador import segredos


def test_ida_e_volta_inclusive_com_acentos():
    for texto in ["abc123", "chave-com-ç-e-ã", "postgresql://u:p%40ss@localhost:5432/db", "x" * 5000]:
        assert segredos.revelar(segredos.proteger(texto)) == texto


def test_o_valor_gravado_nao_contem_o_segredo():
    token = segredos.proteger("meu-hash-secreto")
    assert "meu-hash-secreto" not in token


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI só existe no Windows")
def test_no_windows_usa_a_dpapi():
    token = segredos.proteger("segredo")
    assert token.startswith("dpapi:")
    # protegido de verdade: o mesmo texto protegido duas vezes não dá o mesmo valor (sal da DPAPI)
    assert segredos.proteger("segredo") != token


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI só existe no Windows")
def test_blob_adulterado_nao_e_lido():
    token = segredos.proteger("segredo")
    adulterado = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
    with pytest.raises(segredos.SegredoIlegivel):
        segredos.revelar(adulterado)


def test_formato_desconhecido_e_lixo_dao_erro_claro():
    for valor in ["", "qualquer coisa", "dpapi:!!!nao-e-base64!!!", "texto:@@@"]:
        with pytest.raises(segredos.SegredoIlegivel):
            segredos.revelar(valor)


def test_fora_do_windows_cai_no_esquema_marcado(monkeypatch):
    monkeypatch.setattr(segredos.sys, "platform", "linux")
    token = segredos.proteger("segredo")
    assert token.startswith("texto:")
    assert segredos.revelar(token) == "segredo"


def test_fora_do_windows_nao_le_o_que_a_dpapi_protegeu(monkeypatch):
    monkeypatch.setattr(segredos.sys, "platform", "linux")
    with pytest.raises(segredos.SegredoIlegivel, match="DPAPI"):
        segredos.revelar("dpapi:AAAA")
