"""`python -m cortador` — abre o Nbr Cortador."""
from __future__ import annotations

import sys


def principal() -> int:
    from cortador import config
    from cortador.ui.app import Aplicativo

    cfg = config.carregar()

    app = Aplicativo(cfg)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
