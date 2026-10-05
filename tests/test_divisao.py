"""Cortador: plano de partes, leitor de intervalo (sem copiar para disco) e modo 'só cortar'."""
import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

from cortador.compartilhado import formato as partes
from cortador import divisao
from cortador.divisao import ErroDivisao, LeitorDeIntervalo, MIB


def _arquivo(tmp_path, tamanho, nome="Filme.mkv", semente=1):
    caminho = tmp_path / nome
    dados = random.Random(semente).randbytes(tamanho)
    caminho.write_bytes(dados)
    return caminho, dados


# --------------------------------------------------------------------------- tamanhos

def test_parse_tamanho():
    assert divisao.parse_tamanho("1900M") == 1900 * MIB
    assert divisao.parse_tamanho("1900MiB") == 1900 * MIB
    assert divisao.parse_tamanho("1.5g") == int(1.5 * 1024 * MIB)
    assert divisao.parse_tamanho("500k") == 500 * 1024
    assert divisao.parse_tamanho("123") == 123


@pytest.mark.parametrize("texto", ["", "abc", "-5M", "0", "10X"])
def test_parse_tamanho_invalido(texto):
    with pytest.raises(ErroDivisao):
        divisao.parse_tamanho(texto)


def test_limites_por_conta():
    assert divisao.limite_da_conta(False) == 2000 * MIB
    assert divisao.limite_da_conta(True) == 4000 * MIB
    assert divisao.tamanho_padrao(False) == 1900 * MIB
    assert divisao.tamanho_padrao(True) == 3900 * MIB
    # o padrão sempre cabe no limite da própria conta
    assert divisao.tamanho_padrao(False) < divisao.limite_da_conta(False)
    assert divisao.tamanho_padrao(True) < divisao.limite_da_conta(True)


