"""Leitura de metadados (MKV/MP4) e avaliação de compatibilidade, com arquivos sintéticos."""
import os
import struct

import pytest

from cortador import midia
from cortador.midia import ATENCAO, INFO, Audio, InfoMidia, Video, avaliar


# ====================================================================== construtores de MKV (EBML)

def _id(valor: int) -> bytes:
    return valor.to_bytes((valor.bit_length() + 7) // 8, "big")


def _tamanho(n: int | None) -> bytes:
    if n is None:
        return b"\x01\xff\xff\xff\xff\xff\xff\xff"        # desconhecido
    return (0x10000000 | n).to_bytes(4, "big")


def el(id_: int, conteudo: bytes = b"", tamanho: int | None = -1) -> bytes:
    return _id(id_) + _tamanho(len(conteudo) if tamanho == -1 else tamanho) + conteudo


def uint(id_: int, valor: int) -> bytes:
    return el(id_, valor.to_bytes(max(1, (valor.bit_length() + 7) // 8), "big"))


def txt(id_: int, valor: str) -> bytes:
    return el(id_, valor.encode())


def dvcc(perfil: int, nivel: int = 6, camada_extra: bool = True) -> bytes:
    """Payload do registro dvcC (24 bytes; só os 5 primeiros importam)."""
    byte2 = (perfil << 1) | (nivel >> 5)
    byte3 = ((nivel & 31) << 3) | (1 << 2) | (int(camada_extra) << 1) | 1
    return bytes([1, 0, byte2, byte3, 0x20]) + bytes(19)


def hvcc(bits: int = 10) -> bytes:
    registro = bytearray(23)
    registro[0] = 1
    registro[1] = 2 if bits > 8 else 1
    registro[17] = 0xF8 | (bits - 8)
    registro[18] = 0xF8 | (bits - 8)
    return bytes(registro)


def faixa_video(codec="V_MPEGH/ISO/HEVC", largura=3840, altura=2160, privado=None, dv=None, transferencia=None,
                fps=24.0) -> bytes:
    colour = b""
    if transferencia is not None:
        colour = el(0x55B0, uint(0x55BA, transferencia) + uint(0x55BB, 9))
    video = el(0xE0, uint(0xB0, largura) + uint(0xBA, altura) + colour)
    mapa = b""
    if dv is not None:
        mapa = el(0x41E4, uint(0x41E7, 0x64766343) + el(0x41ED, dv))
    privado_el = el(0x63A2, privado) if privado is not None else b""
    return el(0xAE, uint(0xD7, 1) + uint(0x83, 1) + txt(0x86, codec) + uint(0x23E383, int(1e9 / fps)) + privado_el
              + video + mapa)


def faixa_audio(codec="A_AC3", canais=6, idioma="eng", nome=None) -> bytes:
    nome_el = txt(0x536E, nome) if nome else b""
    return el(0xAE, uint(0xD7, 2) + uint(0x83, 2) + txt(0x86, codec) + txt(0x22B59C, idioma) + nome_el
              + el(0xE1, uint(0x9F, canais)))


def faixa_legenda() -> bytes:
    return el(0xAE, uint(0xD7, 9) + uint(0x83, 17) + txt(0x86, "S_TEXT/UTF8"))


def mkv(faixas: list[bytes], duracao_s: float | None = 7200.0, segmento_conhecido=True, com_cluster=True) -> bytes:
    cabecalho = el(0x1A45DFA3, txt(0x4282, "matroska"))
    info = b""
    if duracao_s is not None:
        info = el(0x1549A966, uint(0x2AD7B1, 1_000_000) + el(0x4489, struct.pack(">d", duracao_s * 1000)))
    trilhas = el(0x1654AE6B, b"".join(faixas))
    cluster = el(0x1F43B675, bytes(64)) if com_cluster else b""
    corpo = el(0xEC, bytes(10)) + info + trilhas + cluster
    return cabecalho + el(0x18538067, corpo, None if not segmento_conhecido else -1)


@pytest.fixture
def gravar(tmp_path):
    def _gravar(dados: bytes, nome="filme.mkv") -> str:
        caminho = tmp_path / nome
        caminho.write_bytes(dados)
        return str(caminho)
    return _gravar


# ====================================================================== MKV

def test_mkv_dolby_vision_perfil_7(gravar):
    dados = mkv([
        faixa_video(privado=hvcc(10), dv=dvcc(7, 6, True)),
        faixa_audio("A_TRUEHD", 8, "eng", "TrueHD Atmos"),
        faixa_audio("A_AC3", 6, "por"),
        faixa_legenda(), faixa_legenda(),
    ])
    info = midia.ler(gravar(dados))
    v = info.video
    assert (v.codec, v.largura, v.altura, v.bits) == ("HEVC", 3840, 2160, 10)
    assert (v.dv_perfil, v.dv_nivel, v.dv_camada_extra) == (7, 6, True)
    assert v.fps == pytest.approx(24.0, abs=0.01)
    assert info.duracao_s == pytest.approx(7200.0)
    assert [(a.codec, a.canais, a.idioma) for a in info.audios] == [("TrueHD", 8, "en"), ("AC3", 6, "pt")]
    assert info.audios[0].titulo == "TrueHD Atmos"
    assert info.legendas == 2
    assert info.resolucao == 2160 and info.qualidade_sugerida == "2160p, HDR"


def test_mkv_hdr10_sem_dolby_vision(gravar):
    info = midia.ler(gravar(mkv([faixa_video(privado=hvcc(10), transferencia=16), faixa_audio()])))
    assert info.video.hdr == "HDR10" and info.video.dv_perfil is None
    assert info.qualidade_sugerida == "2160p, HDR"


def test_mkv_hlg_e_sdr(gravar):
    assert midia.ler(gravar(mkv([faixa_video(transferencia=18), faixa_audio()]))).video.hdr == "HLG"
    sdr = midia.ler(gravar(mkv([faixa_video("V_MPEG4/ISO/AVC", 1920, 1080), faixa_audio()])))
    assert sdr.video.hdr is None and sdr.qualidade_sugerida == "1080p"


def test_mkv_resolucao_pela_largura_em_tela_larga(gravar):
    info = midia.ler(gravar(mkv([faixa_video(largura=3840, altura=1600), faixa_audio()])))
    assert info.resolucao == 2160  # 3840x1600 é "4K" em cinemascope
    assert midia.ler(gravar(mkv([faixa_video(largura=1920, altura=800), faixa_audio()]))).resolucao == 1080
    assert midia.ler(gravar(mkv([faixa_video(largura=1280, altura=720), faixa_audio()]))).resolucao == 720


def test_mkv_segmento_de_tamanho_desconhecido(gravar):
    info = midia.ler(gravar(mkv([faixa_video(), faixa_audio()], segmento_conhecido=False)))
    assert info.video.codec == "HEVC" and len(info.audios) == 1


def test_mkv_sem_cluster_e_sem_duracao(gravar):
    info = midia.ler(gravar(mkv([faixa_video(), faixa_audio()], duracao_s=None, com_cluster=False)))
    assert info.duracao_s is None and info.video is not None


def test_mkv_codecs_de_video(gravar):
    for codec_id, esperado in (("V_AV1", "AV1"), ("V_VP9", "VP9"), ("V_MPEG2", "MPEG-2"), ("V_MPEG4/ISO/ASP", "MPEG-4")):
        assert midia.ler(gravar(mkv([faixa_video(codec_id), faixa_audio()]))).video.codec == esperado


def test_mkv_avc_10_bits(gravar):
    avcc = bytes([1, 110, 0, 40, 0xFF])
    info = midia.ler(gravar(mkv([faixa_video("V_MPEG4/ISO/AVC", 1920, 1080, privado=avcc), faixa_audio()])))
    assert info.video.codec == "H.264" and info.video.bits == 10


def test_mkv_so_o_primeiro_video_conta(gravar):
    info = midia.ler(gravar(mkv([faixa_video(largura=3840), faixa_video("V_AV1", 1280, 720), faixa_audio()])))
    assert info.video.codec == "HEVC" and info.video.largura == 3840


def test_mkv_idioma_bcp47_vale_mais_que_o_iso_e_vira_duas_letras(gravar):
    faixa = el(0xAE, uint(0xD7, 2) + uint(0x83, 2) + txt(0x86, "A_AAC") + txt(0x22B59C, "eng") + txt(0x22B59D, "pt-BR")
               + el(0xE1, uint(0x9F, 2)))
    info = midia.ler(gravar(mkv([faixa_video(), faixa])))
    assert info.audios[0].idioma == "pt" and info.audios[0].codec == "AAC"


# ====================================================================== MP4

def caixa(tipo: bytes, conteudo: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(conteudo), tipo) + conteudo


def entrada_visual(tipo: bytes, filhos: bytes, largura=3840, altura=2160) -> bytes:
    corpo = bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", largura, altura) + bytes(50)
    assert len(corpo) == 78
    return caixa(tipo, corpo + filhos)


def entrada_audio(tipo: bytes, canais=6) -> bytes:
    corpo = bytes(6) + struct.pack(">H", 1) + bytes(8) + struct.pack(">HH", canais, 16) + bytes(8)
    assert len(corpo) == 28
    return caixa(tipo, corpo)


def trak(manipulador: bytes, entrada: bytes, idioma: str = "eng") -> bytes:
    codigo = sum((ord(c) - 0x60) << s for c, s in zip(idioma, (10, 5, 0)))
    mdhd = caixa(b"mdhd", bytes(20) + struct.pack(">HH", codigo, 0))
    hdlr = caixa(b"hdlr", bytes(8) + manipulador + bytes(12))
    stsd = caixa(b"stsd", bytes(4) + struct.pack(">I", 1) + entrada)
    return caixa(b"trak", caixa(b"mdia", mdhd + hdlr + caixa(b"minf", caixa(b"stbl", stsd))))


def mp4(trilhas: list[bytes], duracao_s=3600, moov_no_fim=True) -> bytes:
    ftyp = caixa(b"ftyp", b"isom" + bytes(4) + b"isom")
    mvhd = caixa(b"mvhd", bytes(12) + struct.pack(">II", 1000, duracao_s * 1000) + bytes(80))
    moov = caixa(b"moov", mvhd + b"".join(trilhas))
    mdat = caixa(b"mdat", bytes(2048))
    return ftyp + (mdat + moov if moov_no_fim else moov + mdat)


def test_mp4_hevc_com_dolby_vision_e_hdr(gravar):
    colr = caixa(b"colr", b"nclx" + struct.pack(">HHH", 9, 16, 9) + b"\x00")
    filhos = caixa(b"hvcC", hvcc(10)) + caixa(b"dvvC", dvcc(8, 6, False)) + colr
    dados = mp4([
        trak(b"vide", entrada_visual(b"hvc1", filhos)),
        trak(b"soun", entrada_audio(b"ec-3", 6), "por"),
        trak(b"sbtl", caixa(b"tx3g", bytes(30))),
    ])
    info = midia.ler(gravar(dados, "filme.mp4"))
    v = info.video
    assert (v.codec, v.largura, v.altura, v.bits, v.hdr) == ("HEVC", 3840, 2160, 10, "HDR10")
    assert (v.dv_perfil, v.dv_camada_extra) == (8, False)
    assert info.duracao_s == pytest.approx(3600)
    assert [(a.codec, a.canais, a.idioma) for a in info.audios] == [("EAC3", 6, "pt")]
    assert info.legendas == 1


def test_mp4_moov_no_comeco_e_no_fim(gravar):
    faixas = [trak(b"vide", entrada_visual(b"avc1", caixa(b"avcC", bytes([1, 100, 0, 40, 0xFF])), 1920, 1080)),
              trak(b"soun", entrada_audio(b"mp4a", 2))]
    for no_fim in (True, False):
        info = midia.ler(gravar(mp4(faixas, moov_no_fim=no_fim), f"f{no_fim}.mp4"))
        assert info.video.codec == "H.264" and info.video.bits == 8 and info.audios[0].codec == "AAC"


def test_mp4_entrada_dolby_vision_dvhe(gravar):
    dados = mp4([trak(b"vide", entrada_visual(b"dvhe", caixa(b"hvcC", hvcc(10)) + caixa(b"dvcC", dvcc(5, 9, False))))])
    assert midia.ler(gravar(dados, "dv.mp4")).video.dv_perfil == 5


# ====================================================================== arquivos ruins

def test_arquivo_que_nao_e_video(gravar):
    with pytest.raises(midia.MidiaIlegivel, match="formato"):
        midia.ler(gravar(b"nada de video aqui" * 10, "x.mkv"))


def test_arquivo_vazio_ou_inexistente(gravar, tmp_path):
    with pytest.raises(midia.MidiaIlegivel):
        midia.ler(gravar(b"", "vazio.mkv"))
    with pytest.raises(midia.MidiaIlegivel, match="abrir"):
        midia.ler(str(tmp_path / "nao_existe.mkv"))


def test_mkv_truncado_no_meio_das_trilhas(gravar):
    completo = mkv([faixa_video(), faixa_audio()])
    cortado = completo[: completo.index(_id(0x1654AE6B)) + 20]
    with pytest.raises(midia.MidiaIlegivel):
        midia.ler(gravar(cortado))


def test_mkv_sem_trilhas(gravar):
    with pytest.raises(midia.MidiaIlegivel, match="trilhas"):
        midia.ler(gravar(el(0x1A45DFA3, txt(0x4282, "matroska")) + el(0x18538067, el(0xEC, bytes(10)))))


def test_mp4_sem_moov(gravar):
    with pytest.raises(midia.MidiaIlegivel, match="moov"):
        midia.ler(gravar(caixa(b"ftyp", b"isom" + bytes(8)) + caixa(b"mdat", bytes(100)), "sem.mp4"))


# ====================================================================== avaliação

def _info(video: Video | None, audios=None, duracao=7200.0) -> InfoMidia:
    return InfoMidia(container="mkv", duracao_s=duracao, video=video,
                     audios=[Audio("AC3", 6, "pt")] if audios is None else audios)


def _textos(avisos):
    return [a.texto for a in avisos]


def test_video_comum_nao_tem_aviso():
    assert avaliar(_info(Video("H.264", 1920, 1080, bits=8)), 4_000_000_000) == []
    assert avaliar(_info(Video("HEVC", 1920, 1080, bits=10, hdr="HDR10")), 6_000_000_000) == []


def test_dolby_vision_perfil_7_pede_atencao():
    avisos = avaliar(_info(Video("HEVC", 3840, 2160, bits=10, dv_perfil=7, dv_camada_extra=True)))
    assert [a.nivel for a in avisos] == [ATENCAO]
    assert "perfil 7" in avisos[0].texto and "camada" in avisos[0].texto


def test_dolby_vision_perfil_5_avisa_das_cores():
    avisos = avaliar(_info(Video("HEVC", 3840, 2160, bits=10, dv_perfil=5)))
    assert avisos[0].nivel == ATENCAO and "cores" in avisos[0].texto


def test_dolby_vision_perfil_8_e_so_informativo():
    avisos = avaliar(_info(Video("HEVC", 3840, 2160, bits=10, dv_perfil=8)))
    assert [a.nivel for a in avisos] == [INFO]


def test_codecs_problematicos():
    assert any("AV1" in t for t in _textos(avaliar(_info(Video("AV1", 3840, 2160)))))
    assert any("VC-1" in t for t in _textos(avaliar(_info(Video("VC-1", 1920, 1080)))))
    assert any("10 bits" in t for t in _textos(avaliar(_info(Video("H.264", 1920, 1080, bits=10)))))


def test_bitrate_alto_so_com_tamanho_e_duracao():
    video = Video("HEVC", 3840, 2160, bits=10)
    gb76 = 76_210_749_463
    assert any("65 Mbps" in t for t in _textos(avaliar(_info(video, duracao=9318), gb76)))
    assert not any("Mbps" in t for t in _textos(avaliar(_info(video, duracao=9318), 10_000_000_000)))
    assert not any("Mbps" in t for t in _textos(avaliar(_info(video, duracao=None), gb76)))
    assert not any("Mbps" in t for t in _textos(avaliar(_info(video), 0)))


def test_sem_video_e_sem_audio():
    assert avaliar(_info(None))[0].nivel == ATENCAO
    avisos = avaliar(_info(Video("H.264", 1920, 1080, bits=8), audios=[]))
    assert any("áudio" in t for t in _textos(avisos))


def test_audio_sem_portugues_e_decodificacao_por_software():
    video = Video("H.264", 1920, 1080, bits=8)
    avisos = avaliar(_info(video, audios=[Audio("DTS", 6, "en")]))
    assert [a.nivel for a in avisos] == [INFO, INFO]
    assert any("português" in t for t in _textos(avisos)) and any("software" in t for t in _textos(avisos))
    # idioma desconhecido não vira aviso de "sem português"
    assert not any("português" in t for t in _textos(avaliar(_info(video, audios=[Audio("AC3", 6, "und")]))))
    assert avaliar(_info(video, audios=[Audio("AC3", 6, "pt-br")])) == []


# ====================================================================== textos

def test_resumos():
    info = InfoMidia("mkv", 9318.34, Video("HEVC", 3840, 2160, 23.976, 10, None, 7, 6, True),
                     [Audio("TrueHD", 8, "en"), Audio("AC3", 6, "pt"), Audio("AC3", 2, "und")], legendas=3)
    assert info.resumo_video() == "2160p (3840x2160) · HEVC 10-bit · Dolby Vision P7 · 23,976 fps · 2h35min"
    assert info.resumo_audio() == "3 áudio(s): TrueHD 7.1 (en), AC3 5.1 (pt), AC3 2.0 · 3 legenda(s)"
    assert InfoMidia("mkv").resumo_video() == "Sem trilha de vídeo"
    assert InfoMidia("mkv").resumo_audio() == "Sem trilha de áudio"


def test_resumo_de_muitos_audios_e_duracao_curta():
    info = InfoMidia("mp4", 90, Video("H.264", 1280, 720, bits=8), [Audio("AAC", 2, "en")] * 8)
    assert info.resumo_video().endswith("2min")  # 90 s arredonda para 2 min
    assert "+2" in info.resumo_audio() and info.resumo_audio().startswith("8 áudio(s)")


# ====================================================================== arquivo real (opcional)

F1 = os.getenv("NBR_TESTE_MKV", r"D:\Midias\filmes\F1.The.Movie.2025.2160p.UHD.BluRay.Remux.DV.P7.HDR.MULTi[Ben The Men].mkv")


@pytest.mark.skipif(not os.path.isfile(F1), reason="arquivo de teste real indisponível")
def test_arquivo_real_dolby_vision_p7():
    info = midia.ler(F1)
    assert (info.video.codec, info.video.dv_perfil, info.video.dv_camada_extra) == ("HEVC", 7, True)
    assert (info.video.largura, info.video.altura, info.video.bits) == (3840, 2160, 10)
    assert info.duracao_s == pytest.approx(9318.3, abs=1)
    assert len(info.audios) >= 5 and any(a.codec == "TrueHD" for a in info.audios)
    assert any("perfil 7" in a.texto for a in avaliar(info, os.path.getsize(F1)))
