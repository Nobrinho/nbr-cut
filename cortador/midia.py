"""Metadados do vídeo lidos do próprio arquivo (sem ffprobe): codec, resolução, HDR/Dolby Vision, áudios.

Serve para avisar ANTES de postar quando o arquivo tem um formato que o Nbr PLAY não toca bem (ex.: Dolby
Vision perfil 7 sem decodificador DV no aparelho) e para preencher qualidade/duração com dado real.

- MKV: lê só o início do arquivo (Info + Tracks, que ficam antes dos clusters) num parser EBML mínimo.
- MP4/MOV: percorre as caixas de topo com seek (o `moov` pode estar no fim) e lê só o `moov`.

Nada aqui copia ou decodifica o filme; o custo é de alguns MB lidos.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

LIMITE_CABECALHO_MKV = 16 * 1024 * 1024
LIMITE_MOOV = 192 * 1024 * 1024
BITRATE_ALTO_MBPS = 50.0

INFO, ATENCAO = "info", "atencao"


class MidiaIlegivel(Exception):
    """O arquivo não é um MKV/MP4 que dê para ler (ou está truncado)."""


@dataclass
class Video:
    codec: str                      # "HEVC", "H.264", "AV1", ...
    largura: int = 0
    altura: int = 0
    fps: float | None = None
    bits: int | None = None         # profundidade de cor
    hdr: str | None = None          # "HDR10" | "HLG"
    dv_perfil: int | None = None    # Dolby Vision: perfil (5, 7, 8…)
    dv_nivel: int | None = None
    dv_camada_extra: bool = False   # camada de realce (EL) presente = perfil de camada dupla


@dataclass
class Audio:
    codec: str
    canais: int = 0
    idioma: str | None = None
    titulo: str | None = None


@dataclass
class Aviso:
    nivel: str                      # INFO | ATENCAO
    texto: str


@dataclass
class InfoMidia:
    container: str                  # "mkv" | "mp4"
    duracao_s: float | None = None
    video: Video | None = None
    audios: list[Audio] = field(default_factory=list)
    legendas: int = 0

    # ------------------------------------------------------------------ derivados

    @property
    def resolucao(self) -> int:
        """Rótulo de resolução (2160, 1440, 1080, 720, 480) pela LARGURA: filme em tela larga tem altura menor."""
        if not self.video or not self.video.largura:
            return 0
        largura = self.video.largura
        for minimo, rotulo in ((3000, 2160), (2400, 1440), (1800, 1080), (1200, 720)):
            if largura >= minimo:
                return rotulo
        return 480

    @property
    def qualidade_sugerida(self) -> str | None:
        if not self.resolucao:
            return None
        qualidade = f"{self.resolucao}p"
        if self.video and (self.video.hdr or self.video.dv_perfil):
            qualidade += ", HDR"
        return qualidade

    def bitrate_medio_mbps(self, tamanho_bytes: int) -> float | None:
        if not self.duracao_s or self.duracao_s <= 0 or tamanho_bytes <= 0:
            return None
        return tamanho_bytes * 8 / self.duracao_s / 1_000_000

    def resumo_video(self) -> str:
        v = self.video
        if v is None:
            return "Sem trilha de vídeo"
        partes = []
        if self.resolucao:
            partes.append(f"{self.resolucao}p ({v.largura}x{v.altura})")
        partes.append(v.codec + (f" {v.bits}-bit" if v.bits and v.bits > 8 else ""))
        if v.dv_perfil:
            partes.append(f"Dolby Vision P{v.dv_perfil}")
        if v.hdr:
            partes.append(v.hdr)
        if v.fps:
            partes.append(f"{v.fps:.3f}".rstrip("0").rstrip(".").replace(".", ",") + " fps")
        if self.duracao_s:
            partes.append(_duracao_humana(self.duracao_s))
        return " · ".join(partes)

    def resumo_audio(self) -> str:
        if not self.audios:
            return "Sem trilha de áudio"
        itens = []
        for a in self.audios[:6]:
            canais = _canais(a.canais)
            idioma = f" ({a.idioma})" if a.idioma and a.idioma != "und" else ""
            itens.append(f"{a.codec}{' ' + canais if canais else ''}{idioma}")
        mais = f" +{len(self.audios) - 6}" if len(self.audios) > 6 else ""
        legendas = f" · {self.legendas} legenda(s)" if self.legendas else ""
        return f"{len(self.audios)} áudio(s): " + ", ".join(itens) + mais + legendas


def _duracao_humana(segundos: float) -> str:
    minutos = int(round(segundos / 60))
    horas, minutos = divmod(minutos, 60)
    return f"{horas}h{minutos:02d}min" if horas else f"{minutos}min"


def _canais(n: int) -> str:
    return {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}.get(n, f"{n}ch" if n else "")


# ============================================================================ avaliação

def avaliar(info: InfoMidia, tamanho_bytes: int = 0) -> list[Aviso]:
    """O que o usuário deve saber antes de postar este arquivo. Lista vazia = nada de especial."""
    avisos: list[Aviso] = []
    v = info.video
    if v is None:
        return [Aviso(ATENCAO, "O arquivo não tem trilha de vídeo.")]

    if v.dv_perfil == 7:
        avisos.append(Aviso(
            ATENCAO,
            "Dolby Vision perfil 7 (camada dupla de UHD Blu-ray). Aparelhos sem decodificador DV tocam só a camada "
            "base em HDR10 (o Nbr PLAY faz isso); sem decodificador HEVC de hardware pode não ter imagem.",
        ))
    elif v.dv_perfil == 5:
        avisos.append(Aviso(
            ATENCAO,
            "Dolby Vision perfil 5 não tem camada base compatível: sem decodificador DV a imagem sai com cores "
            "distorcidas (verde/roxo).",
        ))
    elif v.dv_perfil:
        avisos.append(Aviso(INFO, f"Dolby Vision perfil {v.dv_perfil}: toca como HDR10 onde não há decodificador DV."))

    if v.codec == "AV1":
        avisos.append(Aviso(ATENCAO, "AV1 só toca em aparelhos com decodificador AV1 (hardware recente)."))
    elif v.codec in ("VC-1", "MPEG-2", "MPEG-4"):
        avisos.append(Aviso(ATENCAO, f"{v.codec} é um codec antigo: pode não ter decodificador em todos os aparelhos."))
    elif v.codec == "H.264" and v.bits and v.bits > 8:
        avisos.append(Aviso(ATENCAO, "H.264 de 10 bits quase não tem decodificador de hardware nos aparelhos."))

    mbps = info.bitrate_medio_mbps(tamanho_bytes)
    if mbps and mbps >= BITRATE_ALTO_MBPS:
        avisos.append(Aviso(
            ATENCAO,
            f"Bitrate médio de {mbps:.0f} Mbps: exige conexão rápida e vai engasgar em redes mais lentas.",
        ))

    if not info.audios:
        avisos.append(Aviso(ATENCAO, "O arquivo não tem trilha de áudio."))
    else:
        idiomas = {(a.idioma or "und").lower()[:2] for a in info.audios}
        if idiomas - {"un"} and "pt" not in idiomas:
            avisos.append(Aviso(INFO, "Nenhuma trilha de áudio em português."))
        if any(a.codec in ("DTS", "TrueHD") for a in info.audios):
            avisos.append(Aviso(INFO, "DTS/TrueHD são decodificados por software no app (usa mais bateria)."))
    return avisos


# ============================================================================ leitura

def ler(caminho: str | os.PathLike) -> InfoMidia:
    """Lê os metadados de [caminho]. Levanta [MidiaIlegivel] se não for um MKV/MP4 legível."""
    try:
        with open(caminho, "rb") as arquivo:
            abertura = arquivo.read(12)
            if abertura[:4] == b"\x1a\x45\xdf\xa3":
                arquivo.seek(0)
                return _ler_mkv(arquivo.read(LIMITE_CABECALHO_MKV))
            if abertura[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
                return _ler_mp4(arquivo)
    except OSError as erro:
        raise MidiaIlegivel(f"não consegui abrir o arquivo: {erro}") from erro
    raise MidiaIlegivel("formato não reconhecido (só MKV e MP4)")


# ---------------------------------------------------------------------------- Dolby Vision

def _dv_de_configuracao(dados: bytes) -> tuple[int, int, bool] | None:
    """(perfil, nível, tem camada de realce) do registro `dvcC`/`dvvC`."""
    if len(dados) < 4:
        return None
    perfil = dados[2] >> 1
    nivel = ((dados[2] & 1) << 5) | (dados[3] >> 3)
    camada_extra = bool((dados[3] >> 1) & 1)
    return perfil, nivel, camada_extra


def _bits_hevc(hvcc: bytes) -> int | None:
    return (hvcc[17] & 7) + 8 if len(hvcc) > 17 else None


def _bits_avc(avcc: bytes) -> int | None:
    if len(avcc) < 2:
        return None
    return 10 if avcc[1] in (110, 122, 244) else 8


# ---------------------------------------------------------------------------- MKV (EBML)

_ID_SEGMENT, _ID_INFO, _ID_TRACKS, _ID_CLUSTER = 0x18538067, 0x1549A966, 0x1654AE6B, 0x1F43B675
_ID_TIMECODE_SCALE, _ID_DURATION = 0x2AD7B1, 0x4489
_ID_TRACK_ENTRY, _ID_TRACK_TYPE, _ID_CODEC_ID, _ID_CODEC_PRIVATE = 0xAE, 0x83, 0x86, 0x63A2
_ID_NAME, _ID_LANGUAGE, _ID_LANGUAGE_BCP47, _ID_DEFAULT_DURATION = 0x536E, 0x22B59C, 0x22B59D, 0x23E383
_ID_VIDEO, _ID_AUDIO = 0xE0, 0xE1
_ID_PIXEL_W, _ID_PIXEL_H, _ID_COLOUR = 0xB0, 0xBA, 0x55B0
_ID_BITS_PER_CHANNEL, _ID_TRANSFER, _ID_PRIMARIES = 0x55B2, 0x55BA, 0x55BB
_ID_CHANNELS = 0x9F
_ID_BLOCK_ADD_MAPPING, _ID_BLOCK_ADD_TYPE, _ID_BLOCK_ADD_DATA = 0x41E4, 0x41E7, 0x41ED
_MASTER_MKV = {_ID_SEGMENT, _ID_INFO, _ID_TRACKS, _ID_TRACK_ENTRY, _ID_VIDEO, _ID_AUDIO, _ID_COLOUR, _ID_BLOCK_ADD_MAPPING}
_TIPO_DVCC, _TIPO_DVVC = 0x64766343, 0x64767643  # "dvcC", "dvvC"

_CODECS_VIDEO_MKV = {
    "V_MPEGH/ISO/HEVC": "HEVC", "V_MPEG4/ISO/AVC": "H.264", "V_AV1": "AV1", "V_VP9": "VP9", "V_VP8": "VP8",
    "V_MPEG2": "MPEG-2", "V_MPEG4/ISO/ASP": "MPEG-4", "V_MPEG4/ISO/SP": "MPEG-4", "V_MPEG4/ISO/AP": "MPEG-4",
}
_CODECS_AUDIO_MKV = {
    "A_AC3": "AC3", "A_EAC3": "EAC3", "A_TRUEHD": "TrueHD", "A_DTS": "DTS", "A_DTS/EXPRESS": "DTS",
    "A_DTS/LOSSLESS": "DTS", "A_OPUS": "Opus", "A_FLAC": "FLAC", "A_VORBIS": "Vorbis", "A_MPEG/L3": "MP3",
    "A_MPEG/L2": "MP2",
}
_IDIOMAS = {
    "por": "pt", "eng": "en", "spa": "es", "fra": "fr", "fre": "fr", "ita": "it", "deu": "de", "ger": "de",
    "jpn": "ja", "rus": "ru", "hin": "hi", "kor": "ko", "zho": "zh", "chi": "zh", "und": "und",
}


def _vint_id(b: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(b):
        raise MidiaIlegivel("cabeçalho truncado")
    primeiro = b[pos]
    tamanho = 1
    marcador = 0x80
    while tamanho <= 4 and not primeiro & marcador:
        tamanho += 1
        marcador >>= 1
    if tamanho > 4 or pos + tamanho > len(b):
        raise MidiaIlegivel("elemento EBML inválido")
    return int.from_bytes(b[pos:pos + tamanho], "big"), tamanho


def _vint_tamanho(b: bytes, pos: int) -> tuple[int | None, int]:
    """(tamanho do payload ou None se desconhecido, bytes usados)."""
    if pos >= len(b):
        raise MidiaIlegivel("cabeçalho truncado")
    primeiro = b[pos]
    tamanho = 1
    marcador = 0x80
    while tamanho <= 8 and not primeiro & marcador:
        tamanho += 1
        marcador >>= 1
    if tamanho > 8 or pos + tamanho > len(b):
        raise MidiaIlegivel("tamanho EBML inválido")
    valor = int.from_bytes(b[pos:pos + tamanho], "big") & ((1 << (7 * tamanho)) - 1)
    return (None if valor == (1 << (7 * tamanho)) - 1 else valor), tamanho


def _elementos(b: bytes, inicio: int, fim: int):
    """(id, início do payload, fim do payload) de cada elemento filho em [inicio, fim)."""
    pos = inicio
    while pos < fim:
        id_, n_id = _vint_id(b, pos)
        tamanho, n_tam = _vint_tamanho(b, pos + n_id)
        ini = pos + n_id + n_tam
        if tamanho is None:                    # tamanho desconhecido (Segment ao vivo): vai até o fim
            tamanho = fim - ini
        yield id_, ini, min(ini + tamanho, fim)
        if ini + tamanho > fim:        # elemento passa do limite do pai (arquivo truncado): para aqui
            return
        pos = ini + tamanho


def _uint(b: bytes, ini: int, fim: int) -> int:
    return int.from_bytes(b[ini:fim], "big")


def _float(b: bytes, ini: int, fim: int) -> float:
    tamanho = fim - ini
    if tamanho == 4:
        return struct.unpack(">f", b[ini:fim])[0]
    if tamanho == 8:
        return struct.unpack(">d", b[ini:fim])[0]
    return 0.0


def _texto(b: bytes, ini: int, fim: int) -> str:
    return b[ini:fim].split(b"\x00")[0].decode("utf-8", "replace")


def _ler_mkv(b: bytes) -> InfoMidia:
    info = InfoMidia(container="mkv")
    escala = 1_000_000
    tracks_achada = False
    for id_, ini, fim in _elementos(b, 0, len(b)):
        if id_ != _ID_SEGMENT:
            continue
        for sid, sini, sfim in _elementos(b, ini, fim):
            if sid == _ID_INFO:
                duracao = 0.0
                for iid, iini, ifim in _elementos(b, sini, sfim):
                    if iid == _ID_TIMECODE_SCALE:
                        escala = _uint(b, iini, ifim) or escala
                    elif iid == _ID_DURATION:
                        duracao = _float(b, iini, ifim)
                if duracao:
                    info.duracao_s = duracao * escala / 1e9
            elif sid == _ID_TRACKS:
                tracks_achada = True
                for tid, tini, tfim in _elementos(b, sini, sfim):
                    if tid == _ID_TRACK_ENTRY:
                        _trilha_mkv(b, tini, tfim, info)
            elif sid == _ID_CLUSTER:
                break
        break
    if not tracks_achada:
        raise MidiaIlegivel("não achei as trilhas no início do arquivo")
    return info


def _trilha_mkv(b: bytes, ini: int, fim: int, info: InfoMidia) -> None:
    tipo = 0
    codec_id = ""
    privado = b""
    nome = None
    idioma = "eng"
    bcp47 = None
    padrao_ns = 0
    largura = altura = 0
    canais = 0
    transferencia = primarias = None
    bits_cor = None
    dv: tuple[int, int, bool] | None = None
    for id_, i, f in _elementos(b, ini, fim):
        if id_ == _ID_TRACK_TYPE:
            tipo = _uint(b, i, f)
        elif id_ == _ID_CODEC_ID:
            codec_id = _texto(b, i, f)
        elif id_ == _ID_CODEC_PRIVATE:
            privado = b[i:f]
        elif id_ == _ID_NAME:
            nome = _texto(b, i, f)
        elif id_ == _ID_LANGUAGE:
            idioma = _texto(b, i, f)
        elif id_ == _ID_LANGUAGE_BCP47:
            bcp47 = _texto(b, i, f)
        elif id_ == _ID_DEFAULT_DURATION:
            padrao_ns = _uint(b, i, f)
        elif id_ == _ID_VIDEO:
            for vid, vi, vf in _elementos(b, i, f):
                if vid == _ID_PIXEL_W:
                    largura = _uint(b, vi, vf)
                elif vid == _ID_PIXEL_H:
                    altura = _uint(b, vi, vf)
                elif vid == _ID_COLOUR:
                    for cid, ci, cf in _elementos(b, vi, vf):
                        if cid == _ID_TRANSFER:
                            transferencia = _uint(b, ci, cf)
                        elif cid == _ID_PRIMARIES:
                            primarias = _uint(b, ci, cf)
                        elif cid == _ID_BITS_PER_CHANNEL:
                            bits_cor = _uint(b, ci, cf)
        elif id_ == _ID_AUDIO:
            for aid, ai, af in _elementos(b, i, f):
                if aid == _ID_CHANNELS:
                    canais = _uint(b, ai, af)
        elif id_ == _ID_BLOCK_ADD_MAPPING:
            tipo_mapa, dados = None, b""
            for mid, mi, mf in _elementos(b, i, f):
                if mid == _ID_BLOCK_ADD_TYPE:
                    tipo_mapa = _uint(b, mi, mf)
                elif mid == _ID_BLOCK_ADD_DATA:
                    dados = b[mi:mf]
            if tipo_mapa in (_TIPO_DVCC, _TIPO_DVVC):
                dv = _dv_de_configuracao(dados) or dv

    if tipo == 1 and info.video is None:
        video = Video(codec=_CODECS_VIDEO_MKV.get(codec_id) or _codec_vfw(codec_id, privado) or codec_id or "?",
                      largura=largura, altura=altura, fps=round(1e9 / padrao_ns, 3) if padrao_ns else None)
        if video.codec == "HEVC":
            video.bits = _bits_hevc(privado)
        elif video.codec == "H.264":
            video.bits = _bits_avc(privado[:5] if codec_id.endswith("AVC") else b"")
        video.bits = video.bits or bits_cor
        video.hdr = _hdr(transferencia)
        if dv:
            video.dv_perfil, video.dv_nivel, video.dv_camada_extra = dv
        info.video = video
    elif tipo == 2:
        codec = _CODECS_AUDIO_MKV.get(codec_id) or ("AAC" if codec_id.startswith("A_AAC") else
                                                    "PCM" if codec_id.startswith("A_PCM") else codec_id[2:] or "?")
        lingua = _IDIOMAS.get((bcp47 or idioma or "und").lower(), (bcp47 or idioma or "und").lower()[:2])
        info.audios.append(Audio(codec=codec, canais=canais, idioma=lingua, titulo=nome))
    elif tipo == 17:
        info.legendas += 1


def _codec_vfw(codec_id: str, privado: bytes) -> str | None:
    if codec_id != "V_MS/VFW/FOURCC" or len(privado) < 20:
        return None
    fourcc = privado[16:20]
    return "VC-1" if fourcc in (b"WVC1", b"WMV3", b"WMVA") else fourcc.decode("latin-1", "replace")


def _hdr(transferencia: int | None) -> str | None:
    return {16: "HDR10", 18: "HLG"}.get(transferencia) if transferencia is not None else None


# ---------------------------------------------------------------------------- MP4 / MOV

_CAIXAS_MP4 = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_VISUAIS_MP4 = {
    b"hvc1": "HEVC", b"hev1": "HEVC", b"dvh1": "HEVC", b"dvhe": "HEVC", b"avc1": "H.264", b"avc3": "H.264",
    b"dva1": "H.264", b"dvav": "H.264", b"av01": "AV1", b"vp09": "VP9", b"mp4v": "MPEG-4",
}
_AUDIOS_MP4 = {
    b"mp4a": "AAC", b"ac-3": "AC3", b"ec-3": "EAC3", b"dtsc": "DTS", b"dtsh": "DTS", b"dtse": "DTS",
    b"dtsl": "DTS", b"Opus": "Opus", b"fLaC": "FLAC", b"alac": "ALAC", b".mp3": "MP3",
}
_LEGENDAS_MP4 = {b"tx3g", b"wvtt", b"stpp", b"text", b"sbtl"}


def _caixas(b: bytes, inicio: int, fim: int):
    """(tipo, início do payload, fim) de cada caixa MP4 em [inicio, fim)."""
    pos = inicio
    while pos + 8 <= fim:
        tamanho, tipo = struct.unpack(">I4s", b[pos:pos + 8])
        cabecalho = 8
        if tamanho == 1:
            if pos + 16 > fim:
                return
            tamanho = struct.unpack(">Q", b[pos + 8:pos + 16])[0]
            cabecalho = 16
        elif tamanho == 0:
            tamanho = fim - pos
        if tamanho < cabecalho:
            return
        yield tipo, pos + cabecalho, min(pos + tamanho, fim)
        pos += tamanho


def _ler_mp4(arquivo) -> InfoMidia:
    tamanho_total = arquivo.seek(0, os.SEEK_END)
    pos = 0
    moov = None
    while pos + 8 <= tamanho_total:
        arquivo.seek(pos)
        cabecalho = arquivo.read(16)
        if len(cabecalho) < 8:
            break
        tamanho, tipo = struct.unpack(">I4s", cabecalho[:8])
        if tamanho == 1 and len(cabecalho) >= 16:
            tamanho = struct.unpack(">Q", cabecalho[8:16])[0]
        elif tamanho == 0:
            tamanho = tamanho_total - pos
        if tamanho < 8:
            break
        if tipo == b"moov":
            if tamanho > LIMITE_MOOV:
                raise MidiaIlegivel("índice (moov) grande demais")
            arquivo.seek(pos)
            moov = arquivo.read(tamanho)
            break
        pos += tamanho
    if moov is None:
        raise MidiaIlegivel("não achei o índice (moov) do MP4")

    info = InfoMidia(container="mp4")
    for tipo, ini, fim in _caixas(moov, 0, len(moov)):
        if tipo != b"moov":
            continue
        for ctipo, cini, cfim in _caixas(moov, ini, fim):
            if ctipo == b"mvhd":
                info.duracao_s = _duracao_mvhd(moov[cini:cfim])
            elif ctipo == b"trak":
                _trilha_mp4(moov, cini, cfim, info)
    return info


def _duracao_mvhd(dados: bytes) -> float | None:
    if not dados:
        return None
    if dados[0] == 1 and len(dados) >= 32:
        escala, duracao = struct.unpack(">IQ", dados[20:32])
    elif len(dados) >= 20:
        escala, duracao = struct.unpack(">II", dados[12:20])
    else:
        return None
    return duracao / escala if escala else None


def _trilha_mp4(b: bytes, ini: int, fim: int, info: InfoMidia) -> None:
    tipo_trilha = None
    idioma = None
    entrada = None

    def descer(i: int, f: int) -> None:
        nonlocal tipo_trilha, idioma, entrada
        for tipo, ci, cf in _caixas(b, i, f):
            if tipo in _CAIXAS_MP4:
                descer(ci, cf)
            elif tipo == b"hdlr" and cf - ci >= 12:
                tipo_trilha = b[ci + 8:ci + 12]
            elif tipo == b"mdhd" and cf - ci >= 24:
                versao = b[ci]
                deslocamento = ci + (32 if versao == 1 else 20)
                if deslocamento + 2 <= cf:
                    codigo = struct.unpack(">H", b[deslocamento:deslocamento + 2])[0]
                    letras = [(codigo >> 10) & 31, (codigo >> 5) & 31, codigo & 31]
                    idioma = "".join(chr(0x60 + n) for n in letras) if all(1 <= n <= 26 for n in letras) else None
            elif tipo == b"stsd" and cf - ci >= 16:
                entrada = (ci + 8, cf)       # pula versão/flags e a contagem de entradas

    descer(ini, fim)
    if entrada is None or tipo_trilha is None:
        return
    for etipo, eini, efim in _caixas(b, *entrada):
        if tipo_trilha == b"vide" and info.video is None and etipo in _VISUAIS_MP4:
            info.video = _video_mp4(b, etipo, eini, efim)
        elif tipo_trilha == b"soun" and etipo in _AUDIOS_MP4:
            canais = struct.unpack(">H", b[eini + 16:eini + 18])[0] if efim - eini >= 28 else 0
            lingua = _IDIOMAS.get(idioma or "und", idioma or "und")
            info.audios.append(Audio(codec=_AUDIOS_MP4[etipo], canais=canais, idioma=lingua))
        elif tipo_trilha in (b"sbtl", b"subt", b"text") or etipo in _LEGENDAS_MP4:
            info.legendas += 1
        break


def _video_mp4(b: bytes, etipo: bytes, ini: int, fim: int) -> Video:
    largura, altura = struct.unpack(">HH", b[ini + 24:ini + 28]) if fim - ini >= 28 else (0, 0)
    video = Video(codec=_VISUAIS_MP4[etipo], largura=largura, altura=altura)
    if etipo in (b"dvh1", b"dvhe", b"dva1", b"dvav"):
        video.dv_perfil = video.dv_perfil or 0
    transferencia = None
    for tipo, ci, cf in _caixas(b, ini + 78, fim):
        if tipo == b"hvcC":
            video.bits = _bits_hevc(b[ci:cf])
        elif tipo == b"avcC":
            video.bits = _bits_avc(b[ci:cf])
        elif tipo in (b"dvcC", b"dvvC"):
            dv = _dv_de_configuracao(b[ci:cf])
            if dv:
                video.dv_perfil, video.dv_nivel, video.dv_camada_extra = dv
        elif tipo == b"colr" and b[ci:ci + 4] == b"nclx" and cf - ci >= 10:
            transferencia = struct.unpack(">H", b[ci + 6:ci + 8])[0]
    video.hdr = _hdr(transferencia)
    if video.dv_perfil == 0:
        video.dv_perfil = None
    return video
