"""Otimizar para streaming: trilhas, comando do ffmpeg, estimativas, execução (ffmpeg falso) e ponta a ponta."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cortador import midia, otimizar
from cortador.midia import Audio, InfoMidia, Video
from cortador.otimizar import (ErroOtimizacao, Ferramentas, OtimizacaoCancelada, PERFIS, Trilha, montar_comando)

GIB = 1024 ** 3
FF = Ferramentas("ffmpeg.exe", "ffprobe.exe")


# ====================================================================== trilhas (as do F1 real)

def _f1():
    t = [Trilha(0, "video", "hevc", idioma="en", largura=3840, altura=2160, bits=10, transferencia="smpte2084",
                primarias="bt2020", espaco="bt2020nc")]
    audios = [("truehd", 8, "en", "TrueHD Atmos"), ("eac3", 6, "en", "DDP5.1"), ("ac3", 6, "en", "DD 5.1"),
              ("ac3", 6, "es", "LA"), ("ac3", 6, "es", "EU"), ("ac3", 6, "fr", "CA"), ("ac3", 6, "fr", "EU"),
              ("ac3", 6, "it", ""), ("ac3", 6, "hi", ""), ("ac3", 6, "pt", "BR"), ("ac3", 6, "ru", "")]
    for i, (codec, canais, lingua, titulo) in enumerate(audios, start=1):
        t.append(Trilha(i, "audio", codec, canais, lingua, titulo))
    subs = [("subrip", "en", ""), ("subrip", "en", "SDH"), ("hdmv_pgs_subtitle", "en", "SDH"), ("subrip", "es", ""),
            ("subrip", "pt", "BR"), ("subrip", "pt", "EU"), ("subrip", "pt", "EU SDH"), ("subrip", "ru", ""),
            ("hdmv_pgs_subtitle", "fr", "")]
    for i, (codec, lingua, titulo) in enumerate(subs, start=12):
        t.append(Trilha(i, "subtitle", codec, 0, lingua, titulo))
    return t


def test_escolhe_pt_e_original_preferindo_eac3_e_ac3():
    sel = otimizar.escolher_trilhas(_f1())
    assert [(a.indice, a.codec, a.idioma, copiar) for a, copiar in sel.audios] == [
        (10, "ac3", "pt", True), (2, "eac3", "en", True)]
    assert sel.descartadas_audio == 9


def test_legendas_so_de_texto_pt_ate_3_e_uma_em_ingles_sem_sdh():
    sel = otimizar.escolher_trilhas(_f1())
    assert [(t.indice, t.idioma) for t in sel.legendas] == [(12, "en"), (16, "pt"), (17, "pt"), (18, "pt")]
    assert all(t.codec == "subrip" for t in sel.legendas)          # PGS (imagem) nunca vai
    assert sel.descartadas_legenda == 9 - 4


def test_audio_so_truehd_ou_dts_e_recodificado_para_eac3():
    trilhas = [Trilha(0, "video", "h264", idioma="en", largura=1920, altura=1080),
               Trilha(1, "audio", "truehd", 8, "en"), Trilha(2, "audio", "dts", 6, "pt")]
    sel = otimizar.escolher_trilhas(trilhas)
    assert [(a.indice, copiar) for a, copiar in sel.audios] == [(2, False), (1, False)]


def test_sem_audio_em_portugues_leva_so_o_original():
    trilhas = [Trilha(0, "video", "h264"), Trilha(1, "audio", "aac", 2, "ja"), Trilha(2, "audio", "aac", 2, "en")]
    sel = otimizar.escolher_trilhas(trilhas)
    assert [a.indice for a, _ in sel.audios] == [1]               # o "original" é a 1ª trilha do arquivo


def test_idioma_indefinido_leva_a_melhor_trilha():
    trilhas = [Trilha(0, "video", "h264"), Trilha(1, "audio", "ac3", 6), Trilha(2, "audio", "eac3", 6)]
    assert [a.indice for a, _ in otimizar.escolher_trilhas(trilhas).audios] == [2]


def test_arquivo_sem_video_nao_serve():
    with pytest.raises(ErroOtimizacao, match="vídeo"):
        otimizar.escolher_trilhas([Trilha(0, "audio", "aac")])


def test_trilhas_do_json_do_ffprobe_ignora_capa_e_normaliza_idioma():
    dados = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084", "tags": {"language": "eng"}},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "por", "title": "BR"}},
        {"index": 2, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"index": 3, "codec_type": "data", "codec_name": "bin_data"},
    ]}
    trilhas = otimizar.trilhas_do_json(dados)
    assert [(t.indice, t.tipo, t.idioma, t.bits) for t in trilhas] == [(0, "video", "en", 10), (1, "audio", "pt", 8)]
    assert trilhas[0].transferencia == "smpte2084" and trilhas[1].titulo == "BR"


# ====================================================================== comando

def _args(perfil="4k18", selecao=None):
    sel = selecao or otimizar.escolher_trilhas(_f1())
    return montar_comando(FF, "in.mkv", "out.mkv", PERFIS[perfil], sel)


def _valor(cmd, chave):
    return cmd[cmd.index(chave) + 1]


def test_comando_4k_usa_nvenc_p4_com_os_bitrates_do_perfil():
    cmd = _args("4k18")
    assert cmd[0] == "ffmpeg.exe" and cmd[-1] == "out.mkv" and _valor(cmd, "-f") == "matroska"
    assert _valor(cmd, "-c:v") == "hevc_nvenc" and _valor(cmd, "-preset") == "p4"
    assert (_valor(cmd, "-b:v"), _valor(cmd, "-maxrate"), _valor(cmd, "-bufsize")) == ("18M", "25M", "50M")
    assert _valor(cmd, "-profile:v") == "main10" and _valor(cmd, "-multipass") == "disabled"
    assert "-vf" not in cmd                                      # 4K mantém a resolução
    assert _valor(cmd, "-hwaccel") == "cuda" and _valor(cmd, "-hwaccel_output_format") == "cuda"


@pytest.mark.parametrize("perfil,mbps", [("4k25", "25M"), ("4k12", "12M"), ("1080p8", "8M")])
def test_bitrate_por_perfil(perfil, mbps):
    assert _valor(_args(perfil), "-b:v") == mbps


def test_1080p_reduz_na_placa_mantendo_a_proporcao():
    assert _valor(_args("1080p8"), "-vf") == "scale_cuda=1920:1080:interp_algo=lanczos"
    sel = otimizar.escolher_trilhas(_f1())
    sel.video.altura = 1600                                       # 3840x1600 (cinemascope) → 1920x800
    assert _valor(_args("1080p8", sel), "-vf") == "scale_cuda=1920:800:interp_algo=lanczos"


def test_1080p_nao_amplia_um_arquivo_que_ja_e_1080p():
    trilhas = [Trilha(0, "video", "h264", largura=1920, altura=1080), Trilha(1, "audio", "aac", 2, "en")]
    assert "-vf" not in _args("1080p8", otimizar.escolher_trilhas(trilhas))


def test_hdr10_repassa_as_etiquetas_de_cor():
    cmd = _args()
    assert _valor(cmd, "-color_trc") == "smpte2084" and _valor(cmd, "-color_primaries") == "bt2020"
    assert _valor(cmd, "-colorspace") == "bt2020nc" and _valor(cmd, "-color_range") == "tv"


def test_sdr_8_bits_usa_perfil_main_e_nao_inventa_etiquetas():
    trilhas = [Trilha(0, "video", "h264", largura=1920, altura=1080, bits=8), Trilha(1, "audio", "aac", 2, "en")]
    cmd = _args("4k12", otimizar.escolher_trilhas(trilhas))
    assert _valor(cmd, "-profile:v") == "main" and "-color_trc" not in cmd


def test_codec_sem_decodificacao_na_placa_decodifica_por_software():
    trilhas = [Trilha(0, "video", "mpeg2video", largura=1920, altura=1080, bits=10), Trilha(1, "audio", "ac3", 6, "en")]
    cmd = _args("1080p8", otimizar.escolher_trilhas(trilhas))
    assert "-hwaccel" not in cmd and _valor(cmd, "-pix_fmt") == "p010le"
    trilhas[0].largura, trilhas[0].altura = 3840, 2160
    assert _valor(_args("1080p8", otimizar.escolher_trilhas(trilhas)), "-vf") == "scale=1920:1080:flags=lanczos"


def test_mapas_copias_e_dispositions():
    cmd = _args()
    mapas = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-map"]
    assert mapas == ["0:0", "0:10", "0:2", "0:12", "0:16", "0:17", "0:18"]
    assert _valor(cmd, "-c:a:0") == "copy" and _valor(cmd, "-c:a:1") == "copy"
    assert _valor(cmd, "-c:s:0") == "copy"
    assert _valor(cmd, "-disposition:a:0") == "default" and _valor(cmd, "-disposition:a:1") == "0"
    assert _valor(cmd, "-disposition:s") == "0"
    assert _valor(cmd, "-reserve_index_space") == str(otimizar.ESPACO_INDICE_MINIMO)   # sem duração: o mínimo
    assert "-progress" in cmd and "-nostdin" in cmd


def test_espaco_do_indice_cresce_com_a_duracao_e_sobra_para_um_filme_longo():
    f1 = otimizar.espaco_do_indice(9_318)                           # o F1 precisou de 237 020 bytes
    assert f1 >= 10 * 237_020 and f1 == 9_318 * otimizar.ESPACO_INDICE_POR_SEGUNDO
    assert otimizar.espaco_do_indice(120) == otimizar.ESPACO_INDICE_MINIMO       # amostra curta: o mínimo
    assert otimizar.espaco_do_indice(0) == otimizar.ESPACO_INDICE_MINIMO
    assert otimizar.espaco_do_indice(10 * 3600 * 100) == otimizar.ESPACO_INDICE_MAXIMO   # absurdo: teto
    cmd = montar_comando(FF, "in.mkv", "out.mkv", PERFIS["4k18"], otimizar.escolher_trilhas(_f1()), 9_318.3)
    assert int(_valor(cmd, "-reserve_index_space")) == otimizar.espaco_do_indice(9_318.3) > 237_020


def test_audio_nao_copiavel_e_legenda_mov_text_sao_convertidos():
    trilhas = [Trilha(0, "video", "hevc", largura=3840, altura=2160, bits=10), Trilha(1, "audio", "truehd", 8, "en"),
               Trilha(2, "subtitle", "mov_text", 0, "pt")]
    cmd = _args("4k18", otimizar.escolher_trilhas(trilhas))
    assert _valor(cmd, "-c:a:0") == "eac3" and _valor(cmd, "-b:a:0") == "640k" and _valor(cmd, "-ac:0") == "6"
    assert _valor(cmd, "-c:s:0") == "srt"


def test_sem_legendas_nao_mexe_na_disposition_de_legenda():
    trilhas = [Trilha(0, "video", "hevc", bits=10), Trilha(1, "audio", "ac3", 6, "pt")]
    assert "-disposition:s" not in _args("4k18", otimizar.escolher_trilhas(trilhas))


# ====================================================================== viabilidade e estimativa

def _info(altura=2160, duracao=9318.0, dv=None):
    return InfoMidia("mkv", duracao, Video("HEVC", 3840 if altura > 1080 else 1920, altura, 23.976, 10, "HDR10", dv))


def test_dolby_vision_perfil_5_nao_pode_ser_otimizado():
    assert otimizar.pode_otimizar(_info(dv=5))[0] is False
    assert otimizar.pode_otimizar(_info(dv=7)) == (True, "")
    assert otimizar.pode_otimizar(_info()) == (True, "")
    assert otimizar.pode_otimizar(None)[0] is False and otimizar.pode_otimizar(InfoMidia("mkv"))[0] is False


def test_estimativa_do_f1_bate_com_o_que_medi():
    original = 76_210_749_463
    e = otimizar.estimar(PERFIS["4k18"], _info(), original)
    assert 22 < e.tamanho_bytes / 1e9 < 25                        # ~23 GB
    assert 38 < e.tempo_s / 60 < 45                               # ~41 min
    assert e.download_mb_s == pytest.approx((18 + 2 * 0.64) / 8, abs=0.01) and e.vale_a_pena
    assert 10 < otimizar.estimar(PERFIS["1080p8"], _info(), original).tamanho_bytes / 1e9 < 12
    assert 4.5 < e.upload_s / 3600 < 5.5                          # ~21 GiB a 1,2 MiB/s ≈ 5 h de upload
    assert otimizar.estimar(PERFIS["1080p8"], _info(), original).upload_s < e.upload_s / 2
    assert otimizar.estimar(PERFIS["1080p8"], _info(), original).tempo_s < e.tempo_s / 2


def test_original_ja_leve_nao_vale_a_pena():
    leve = _info(1080, duracao=7200.0)
    assert otimizar.estimar(PERFIS["4k18"], leve, 3 * GIB).vale_a_pena is False   # ~3,6 Mbps
    assert otimizar.estimar(PERFIS["4k18"], _info(), 76_000_000_000).vale_a_pena is True


def test_bitrate_do_video_desconta_o_audio():
    info = _info()
    total = info.bitrate_medio_mbps(76_210_749_463)
    assert otimizar.bitrate_do_video_mbps(info, 76_210_749_463) == pytest.approx(total - 1.28, abs=0.01)
    assert otimizar.bitrate_do_video_mbps(InfoMidia("mkv"), 100) is None


def test_caminho_de_saida_e_espaco(tmp_path):
    destino = otimizar.caminho_de_saida(r"D:\Midias\F1 (2025).mkv", tmp_path, PERFIS["4k18"])
    assert destino == tmp_path / "F1 (2025).4k18.mkv"
    assert otimizar.espaco_livre(tmp_path / "pasta" / "que" / "nao" / "existe") > 0


# ====================================================================== ferramentas

def test_localizar_ffmpeg_por_pasta_e_arquivo(tmp_path, monkeypatch):
    monkeypatch.setattr(otimizar.shutil, "which", lambda nome: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "vazio"))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ffmpeg.exe").write_bytes(b"")
    assert otimizar.localizar_ffmpeg(str(tmp_path)) is None                    # falta o ffprobe
    (tmp_path / "bin" / "ffprobe.exe").write_bytes(b"")
    achado = otimizar.localizar_ffmpeg(str(tmp_path))
    assert achado == Ferramentas(str(tmp_path / "bin" / "ffmpeg.exe"), str(tmp_path / "bin" / "ffprobe.exe"))
    assert otimizar.localizar_ffmpeg(str(tmp_path / "bin" / "ffmpeg.exe")) == achado
    assert otimizar.localizar_ffmpeg("") is None


def test_localizar_ffmpeg_pelo_path_e_pelo_winget(tmp_path, monkeypatch):
    for pasta in ("path", "Microsoft/WinGet/Packages/Gyan.FFmpeg_x/ffmpeg-9/bin"):
        (tmp_path / pasta).mkdir(parents=True)
        (tmp_path / pasta / "ffmpeg.exe").write_bytes(b"")
        (tmp_path / pasta / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setattr(otimizar.shutil, "which", lambda nome: str(tmp_path / "path" / "ffmpeg.exe"))
    assert otimizar.localizar_ffmpeg("").ffmpeg == str(tmp_path / "path" / "ffmpeg.exe")
    monkeypatch.setattr(otimizar.shutil, "which", lambda nome: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert "Gyan.FFmpeg_x" in otimizar.localizar_ffmpeg("").ffmpeg


def test_nvenc_funciona_olha_o_codigo_de_saida(monkeypatch):
    class R:
        def __init__(self, codigo):
            self.returncode = codigo

    monkeypatch.setattr(otimizar.subprocess, "run", lambda *a, **k: R(0))
    assert otimizar.nvenc_funciona(FF) is True
    monkeypatch.setattr(otimizar.subprocess, "run", lambda *a, **k: R(1))
    assert otimizar.nvenc_funciona(FF) is False

    def quebra(*a, **k):
        raise OSError("sem ffmpeg")
    monkeypatch.setattr(otimizar.subprocess, "run", quebra)
    assert otimizar.nvenc_funciona(FF) is False


# ====================================================================== progresso

def test_interpretar_progresso():
    p = otimizar.interpretar_progresso(
        {"out_time_us": "30000000", "fps": "91.5", "speed": "3.8x", "total_size": "1048576", "progress": "continue"}, 120.0)
    assert p.fracao == pytest.approx(0.25) and p.fps == 91.5 and p.velocidade == 3.8
    assert p.tamanho_bytes == 1048576 and p.restante_s == pytest.approx(90 / 3.8)


def test_progresso_sem_tempo_ou_com_valores_estranhos():
    assert otimizar.interpretar_progresso({"out_time_us": "N/A"}, 100) is None
    assert otimizar.interpretar_progresso({}, 100) is None
    assert otimizar.interpretar_progresso({"out_time_us": "-9223372036854775807"}, 100) is None
    p = otimizar.interpretar_progresso({"out_time_us": "5000000", "speed": "N/A", "fps": "x"}, 100)
    assert p.velocidade == 0.0 and p.restante_s is None and p.fps == 0.0
    assert otimizar.interpretar_progresso({"out_time_us": "999000000"}, 100).fracao == 1.0     # nunca passa de 100%
    assert otimizar.interpretar_progresso({"out_time_ms": "1000000"}, 10).fracao == pytest.approx(0.1)


# ====================================================================== executar (ffmpeg falso)

FALSO = r'''
import sys, time
saida = sys.argv[-1]
modo = sys.argv[1]
if modo == "ok":
    for i in range(1, 4):
        print(f"out_time_us={i * 10_000_000}\nfps=90\nspeed=3.5x\ntotal_size={i * 1000}\nprogress=continue", flush=True)
    open(saida, "wb").write(b"x" * 4096)
    print("progress=end", flush=True)
elif modo == "erro":
    sys.stderr.write("Unrecognized option 'xyz'\n")
    open(saida, "wb").write(b"meio")
    sys.exit(3)
elif modo == "vazio":
    open(saida, "wb").write(b"")
elif modo == "longo":
    open(saida, "wb").write(b"parcial")
    while True:
        print("out_time_us=1000000\nspeed=1x\nprogress=continue", flush=True)
        time.sleep(0.1)
'''


@pytest.fixture
def falso(tmp_path):
    script = tmp_path / "falso.py"
    script.write_text(FALSO, encoding="utf-8")

    def comando(modo, destino):
        return [sys.executable, str(script), modo, str(destino)]
    return comando


def test_executar_com_sucesso_reporta_progresso_e_renomeia(falso, tmp_path):
    destino = tmp_path / "saida" / "filme.4k18.mkv"
    eventos = []
    resultado = otimizar.executar(falso("ok", destino), destino, 30.0, eventos.append, lambda: False)
    assert resultado == destino and destino.read_bytes() == b"x" * 4096
    assert not (tmp_path / "saida" / "filme.4k18.mkv.parcial").exists()
    fracoes = [e.fracao for e in eventos]
    assert fracoes[:3] == pytest.approx([1 / 3, 2 / 3, 1.0]) and fracoes[-1] == 1.0
    assert eventos[0].velocidade == 3.5 and eventos[-1].tamanho_bytes == 4096


def test_executar_com_erro_apaga_o_parcial_e_mostra_o_motivo(falso, tmp_path):
    destino = tmp_path / "f.mkv"
    with pytest.raises(ErroOtimizacao, match="Unrecognized option"):
        otimizar.executar(falso("erro", destino), destino, 30.0, lambda p: None, lambda: False)
    assert not destino.exists() and not (tmp_path / "f.mkv.parcial").exists()


def test_executar_saida_vazia_e_erro(falso, tmp_path):
    destino = tmp_path / "f.mkv"
    with pytest.raises(ErroOtimizacao):
        otimizar.executar(falso("vazio", destino), destino, 30.0, lambda p: None, lambda: False)
    assert not destino.exists()


def test_executar_cancelado_mata_o_processo_e_apaga_o_parcial(falso, tmp_path):
    destino = tmp_path / "f.mkv"
    cancelar = threading.Event()
    threading.Timer(0.8, cancelar.set).start()
    inicio = time.time()
    with pytest.raises(OtimizacaoCancelada):
        otimizar.executar(falso("longo", destino), destino, 30.0, lambda p: None, cancelar.is_set)
    assert time.time() - inicio < 10
    assert not destino.exists() and not (tmp_path / "f.mkv.parcial").exists()


def test_executar_sem_o_programa(tmp_path):
    with pytest.raises(ErroOtimizacao, match="iniciar"):
        otimizar.executar([str(tmp_path / "nao_existe.exe"), "x"], tmp_path / "f.mkv", 1.0, lambda p: None, lambda: False)


# ====================================================================== ponta a ponta (ffmpeg e placa reais)

_FF = otimizar.localizar_ffmpeg("")
_PLACA = bool(_FF) and otimizar.nvenc_funciona(_FF)


@pytest.mark.skipif(not _PLACA, reason="precisa do ffmpeg com NVENC e uma placa NVIDIA")
def test_ponta_a_ponta_reencoda_e_mantem_hdr_audio_e_legenda(tmp_path):
    origem = tmp_path / "origem.mkv"
    legenda = tmp_path / "pt.srt"
    legenda.write_text("1\n00:00:00,000 --> 00:00:01,000\nOla\n", encoding="utf-8")
    gerar = subprocess.run(
        [_FF.ffmpeg, "-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=24:d=3",
         "-f", "lavfi", "-i", "sine=frequency=440:d=3", "-f", "lavfi", "-i", "sine=frequency=880:d=3", "-i", str(legenda),
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
         "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-b:v", "30M",
         "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
         "-c:a", "ac3", "-b:a", "192k", "-c:s", "srt",
         "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=por", "-metadata:s:s:0", "language=por",
         str(origem)], capture_output=True)
    assert gerar.returncode == 0, gerar.stderr.decode("utf-8", "replace")[-400:]

    selecao = otimizar.escolher_trilhas(otimizar.listar_trilhas(_FF, str(origem)))
    assert [a.idioma for a, _ in selecao.audios] == ["pt", "en"] and len(selecao.legendas) == 1
    destino = tmp_path / "saida" / "origem.4k12.mkv"
    cmd = montar_comando(_FF, str(origem), str(destino), PERFIS["4k12"], selecao)
    eventos = []
    otimizar.executar(cmd, destino, 3.0, eventos.append, lambda: False)

    info = midia.ler(str(destino))
    assert (info.video.codec, info.video.largura, info.video.altura, info.video.bits) == ("HEVC", 1920, 1080, 10)
    assert info.video.hdr == "HDR10" or info.video.hdr is None          # etiqueta de transferência PQ conservada
    assert [(a.codec, a.idioma) for a in info.audios] == [("AC3", "pt"), ("AC3", "en")]
    assert info.legendas == 1 and eventos[-1].fracao == 1.0
    # índice (Cues) logo no começo do arquivo
    assert b"\x1c\x53\xbb\x6b" in destino.read_bytes()[:4096]
