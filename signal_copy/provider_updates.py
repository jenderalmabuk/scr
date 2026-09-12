"""Conservative English provider-update parser, pilot channel only."""
from dataclasses import dataclass
from enum import Enum
import re

PILOT_CHANNEL = -1001652601224


class UpdateKind(str, Enum):
    MOVE_SL_BE = "MOVE_SL_BE"
    MOVE_SL_PRICE = "MOVE_SL_PRICE"
    CANCEL = "CANCEL"
    CLOSE = "CLOSE"
    TP_HIT = "TP_HIT"


@dataclass(frozen=True)
class ProviderUpdate:
    kind: UpdateKind
    symbol: str
    price: float | None = None
    tp_index: int | None = None


def _symbol(text: str, fallback: str | None) -> str:
    if fallback:
        return fallback.upper().replace("/", "").replace("-", "")
    ignored = {
        "MOVE", "MOVED", "SET", "CANCEL", "CANCELLED", "CLOSE", "CLOSED",
        "SIGNAL", "STOP", "LOSS", "SL", "BEP", "BE", "TP", "HIT", "NOW", "TO",
        "CRYPTO", "FULL", "PARTIAL", "POSITION", "TRADE", "ENTRY", "TARGET",
        "PROFIT", "SETUP", "DETAIL", "DETAILS", "ALL", "HERE", "IS", "IT",
    }
    for base in re.findall(r"(?:#|\b)([A-Z0-9]{2,12})(?:/USDT|USDT)?\b", text.upper()):
        if base not in ignored:
            return base if base.endswith("USDT") else base + "USDT"
    return ""


def parse_provider_update(text: str, channel_id: int | None, reply_symbol: str | None = None) -> ProviderUpdate | None:
    if channel_id != PILOT_CHANNEL:
        return None
    normalized = " ".join((text or "").split())
    upper = normalized.upper()
    symbol = _symbol(upper, reply_symbol)
    if not symbol:
        return None
    if re.search(r"\b(?:MOVE|MOVED|SET)\s+(?:THE\s+)?SL\s+(?:TO\s+)?(?:BE|BEP|BREAKEVEN|BREAK[ -]?EVEN)\b|\bSL\s+(?:MOVED\s+)?TO\s+(?:BE|BEP|BREAKEVEN)\b", upper):
        return ProviderUpdate(UpdateKind.MOVE_SL_BE, symbol)
    m = re.search(r"\bSL\s+(?:MOVE|MOVED|SET)?\s*(?:TO|AT)\s*[:@]?\s*([0-9]+(?:\.[0-9]+)?)", upper)
    if m:
        return ProviderUpdate(UpdateKind.MOVE_SL_PRICE, symbol, float(m.group(1)))
    if re.search(r"\bCANCEL(?:LED)?\b", upper):
        return ProviderUpdate(UpdateKind.CANCEL, symbol)
    if re.search(r"\bCLOSE\s+(?:NOW\s+)?", upper):
        return ProviderUpdate(UpdateKind.CLOSE, symbol)
    m = re.search(r"\bTP\s*([1-9][0-9]*)\s+(?:HIT|REACHED)\b", upper)
    if m:
        return ProviderUpdate(UpdateKind.TP_HIT, symbol, tp_index=int(m.group(1)))
    return None


def safer_stop(side: str, old_sl: float, new_sl: float, entry: float) -> bool:
    if min(old_sl, new_sl, entry) <= 0:
        return False
    return old_sl <= new_sl <= entry if side.upper() == "LONG" else entry <= new_sl <= old_sl
