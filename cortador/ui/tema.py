"""Cores e fontes do app (identidade NBR Play: tokens do brand kit, tema escuro)."""
from __future__ import annotations

DESTAQUE = "#FFFFFF"
DESTAQUE_HOVER = "#D9D9D9"
SOBRE_DESTAQUE = "#0B0C0F"
FUNDO = "#0B0C0F"
PAINEL = "#16181D"
PAINEL_2 = "#22252C"
BORDA = "#555D6B"
TEXTO = "#F5F5F7"
TEXTO_SUAVE = "#B7BBC4"
SUCESSO = "#70D6A0"
ALERTA = "#F0C56B"
ERRO = "#FF929C"
ERRO_FUNDO = "#3a1f22"
ERRO_FUNDO_HOVER = "#53292d"
OK = SUCESSO

# Cores do estado de cada item da fila.
COR_ESTADO = {
    "novo": TEXTO_SUAVE,
    "pronto": SUCESSO,
    "enviando": ALERTA,
    "concluido": SUCESSO,
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