@pytest.mark.parametrize("total,maximo", [(1000, 300), (1001, 300), (999, 500), (10_000, 3), (7, 3)])
def test_planejar_tamanhos_iguais_e_dentro_do_limite(total, maximo):
    tamanhos = divisao.planejar_tamanhos(total, maximo)
    assert sum(tamanhos) == total
    assert all(0 < t <= maximo for t in tamanhos)
    assert max(tamanhos) - min(tamanhos) <= 1
    assert len(tamanhos) == -(-total // maximo)  # o mínimo possível


def test_nao_sobra_parte_minuscula():
    assert divisao.planejar_tamanhos(601, 300) == [201, 200, 200]


def test_cabendo_numa_parte_devolve_uma_so():
    assert divisao.planejar_tamanhos(100, 100) == [100]
    assert divisao.planejar_tamanhos(100, 500) == [100]


def test_planejar_rejeita_vazio_e_maximo_invalido():
    with pytest.raises(ErroDivisao):
        divisao.planejar_tamanhos(0, 100)
    with pytest.raises(ErroDivisao):
        divisao.planejar_tamanhos(100, 0)


# --------------------------------------------------------------------------- plano

def test_montar_partes_nomes_offsets_e_tamanhos():
    plano = divisao.montar_partes("Duna (2021).mkv", 1000, 300)
    assert [p.nome for p in plano] == [
        "Duna (2021).mkv.part01of04", "Duna (2021).mkv.part02of04",
        "Duna (2021).mkv.part03of04", "Duna (2021).mkv.part04of04",
    ]
    assert [p.indice for p in plano] == [1, 2, 3, 4]
    assert all(p.total_partes == 4 for p in plano)
    # contíguas, sem buraco nem sobreposição, cobrindo o arquivo todo
    esperado = 0
    for p in plano:
        assert p.offset == esperado
        esperado += p.tamanho
    assert esperado == 1000


def test_montar_partes_arquivo_unico_mantem_o_nome():
    plano = divisao.montar_partes("Filme.mp4", 100, 500)
    assert len(plano) == 1
    assert plano[0].nome == "Filme.mp4"
    assert (plano[0].indice, plano[0].total_partes, plano[0].offset, plano[0].tamanho) == (1, 1, 0, 100)


def test_nomes_das_partes_sao_reconhecidos_pelo_bot():
    plano = divisao.montar_partes("Filme (2020) [4K].mkv", 25 * 1024 * MIB, divisao.TAMANHO_PADRAO)
    assert len(plano) == 14
    for p in plano:
        nome = partes.interpretar_nome(p.nome)
        assert nome is not None
        assert (nome.base, nome.indice, nome.total) == ("Filme (2020) [4K].mkv", p.indice, 14)


def test_plano_de_filme_de_20gb_cabe_no_limite_gratis():
    plano = divisao.montar_partes("F.mkv", 20 * 1024 * MIB, divisao.TAMANHO_PADRAO)
    assert all(p.tamanho <= divisao.LIMITE_GRATIS for p in plano)
    assert len(plano) == 11


# --------------------------------------------------------------------------- leitor de intervalo

def test_leitor_le_so_o_intervalo(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    with LeitorDeIntervalo(caminho, 300, 200) as leitor:
        assert len(leitor) == 200
        assert leitor.read() == dados[300:500]
        assert leitor.read(10) == b""


def test_leitor_devolve_blocos_exatos(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    with LeitorDeIntervalo(caminho, 100, 650) as leitor:
        blocos = []
        while True:
            bloco = leitor.read(256)
            if not bloco:
                break
            blocos.append(bloco)
    # o upload do Telegram exige blocos cheios, exceto o último
    assert [len(b) for b in blocos] == [256, 256, 138]
    assert b"".join(blocos) == dados[100:750]


def test_leitor_nunca_passa_do_fim_do_intervalo(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    with LeitorDeIntervalo(caminho, 900, 50) as leitor:
        assert leitor.read(1_000) == dados[900:950]


def test_leitor_seek_e_tell_sao_relativos_ao_intervalo(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    with LeitorDeIntervalo(caminho, 100, 200) as leitor:
        assert leitor.seekable() and leitor.readable()
        assert leitor.seek(0, 2) == 200  # o Telegram descobre o tamanho assim
        assert leitor.tell() == 200
        leitor.seek(0)
        assert leitor.read(5) == dados[100:105]
        assert leitor.tell() == 5
        leitor.seek(10, 1)
        assert leitor.read(1) == dados[115:116]
        leitor.seek(-1, 2)
        assert leitor.read() == dados[299:300]
        assert leitor.seek(10_000) == 200  # não passa do fim


def test_leitor_expoe_nome(tmp_path):
    caminho, _ = _arquivo(tmp_path, 10, nome="origem.mkv")
    assert LeitorDeIntervalo(caminho, 0, 10).name == "origem.mkv"
    assert LeitorDeIntervalo(caminho, 0, 10, nome="Filme.mkv.part01of02").name == "Filme.mkv.part01of02"


def test_leitor_detecta_arquivo_que_encolheu(tmp_path):
    caminho, _ = _arquivo(tmp_path, 1000)
    leitor = LeitorDeIntervalo(caminho, 0, 1000)
    caminho.write_bytes(b"x" * 500)
    with pytest.raises(OSError):
        leitor.read()
    leitor.close()


def test_leitor_rejeita_intervalo_invalido(tmp_path):
    caminho, _ = _arquivo(tmp_path, 10)
    with pytest.raises(ValueError):
        LeitorDeIntervalo(caminho, -1, 5)


def test_juntar_todos_os_intervalos_reproduz_o_original(tmp_path):
    caminho, dados = _arquivo(tmp_path, 10_007)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 1_500)
    juntado = b""
    for parte in plano:
        with LeitorDeIntervalo(caminho, parte.offset, parte.tamanho) as leitor:
            juntado += leitor.read()
    assert juntado == dados


def test_sha256_do_intervalo(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    assert divisao.sha256_do_intervalo(caminho, 100, 300) == hashlib.sha256(dados[100:400]).hexdigest()


# --------------------------------------------------------------------------- só cortar

def test_gravar_partes_escreve_partes_e_manifesto(tmp_path):
    caminho, dados = _arquivo(tmp_path, 10_000)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 3_000)
    pasta = tmp_path / "saida"
    avancos = []
    manifesto = divisao.gravar_partes(caminho, plano, pasta, progresso=lambda f, t: avancos.append((f, t)))

    nomes = sorted(p.name for p in pasta.iterdir())
    assert nomes == ["Filme.mkv.manifest.json"] + [f"Filme.mkv.part0{i}of04" for i in range(1, 5)]
    assert b"".join((pasta / p["name"]).read_bytes() for p in manifesto["parts"]) == dados
    assert manifesto["sha256"] == hashlib.sha256(dados).hexdigest()
    assert avancos[-1] == (len(dados), len(dados))
    assert not [n for n in nomes if n.endswith(".tmp")]


def test_gravar_partes_nao_sobrescreve_sem_permissao(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 300)
    pasta = tmp_path / "saida"
    divisao.gravar_partes(caminho, plano, pasta)
    with pytest.raises(ErroDivisao):
        divisao.gravar_partes(caminho, plano, pasta)
    divisao.gravar_partes(caminho, plano, pasta, forcar=True)


def test_gravar_partes_recusa_arquivo_que_cabe_numa_parte(tmp_path):
    caminho, dados = _arquivo(tmp_path, 100)
    with pytest.raises(ErroDivisao):
        divisao.gravar_partes(caminho, divisao.montar_partes("F.mkv", 100, 500), tmp_path / "s")


def test_gravar_partes_recusa_se_o_arquivo_mudou(tmp_path):
    caminho, dados = _arquivo(tmp_path, 1000)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 300)
    caminho.write_bytes(dados + b"extra")
    with pytest.raises(ErroDivisao):
        divisao.gravar_partes(caminho, plano, tmp_path / "s")


def test_gravar_partes_compativel_com_o_cortador_de_linha_de_comando(tmp_path):
    """O manifesto e as partes têm de ser aceitos por `ntv2/tools/ntv2_split.py verify/join`."""
    tools = Path(r"C:\DEV\ntv2\tools")
    if not (tools / "ntv2_split.py").exists():
        pytest.skip("ntv2_split.py não está nesta máquina")
    sys.path.insert(0, str(tools))
    try:
        import ntv2_split
    finally:
        sys.path.remove(str(tools))
    caminho, dados = _arquivo(tmp_path, 10_000)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 3_000)
    pasta = tmp_path / "saida"
    divisao.gravar_partes(caminho, plano, pasta)
    assert ntv2_split.verify(pasta, caminho) == []
    junto = ntv2_split.join_parts(pasta, tmp_path / "junto.mkv")
    assert junto.read_bytes() == dados
    # e o plano em si é o mesmo do cortador de linha de comando
    assert [p.tamanho for p in plano] == ntv2_split.plan_parts(len(dados), 3_000)


def test_gravar_partes_limpa_o_que_gravou_se_falhar(tmp_path, monkeypatch):
    caminho, dados = _arquivo(tmp_path, 1000)
    plano = divisao.montar_partes("Filme.mkv", len(dados), 300)
    pasta = tmp_path / "saida"
    chamadas = {"n": 0}
    original = divisao.os.replace

    def replace_que_falha(origem, destino):
        chamadas["n"] += 1
        if chamadas["n"] == 3:
            raise OSError("disco cheio")
        return original(origem, destino)

    monkeypatch.setattr(divisao.os, "replace", replace_que_falha)
    with pytest.raises(OSError):
        divisao.gravar_partes(caminho, plano, pasta)
    assert list(pasta.iterdir()) == []
