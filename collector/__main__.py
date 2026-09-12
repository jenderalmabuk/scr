"""Collector entrypoint: `python -m collector` dispatches on COLLECTOR_MODE."""
from __future__ import annotations

import asyncio
import os
import sys


def main() -> None:
    mode = os.getenv("COLLECTOR_MODE", "binance").lower()
    if mode == "binance":
        from collector.binance import run
    elif mode == "bybit":
        from collector.bybit import run
    elif mode == "oi_rollup":
        from collector.oi_rollup import run
    else:
        print(f"[collector] unknown COLLECTOR_MODE={mode!r} (binance|bybit|oi_rollup)")
        sys.exit(1)
    asyncio.run(run())


if __name__ == "__main__":
    main()
