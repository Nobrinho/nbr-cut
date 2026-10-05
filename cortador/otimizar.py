"""Otimizar para streaming: reencoda o vídeo num bitrate que o download do app acompanha (ffmpeg + NVENC).

Um remux de UHD Blu-ray tem ~55 Mbps de vídeo; o Telegram entrega ~5 MB/s (40 Mbps) a uma conta comum, e o
filme trava em buffer. Aqui geramos uma CÓPIA (o original nunca é alterado) em HEVC 10-bit com o bitrate do
perfil escolhido, mantendo HDR10 (cores e metadados de mastering), uma ou duas trilhas de áudio sem recodificar
(português + original), legendas de texto e o índice do MKV no começo do arquivo.

Medido numa RTX 3080 com o preset `p4` (F1, 4K, 62 Mbps de origem): 4K a 18 Mbps = VMAF 99,4 a ~3,8× o tempo
real; o `p7` é 3,5× mais lento sem ganho. O Dolby Vision é descartado (o app toca a camada base HDR10).

Este módulo não conhece Tk: a interface chama [localizar_ffmpeg], [listar_trilhas], [escolher_trilhas],
[montar_comando] e [executar] (que roda numa thread e reporta o progresso por callback).
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cortador import midia
from cortador.midia import InfoMidia

# Velocidade (× tempo real) medida na RTX 3080 com o preset p4; só serve de estimativa na tela.
VELOCIDADE_4K = 3.8
VELOCIDADE_1080P = 9.0
AUDIO_MBPS_ESTIMADO = 0.64          # cada trilha AC3/EAC3 5.1 copiada
SOBRA_DO_CONTAINER = 1.01
ESPACO_RESERVADO_INDICE = 200_000   # bytes no começo do MKV para o índice (Cues)
CODECS_COM_DECODIFICACAO_POR_PLACA = {"hevc", "h264", "vp9", "av1"}
CODECS_DE_AUDIO_COPIAVEIS = {"eac3", "ac3", "aac", "opus", "mp3"}
CODECS_DE_LEGENDA_TEXTO = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}


class ErroOtimizacao(Exception):
    """O ffmpeg falhou ou não dá para otimizar este arquivo (a mensagem é para o usuário)."""


class OtimizacaoCancelada(Exception):
    """O usuário cancelou; o arquivo parcial já foi apagado."""


@dataclass(frozen=True)
class Perfil:
    id: str
    nome: str
    video_mbps: int
    altura: int | None = None       # 1080 = reduz para Full HD; None = mantém a resolução

    @property
    def descricao(self) -> str:
        return self.nome


PERFIS: dict[str, Perfil] = {p.id: p for p in (
    Perfil("4k18", "4K · 18 Mbps (recomendado)", 18),
    Perfil("4k25", "4K · 25 Mbps (qualidade máxima)", 25),
    Perfil("4k12", "4K · 12 Mbps (econômico)", 12),
    Perfil("1080p8", "1080p · 8 Mbps (leve)", 8, 1080),
)}
PERFIL_PADRAO = "4k18"


# ============================================================================ ferramentas

@dataclass(frozen=True)
class Ferramentas:
    ffmpeg: str
    ffprobe: str


def localizar_ffmpeg(configurado: str = "") -> Ferramentas | None:
    """Acha ffmpeg + ffprobe: o caminho configurado (arquivo ou pasta), o PATH e a instalação do winget."""
    candidatos: list[Path] = []
    if configurado.strip():
        base = Path(configurado.strip().strip('"'))
        candidatos += [base, base / "ffmpeg.exe", base / "bin" / "ffmpeg.exe"]
    achado = shutil.which("ffmpeg")
    if achado:
        candidatos.append(Path(achado))
    local = os.getenv("LOCALAPPDATA")
    if local:
        padrao = os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe")
        candidatos += [Path(p) for p in sorted(glob.glob(padrao, recursive=True))]
    for candidato in candidatos:
        if candidato.is_file():
            sonda = candidato.with_name("ffprobe" + candidato.suffix)
            if sonda.is_file():
                return Ferramentas(str(candidato), str(sonda))
    return None


def nvenc_funciona(ff: Ferramentas, timeout: float = 30.0) -> bool:
    """Codifica meio segundo de nada com hevc_nvenc: confirma a placa NVIDIA e o driver, não só a build."""
    try:
        resultado = subprocess.run(
            [ff.ffmpeg, "-hide_banner", "-nostdin", "-f", "lavfi", "-i", "color=c=black:s=256x144:r=24:d=0.5",
             "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=timeout, creationflags=_SEM_JANELA,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return resultado.returncode == 0


_SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ============================================================================ trilhas

@dataclass
class Trilha:
    indice: int                     # índice do stream no arquivo (0:N)
    tipo: str                       # video | audio | subtitle
    codec: str
    canais: int = 0
    idioma: str = "und"             # duas letras (pt, en…) ou "und"
    titulo: str = ""
    largura: int = 0
    altura: int = 0
    bits: int = 8
    transferencia: str = ""
    primarias: str = ""
    espaco: str = ""


def _idioma(bruto: str | None) -> str:
    bruto = (bruto or "und").strip().lower()
    return midia._IDIOMAS.get(bruto, bruto[:2] if bruto else "und")


def listar_trilhas(ff: Ferramentas, caminho: str) -> list[Trilha]:
    """Trilhas do arquivo lidas pelo ffprobe (ignora capas/miniaturas anexadas)."""
    try:
        saida = subprocess.run(
            [ff.ffprobe, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,channels,width,height,pix_fmt,color_transfer,color_primaries,"
             "color_space,bits_per_raw_sample:stream_tags=language,title:stream_disposition=attached_pic",
             "-of", "json", caminho],
            capture_output=True, timeout=60, creationflags=_SEM_JANELA,
        )
    except (OSError, subprocess.SubprocessError) as erro:
        raise ErroOtimizacao(f"Não consegui rodar o ffprobe: {erro}") from erro
    if saida.returncode != 0:
        raise ErroOtimizacao("O ffprobe não conseguiu ler o arquivo: " + saida.stderr.decode("utf-8", "replace")[-300:])
    return trilhas_do_json(json.loads(saida.stdout.decode("utf-8", "replace") or "{}"))


def trilhas_do_json(dados: dict) -> list[Trilha]:
    trilhas = []
    for s in dados.get("streams", []):
        if (s.get("disposition") or {}).get("attached_pic"):
            continue
        tipo = s.get("codec_type")
        if tipo not in ("video", "audio", "subtitle"):
            continue
        marcas = s.get("tags") or {}
        pix = str(s.get("pix_fmt") or "")
        bits = int(s.get("bits_per_raw_sample") or 0) or (10 if "10" in pix or "12" in pix else 8)
        trilhas.append(Trilha(
            indice=int(s["index"]), tipo=tipo, codec=str(s.get("codec_name") or ""),
            canais=int(s.get("channels") or 0), idioma=_idioma(marcas.get("language")),
            titulo=str(marcas.get("title") or ""), largura=int(s.get("width") or 0), altura=int(s.get("height") or 0),
            bits=bits, transferencia=str(s.get("color_transfer") or ""), primarias=str(s.get("color_primaries") or ""),
            espaco=str(s.get("color_space") or ""),
        ))
    return trilhas


@dataclass
class Selecao:
    video: Trilha
    audios: list[tuple[Trilha, bool]] = field(default_factory=list)   # (trilha, copiar sem recodificar?)
    legendas: list[Trilha] = field(default_factory=list)
    descartadas_audio: int = 0
    descartadas_legenda: int = 0

    def resumo(self) -> str:
        audios = ", ".join(f"{t.idioma} {t.codec.upper()}{' 5.1' if t.canais == 6 else ''}"
                           f"{'' if copiar else ' (→EAC3)'}" for t, copiar in self.audios) or "nenhum"
        legendas = ", ".join(sorted({t.idioma for t in self.legendas})) or "nenhuma"
        return (f"Áudio: {audios} · Legendas: {legendas} ({len(self.legendas)}) — "
                f"descarta {self.descartadas_audio} áudio(s) e {self.descartadas_legenda} legenda(s)")


_RANK_AUDIO = {"eac3": 0, "ac3": 1, "aac": 2, "opus": 3}


def _melhor_audio(candidatas: list[Trilha]) -> Trilha:
    """Prefere EAC3/AC3 (copiáveis e leves); entre iguais, mais canais e a primeira do arquivo."""
    return sorted(candidatas, key=lambda t: (_RANK_AUDIO.get(t.codec, 9), -t.canais, t.indice))[0]


def _eh_sdh(t: Trilha) -> bool:
    texto = t.titulo.lower()
    return any(marca in texto for marca in ("sdh", " cc", "hearing", "forced"))


def escolher_trilhas(trilhas: list[Trilha]) -> Selecao:
    """O que levar para a cópia: o vídeo, o áudio em português + o original, legendas de texto pt/en."""
    videos = [t for t in trilhas if t.tipo == "video"]
    if not videos:
        raise ErroOtimizacao("O arquivo não tem trilha de vídeo.")
    audios = [t for t in trilhas if t.tipo == "audio"]
    legendas = [t for t in trilhas if t.tipo == "subtitle"]

    escolhidos: list[Trilha] = []
    original = next((a.idioma for a in audios if a.idioma != "und"), audios[0].idioma if audios else "und")
    for lingua in ("pt", original):
        daquela = [a for a in audios if a.idioma == lingua and a not in escolhidos]
        if daquela:
            escolhidos.append(_melhor_audio(daquela))
    if audios and not escolhidos:
        escolhidos.append(_melhor_audio(audios))
    selecao = Selecao(video=videos[0])
    for trilha in escolhidos:
        selecao.audios.append((trilha, trilha.codec in CODECS_DE_AUDIO_COPIAVEIS))
    selecao.descartadas_audio = len(audios) - len(escolhidos)

    de_texto = [t for t in legendas if t.codec in CODECS_DE_LEGENDA_TEXTO]
    levadas: list[Trilha] = sorted((t for t in de_texto if t.idioma == "pt"), key=lambda t: (_eh_sdh(t), t.indice))[:3]
    for lingua in dict.fromkeys(("en", original)):
        if lingua in ("pt", "und"):
            continue
        daquela = sorted((t for t in de_texto if t.idioma == lingua), key=lambda t: (_eh_sdh(t), t.indice))
        if daquela:
            levadas.append(daquela[0])
    selecao.legendas = sorted(levadas, key=lambda t: t.indice)
    selecao.descartadas_legenda = len(legendas) - len(selecao.legendas)
    return selecao


# ============================================================================ viabilidade e estimativa

def pode_otimizar(info: InfoMidia | None) -> tuple[bool, str]:
    """(pode, motivo se não). Dolby Vision perfil 5 não tem camada base HDR10: reencodar estragaria as cores."""
    if info is None or info.video is None:
        return False, "Ainda não li os metadados do arquivo."
    if info.video.dv_perfil == 5:
        return False, "Dolby Vision perfil 5 não tem camada base compatível: reencodar distorceria as cores."
    return True, ""


def bitrate_do_video_mbps(info: InfoMidia, tamanho_bytes: int, n_audios: int = 2) -> float | None:
    """Bitrate médio do arquivo menos o que o áudio costuma ocupar (estimativa)."""
    total = info.bitrate_medio_mbps(tamanho_bytes)
    return max(total - n_audios * AUDIO_MBPS_ESTIMADO, 0.1) if total else None


@dataclass(frozen=True)
class Estimativa:
    tamanho_bytes: int
    tempo_s: float
    mbps_total: float
    download_mb_s: float
    vale_a_pena: bool               # o original é bem maior que o resultado?


def estimar(perfil: Perfil, info: InfoMidia, tamanho_original: int, n_audios: int = 2) -> Estimativa:
    duracao = info.duracao_s or 0.0
    mbps = perfil.video_mbps + n_audios * AUDIO_MBPS_ESTIMADO
    tamanho = int(mbps * 1_000_000 / 8 * duracao * SOBRA_DO_CONTAINER)
    reduz_para_1080 = perfil.altura is not None and info.video is not None and info.video.altura > perfil.altura
    velocidade = VELOCIDADE_1080P if reduz_para_1080 or (info.video and info.video.altura <= 1080) else VELOCIDADE_4K
    original = info.bitrate_medio_mbps(tamanho_original) or 0.0
    return Estimativa(
        tamanho_bytes=tamanho, tempo_s=duracao / velocidade if duracao else 0.0, mbps_total=mbps,
        download_mb_s=mbps / 8, vale_a_pena=original >= mbps * 1.25,
    )


def espaco_livre(pasta: str | os.PathLike) -> int:
    alvo = Path(pasta)
    while not alvo.exists() and alvo != alvo.parent:
        alvo = alvo.parent
    return shutil.disk_usage(alvo).free


def caminho_de_saida(origem: str, pasta: str | os.PathLike, perfil: Perfil) -> Path:
    base = Path(origem).stem
    return Path(pasta) / f"{base}.{perfil.id}.mkv"


# ============================================================================ comando

def _dimensoes_1080p(v: Trilha) -> tuple[int, int]:
    largura = 1920
    altura = round(largura * v.altura / v.largura / 2) * 2 if v.largura else 1080
    return largura, altura


def montar_comando(ff: Ferramentas, origem: str, destino: str, perfil: Perfil, selecao: Selecao) -> list[str]:
    """Linha de comando do ffmpeg para [perfil]. O vídeo vai por NVENC (preset p4, VBR com teto de 1,4×)."""
    v = selecao.video
    placa = v.codec in CODECS_COM_DECODIFICACAO_POR_PLACA
    reduzir = perfil.altura is not None and (v.altura > perfil.altura or v.largura > 1920)
    cmd = [ff.ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    if placa:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-i", origem, "-map", f"0:{v.indice}"]
    for trilha, _ in selecao.audios:
        cmd += ["-map", f"0:{trilha.indice}"]
    for trilha in selecao.legendas:
        cmd += ["-map", f"0:{trilha.indice}"]
    cmd += ["-map_metadata", "0"]

    filtros = []
    if reduzir:
        largura, altura = _dimensoes_1080p(v)
        filtros.append(f"scale_cuda={largura}:{altura}:interp_algo=lanczos" if placa
                       else f"scale={largura}:{altura}:flags=lanczos")
    if filtros:
        cmd += ["-vf", ",".join(filtros)]
    if not placa and v.bits > 8:
        cmd += ["-pix_fmt", "p010le"]

    mbps = perfil.video_mbps
    cmd += ["-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq", "-multipass", "disabled",
            "-profile:v", "main10" if v.bits > 8 else "main", "-rc", "vbr",
            "-b:v", f"{mbps}M", "-maxrate", f"{round(mbps * 1.4)}M", "-bufsize", f"{round(mbps * 2.8)}M",
            "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1", "-bf", "4", "-b_ref_mode", "middle",
            "-g", "120", "-tag:v", "hvc1"]
    # Etiquetas de cor da origem (BT.2020/PQ no HDR10); os metadados de mastering/MaxCLL o ffmpeg repassa sozinho.
    if v.transferencia and v.transferencia != "unknown":
        cmd += ["-color_trc", v.transferencia]
        if v.primarias and v.primarias != "unknown":
            cmd += ["-color_primaries", v.primarias]
        if v.espaco and v.espaco != "unknown":
            cmd += ["-colorspace", v.espaco]
        cmd += ["-color_range", "tv"]

    for posicao, (trilha, copiar) in enumerate(selecao.audios):
        if copiar:
            cmd += [f"-c:a:{posicao}", "copy"]
        else:
            cmd += [f"-c:a:{posicao}", "eac3", f"-b:a:{posicao}", "640k", f"-ac:{posicao}", str(min(trilha.canais or 6, 6))]
    for posicao, trilha in enumerate(selecao.legendas):
        cmd += [f"-c:s:{posicao}", "copy" if trilha.codec in ("subrip", "srt", "ass", "ssa") else "srt"]
    for posicao in range(len(selecao.audios)):
        cmd += [f"-disposition:a:{posicao}", "default" if posicao == 0 else "0"]
    if selecao.legendas:
        cmd += ["-disposition:s", "0"]
    cmd += ["-reserve_index_space", str(ESPACO_RESERVADO_INDICE), "-f", "matroska", destino]
    return cmd


# ============================================================================ execução

@dataclass
class Progresso:
    fracao: float                   # 0..1
    fps: float = 0.0
    velocidade: float = 0.0         # × tempo real
    tamanho_bytes: int = 0
    restante_s: float | None = None


def interpretar_progresso(bloco: dict[str, str], duracao_s: float) -> Progresso | None:
    """Um bloco `-progress` do ffmpeg (chave=valor) → [Progresso]; None se ainda não tem tempo de saída."""
    try:
        tempo_us = int(bloco.get("out_time_us") or bloco.get("out_time_ms") or "")
    except ValueError:
        return None
    if tempo_us < 0:
        return None
    segundos = tempo_us / 1_000_000
    fracao = min(segundos / duracao_s, 1.0) if duracao_s > 0 else 0.0
    velocidade = 0.0
    bruto = (bloco.get("speed") or "").strip().rstrip("x")
    try:
        velocidade = float(bruto)
    except ValueError:
        pass
    try:
        fps = float(bloco.get("fps") or 0)
    except ValueError:
        fps = 0.0
    try:
        tamanho = int(bloco.get("total_size") or 0)
    except ValueError:
        tamanho = 0
    restante = (duracao_s - segundos) / velocidade if velocidade > 0 and duracao_s > 0 else None
    return Progresso(fracao, fps, velocidade, tamanho, max(restante, 0.0) if restante is not None else None)


def executar(
    comando: list[str],
    destino: str | os.PathLike,
    duracao_s: float,
    progresso: Callable[[Progresso], None],
    cancelado: Callable[[], bool],
) -> Path:
    """Roda o ffmpeg gravando em `<destino>.parcial` e, se der certo, renomeia para [destino].

    Bloqueante (chame de uma thread). Levanta [OtimizacaoCancelada] ou [ErroOtimizacao]; nos dois casos o
    arquivo parcial é apagado. [comando] já termina com o caminho de saída; passamos o `.parcial` no lugar.
    """
    destino = Path(destino)
    parcial = destino.with_name(destino.name + ".parcial")
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial.unlink(missing_ok=True)
    cmd = list(comando[:-1]) + [str(parcial)]
    erros = tempfile.TemporaryFile()
    try:
        processo = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=erros, stdin=subprocess.DEVNULL,
                                    creationflags=_SEM_JANELA)
    except OSError as erro:
        erros.close()
        raise ErroOtimizacao(f"Não consegui iniciar o ffmpeg: {erro}") from erro

    parar = threading.Event()
    pediu_cancelar = threading.Event()

    def vigiar() -> None:
        while not parar.wait(0.2):
            if cancelado():
                pediu_cancelar.set()
                try:
                    processo.terminate()
                except OSError:
                    pass
                return

    vigia = threading.Thread(target=vigiar, name="otimizar-cancelar", daemon=True)
    vigia.start()
    bloco: dict[str, str] = {}
    try:
        assert processo.stdout is not None
        for linha in processo.stdout:
            chave, _, valor = linha.decode("utf-8", "replace").strip().partition("=")
            if not chave:
                continue
            bloco[chave] = valor
            if chave == "progress":
                p = interpretar_progresso(bloco, duracao_s)
                if p is not None:
                    progresso(p)
                bloco = {}
        codigo = processo.wait()
    finally:
        parar.set()
        vigia.join(timeout=2)
        if processo.poll() is None:
            processo.kill()
    erros.seek(0)
    cauda = erros.read()[-1500:].decode("utf-8", "replace").strip()
    erros.close()

    if pediu_cancelar.is_set() or (cancelado() and codigo != 0):
        parcial.unlink(missing_ok=True)
        raise OtimizacaoCancelada()
    if codigo != 0 or not parcial.is_file() or parcial.stat().st_size == 0:
        parcial.unlink(missing_ok=True)
        raise ErroOtimizacao(f"O ffmpeg terminou com erro ({codigo}).\n{cauda}")
    os.replace(parcial, destino)
    progresso(Progresso(1.0, tamanho_bytes=destino.stat().st_size, restante_s=0.0))
    return destino
