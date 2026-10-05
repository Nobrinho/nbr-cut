"""Cores e fontes do app (identidade do Nbr PLAY: tema escuro com o verde da marca)."""
from __future__ import annotations

VERDE = "#2CE84B"
VERDE_ESCURO = "#1FB838"
FUNDO = "#101014"
PAINEL = "#17181d"
PAINEL_2 = "#1f2027"
BORDA = "#2c2e37"
TEXTO = "#f2f3f5"
TEXTO_SUAVE = "#9aa0ab"
ALERTA = "#ffb020"
ERRO = "#ff5d5d"
OK = VERDE

# Cores do estado de cada item da fila.
COR_ESTADO = {
    "novo": TEXTO_SUAVE,
    "pronto": VERDE,
    "enviando": ALERTA,
    "concluido": VERDE,
    "erro": ERRO,
    "cancelado": TEXTO_SUAVE,
}

ROTULO_ESTADO = {
    "novo": "escolha o filme",
    "pronto": "pronto para postar",
    "enviando": "enviando…",
    "concluido": "postado",
    "erro": "erro — retome",
    "cancelado": "cancelado",
}

ESPACO = 12
RAIO = 10
