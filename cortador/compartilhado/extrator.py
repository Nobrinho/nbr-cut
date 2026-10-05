"""Título, ano, qualidade e áudio a partir do nome do arquivo.

CÓPIA dos trechos que o Cortador usa de agente_filmes/extrator.py (copiado em 2026-10-04).
A fonte da verdade é o bot; o contrato é vigiado por tests/test_contrato_bot.py
(roda quando o repo do bot está ao lado e pula quando não está). Se o bot mudar, atualize aqui.
Fora da cópia: detecção de episódios/séries (o Cortador só posta filmes).
"""
import re
from datetime import date


# Termos comuns de qualidade/formato usados em releases de filme
PADROES_QUALIDADE = [
    r"4K", r"2160p", r"1080p", r"720p", r"480p",
    r"BluRay", r"Blu-Ray", r"WEB-?DL", r"WEBRip", r"HDRip", r"DVDRip", r"REMUX",
    r"RMZ", r"Dublado", r"Legendado", r"DUAL", r"Nacional",
    r"AC3", r"DTS", r"HDR", r"10bit", r"AV1", r"VP9", r"XviD",
]


PADROES_QUALIDADE_REGEX = [rf"\b{padrao}\b" for padrao in PADROES_QUALIDADE]


REGEX_QUALIDADE = re.compile("|".join(PADROES_QUALIDADE_REGEX), re.IGNORECASE)


# Coisas pra remover do "nome bruto" até sobrar só o título
REGEX_LIMPEZA = re.compile(
    r"[\.\_]|(" + "|".join(PADROES_QUALIDADE_REGEX) + r")|(\d{4})|\b(x264|x265|HEVC|AAC|H\.?264)\b",
    re.IGNORECASE,
)


# Tags de áudio tipo "5.1", "2.0", "7.1" (precisam ser removidas ANTES de trocar
# os pontos por espaço, senão "2.0" vira "2 0" e some o padrão do dot)
REGEX_CANAIS_AUDIO = re.compile(r"\b\d\.\d(?:ch)?\b", re.IGNORECASE)


# Tags entre colchetes/parênteses no fim da legenda, tipo "[MKV]", "(2024)"
REGEX_TAG_COLCHETE = re.compile(r"[\[\(].*?[\]\)]")


# Termos de áudio pro campo "🗣 Áudio:" do template novo (subconjunto da qualidade)
_AUDIO_CANONICO = {"dublado": "Dublado", "legendado": "Legendado", "dual": "Dual"}


REGEX_AUDIO = re.compile("|".join(_AUDIO_CANONICO.keys()), re.IGNORECASE)


REGEX_ANO = re.compile(r"\b(19\d{2}|20\d{2})\b")


REGEX_TITULO_NUMERICO_COM_ANO = re.compile(r"^\s*((?:19|20)\d{2})\s*[\[\(]\s*(?:19|20)\d{2}\s*[\]\)]\s*$")


def extrair_audio(texto: str) -> str | None:
    """Extrai as tags de áudio (Dublado/Legendado/Dual) do texto, pro campo 'Áudio' do template.

    Retorna None se não achar nenhuma (o node que monta a legenda decide o texto padrão).
    """
    encontrados = REGEX_AUDIO.findall(texto)
    if not encontrados:
        return None
    vistos = []
    for termo in encontrados:
        canonico = _AUDIO_CANONICO.get(termo.lower())
        if canonico and canonico not in vistos:
            vistos.append(canonico)
    return ", ".join(vistos) if vistos else None


def extrair_qualidade(texto: str) -> str:
    """Retorna os termos de qualidade encontrados no texto, ex: '1080p, Dublado'."""
    encontrados = REGEX_QUALIDADE.findall(texto)
    if not encontrados:
        return "Qualidade não informada"
    # remove duplicados mantendo ordem
    vistos = []
    for termo in encontrados:
        termo_norm = termo.strip()
        if termo_norm and termo_norm.lower() not in [v.lower() for v in vistos]:
            vistos.append(termo_norm)
    return ", ".join(vistos)


def extrair_ano(texto: str) -> str | None:
    """Extrai ano de release, evitando números de títulos e intervalos históricos."""
    maximo = date.today().year + 1
    candidatos = []
    for match in REGEX_ANO.finditer(texto or ""):
        ano = int(match.group(1))
        entorno = (texto[max(0, match.start() - 5):match.end() + 5] or "").strip()
        if ano > maximo or re.search(r"\d{4}\s*[–—-]\s*\d{4}", entorno):
            continue
        candidatos.append(str(ano))
    return candidatos[-1] if candidatos else None


def _limpar_nome_arquivo(nome_arquivo: str) -> str:
    """Limpa um nome de arquivo estilo release (Toy.Story.1995.1080p.BluRay...)."""
    sem_ext = re.sub(r"\.(mp4|mkv|avi|mov)$", "", nome_arquivo, flags=re.IGNORECASE)
    # remove tags de canal de áudio (5.1, 2.0...) antes de mexer nos pontos
    sem_ext = REGEX_CANAIS_AUDIO.sub(" ", sem_ext)
    limpo = sem_ext.replace(".", " ").replace("_", " ")
    limpo = REGEX_LIMPEZA.sub(" ", limpo)
    limpo = re.sub(r"[\[\]\(\)]", " ", limpo)
    return re.sub(r"\s+", " ", limpo).strip()


def _limpar_primeira_linha_legenda(primeira_linha: str) -> str:
    match_titulo_numerico = REGEX_TITULO_NUMERICO_COM_ANO.match(primeira_linha)
    if match_titulo_numerico:
        return match_titulo_numerico.group(1)

    primeira_linha = REGEX_TAG_COLCHETE.sub("", primeira_linha).strip()
    primeira_linha = REGEX_LIMPEZA.sub(" ", primeira_linha)
    primeira_linha = re.sub(r"[\[\]\(\)]", " ", primeira_linha)
    return re.sub(r"\s+", " ", primeira_linha).strip()


def extrair_titulo(legenda: str | None, nome_arquivo: str) -> str:
    """Tenta extrair o título do filme a partir da legenda (prioridade) ou do nome do arquivo.

    A legenda de um filme ENCAMINHADO costuma trazer, junto do título, coisas como
    "[MKV]" e uma linha de créditos com "@canal | @canal2" do grupo de origem — isso
    não pode ir pro TMDB. Por isso: pega só a primeira linha da legenda, remove tags
    entre colchetes/parênteses, e só usa essa legenda se o resultado não tiver "@" ou
    "|" sobrando (sinal de que ainda tem lixo de créditos/marca d'água). Caso contrário,
    cai pro nome do arquivo, que costuma ser mais confiável pra extrair o título puro.
    """
    if legenda and legenda.strip():
        primeira_linha = legenda.strip().splitlines()[0]
        primeira_linha = re.split(
            r"(?i)\b(?:legenda do v[ií]deo|contexto anterior|origem)\s*:", primeira_linha
        )[0].strip()
        primeira_linha = _limpar_primeira_linha_legenda(primeira_linha)
        if primeira_linha and "@" not in primeira_linha and "|" not in primeira_linha:
            return primeira_linha

    titulo_do_arquivo = _limpar_nome_arquivo(nome_arquivo)
    return titulo_do_arquivo if titulo_do_arquivo else nome_arquivo
