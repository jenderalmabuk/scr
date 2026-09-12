"""Universe loading for collectors: file first, DB fallback."""
from __future__ import annotations

import os
from pathlib import Path


def load_universe() -> list[str]:
    """Read symbols from UNIVERSE_FILE (default universe.txt), one per line."""
    path = Path(os.getenv("UNIVERSE_FILE", "universe.txt"))
    if not path.exists():
        return []
    symbols = []
    for line in path.read_text().split():
        sym = line.strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols
