"""Divisão de um arquivo grande em partes por BYTES, sem recodificar e sem copiar para disco.

Mesmo contrato de `ntv2/tools/ntv2_split.py` (o app Nbr PLAY junta as partes na hora de tocar):
partes de tamanho quase igual, cada uma dentro do limite de upload do Telegram, chamadas
`<arquivo>.partNNofMM`. O nome da parte vem de `partes.formatar_nome` (fonte única do formato).

Aqui a divisão é só um PLANO (`montar_partes`); o conteúdo é lido sob demanda por
`LeitorDeIntervalo` direto do arquivo original, para o upload ou para gravar as partes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from cortador.compartilhado import formato

MIB = 1024 * 1024

# Limites de upload do Telegram (conta comum / Premium).
LIMITE_GRATIS = 2000 * MIB
LIMITE_PREMIUM = 4000 * MIB
# Um pouco abaixo do limite, para não bater na borda.
TAMANHO_PADRAO = 1900 * MIB
TAMANHO_PADRAO_PREMIUM = 3900 * MIB

BLOCO = 8 * MIB
SUFIXO_MANIFESTO = ".manifest.json"

Progresso = Optional[Callable[[int, int], None]]  # (feito, total)

_RE_TAMANHO = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmg]?)(?:i?b)?\s*", re.IGNORECASE)
_UNIDADES = {"": 1, "k": 1024, "m": MIB, "g": 1024 * MIB}


class ErroDivisao(Exception):
    """Erro de uso/ambiente com mensagem pronta para mostrar ao usuário."""


def parse_tamanho(texto: str) -> int:
    """'1900M', '1.9G', '500k' ou '123' (bytes). Base 1024."""
    encontrado = _RE_TAMANHO.fullmatch(texto or "")
    if not encontrado:
        raise ErroDivisao(f"Tamanho inválido: {texto!r} (use por ex. 1900M ou 1.9G)")
    valor = int(float(encontrado.group(1)) * _UNIDADES[encontrado.group(2).lower()])
    if valor <= 0:
        raise ErroDivisao(f"Tamanho inválido: {texto!r}")
    return valor


def humano(n: int) -> str:
    if n >= 1024 * MIB:
        return f"{n / (1024 * MIB):.2f} GiB"
    if n >= MIB:
        return f"{n / MIB:.1f} MiB"
    return f"{n} B"


def limite_da_conta(premium: bool) -> int:
    return LIMITE_PREMIUM if premium else LIMITE_GRATIS


def tamanho_padrao(premium: bool) -> int:
    return TAMANHO_PADRAO_PREMIUM if premium else TAMANHO_PADRAO


def planejar_tamanhos(total: int, maximo: int) -> list[int]:
    """Tamanhos das partes: o MÍNIMO de partes que respeita [maximo], quase iguais (diferença de no
    máximo 1 byte; nunca uma última parte minúscula). Cabendo numa parte só, devolve [total]."""
    if total <= 0:
        raise ErroDivisao("O arquivo está vazio.")
    if maximo <= 0:
        raise ErroDivisao("O tamanho máximo da parte precisa ser maior que zero.")
    quantidade = -(-total // maximo)  # teto
    base, sobra = divmod(total, quantidade)
    return [base + 1 if i < sobra else base for i in range(quantidade)]


@dataclass(frozen=True)
class Parte:
    indice: int         # 1..total_partes, como no nome
    total_partes: int
    offset: int         # posição da parte no arquivo original
    tamanho: int
    nome: str           # nome do arquivo no Telegram (com .partNNofMM quando há várias)


def montar_partes(nome_arquivo: str, total: int, maximo: int) -> list[Parte]:
    """Plano completo: uma [Parte] por pedaço, com offset e nome. Arquivo que cabe numa parte
    só vira uma única [Parte] com o nome do próprio arquivo (sem sufixo)."""
    tamanhos = planejar_tamanhos(total, maximo)
    n = len(tamanhos)
    partes: list[Parte] = []
    offset = 0
    for i, tamanho in enumerate(tamanhos, start=1):
        nome = nome_arquivo if n == 1 else formato.formatar_nome(nome_arquivo, i, n)
        partes.append(Parte(i, n, offset, tamanho, nome))
        offset += tamanho
    return partes


class LeitorDeIntervalo:
    """Objeto de arquivo (só leitura) de um intervalo [offset, offset+tamanho) de um arquivo em
    disco. O upload lê a parte direto do original — sem gravar uma cópia dos 20 GB.

    `read(n)` devolve EXATAMENTE n bytes enquanto houver (o upload do Telegram exige blocos
    cheios), e `b""` no fim do intervalo.
    """

    def __init__(self, caminho: str | os.PathLike, offset: int, tamanho: int, nome: str | None = None):
        if offset < 0 or tamanho < 0:
            raise ValueError("offset/tamanho inválidos")
        self._caminho = os.fspath(caminho)
        self._offset = offset
        self._tamanho = tamanho
        self.name = nome or os.path.basename(self._caminho)
        self._pos = 0
        self._arquivo = None

    def _abrir(self):
        if self._arquivo is None:
            self._arquivo = open(self._caminho, "rb")
        return self._arquivo

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            novo = pos
        elif whence == os.SEEK_CUR:
            novo = self._pos + pos
        elif whence == os.SEEK_END:
            novo = self._tamanho + pos
        else:
            raise ValueError("whence inválido")
        self._pos = max(0, min(novo, self._tamanho))
        return self._pos

    def read(self, n: int = -1) -> bytes:
        restante = self._tamanho - self._pos
        if restante <= 0:
            return b""
        quero = restante if n is None or n < 0 else min(n, restante)
        arquivo = self._abrir()
        arquivo.seek(self._offset + self._pos)
        partes: list[bytes] = []
        faltam = quero
        while faltam > 0:
            pedaco = arquivo.read(faltam)
            if not pedaco:
                raise OSError(f"{self._caminho} acabou antes do esperado (o arquivo mudou?)")
            partes.append(pedaco)
            faltam -= len(pedaco)
        self._pos += quero
        return b"".join(partes)

    def __len__(self) -> int:
        return self._tamanho

    def close(self) -> None:
        if self._arquivo is not None:
            self._arquivo.close()
            self._arquivo = None

    def __enter__(self) -> "LeitorDeIntervalo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def sha256_do_intervalo(caminho: str | os.PathLike, offset: int, tamanho: int) -> str:
    h = hashlib.sha256()
    with LeitorDeIntervalo(caminho, offset, tamanho) as leitor:
        while True:
            bloco = leitor.read(BLOCO)
            if not bloco:
                return h.hexdigest()
            h.update(bloco)


def gravar_partes(
    origem: str | os.PathLike,
    plano: list[Parte],
    pasta: str | os.PathLike,
    *,
    forcar: bool = False,
    progresso: Progresso = None,
) -> dict:
    """Modo 'só cortar': grava as partes numa pasta (com manifesto compatível com
    `ntv2_split.py verify/join`). Escreve em .tmp e renomeia no fim; apaga o que gravou se falhar."""
    origem = Path(origem)
    pasta = Path(pasta)
    if len(plano) < formato.MIN_PARTES:
        raise ErroDivisao("O arquivo cabe numa parte só: não precisa cortar.")
    total = sum(p.tamanho for p in plano)
    if origem.stat().st_size != total:
        raise ErroDivisao("O arquivo mudou de tamanho desde o plano. Refaça o plano.")
    existentes = [p.nome for p in plano if (pasta / p.nome).exists()]
    if (existentes or (pasta / (plano[0].nome.rsplit(".part", 1)[0] + SUFIXO_MANIFESTO)).exists()) and not forcar:
        raise ErroDivisao(f"Já existem partes em {pasta}. Marque para sobrescrever.")
    pasta.mkdir(parents=True, exist_ok=True)
    livre = shutil.disk_usage(pasta).free
    if livre < total + 64 * MIB:
        raise ErroDivisao(f"Espaço insuficiente em {pasta}: livre {humano(livre)}, preciso de {humano(total)}.")

    base = plano[0].nome.rsplit(".part", 1)[0]
    geral = hashlib.sha256()
    feito = 0
    gravadas: list[Path] = []
    manifesto = {"name": base, "size": total, "parts": []}
    try:
        for parte in plano:
            tmp = pasta / (parte.nome + ".tmp")
            final = pasta / parte.nome
            h = hashlib.sha256()
            with LeitorDeIntervalo(origem, parte.offset, parte.tamanho) as leitor, tmp.open("wb") as destino:
                while True:
                    bloco = leitor.read(BLOCO)
                    if not bloco:
                        break
                    destino.write(bloco)
                    h.update(bloco)
                    geral.update(bloco)
                    feito += len(bloco)
                    if progresso:
                        progresso(feito, total)
            os.replace(tmp, final)
            gravadas.append(final)
            manifesto["parts"].append({"name": parte.nome, "size": parte.tamanho, "sha256": h.hexdigest()})
    except BaseException:
        for arquivo in gravadas:
            arquivo.unlink(missing_ok=True)
        for parte in plano:
            (pasta / (parte.nome + ".tmp")).unlink(missing_ok=True)
        raise
    manifesto["sha256"] = geral.hexdigest()
    (pasta / (base + SUFIXO_MANIFESTO)).write_text(
        json.dumps(manifesto, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifesto
