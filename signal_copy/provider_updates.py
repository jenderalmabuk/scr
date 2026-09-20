"""Bilingual (Indonesian + English) provider-update parser, pilot channel only."""
from dataclasses import dataclass
from enum import Enum
import re

PILOT_CHANNEL = -1001652601224


class UpdateKind(str, Enum):
    MOVE_SL_BE = "MOVE_SL_BE"
    MOVE_SL_PRICE = "MOVE_SL_PRICE"
    REMOVE_SL = "REMOVE_SL"
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
        "HILANGKAN", "HAPUS", "SEMENTARA", "GESER", "PINDAHKAN", "TUTUP",
        "SEKARANG", "PASANG", "BATAL", "BATALKAN",
    }
    for base in re.findall(r"(?:#|)([A-Z0-9]{2,12})(?:/USDT|USDT)?", text.upper()):
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

    # 1. Remove / Suspend SL temporarily ("hilangkan sl", "remove sl", "take off sl", "hapus sl")
    if re.search(r"(?:HILANGKAN|REMOVE|CANCEL|DELETE|TAKE\s*OFF|HAPUS)\s+(?:THE\s+)?SL|SL\s+(?:DIHILANGKAN|REMOVED|CANCELLED|SEMENTARA|OFF|DIHAPUS)", upper):
        return ProviderUpdate(UpdateKind.REMOVE_SL, symbol)

    # 2. Move SL to BE ("move sl to be", "sl be", "geser sl ke be", "bep")
    if re.search(r"(?:MOVE|MOVED|SET|GESER|PINDAHKAN|PASANG)\s+(?:THE\s+)?SL\s+(?:TO|KE\s+)?(?:BE|BEP|BREAKEVEN|BREAK[ -]?EVEN)|SL\s+(?:MOVED\s+TO|KE)?\s*(?:BE|BEP|BREAKEVEN)", upper):
        return ProviderUpdate(UpdateKind.MOVE_SL_BE, symbol)

    # 3. Move SL to explicit price ("SL: 0.045", "sl move to 0.045", "geser sl ke 0.045")
    m = re.search(r"SL\s*(?:MOVE|MOVED|SET|GESER|PINDAHKAN)?\s*(?:TO|AT|KE|:)?\s*[:@]?\s*([0-9]+(?:\.[0-9]+)?)", upper)
    if m:
        return ProviderUpdate(UpdateKind.MOVE_SL_PRICE, symbol, float(m.group(1)))

    # 4. Cancel pending ("cancel", "batalkan", "batal")
    if re.search(r"(?:CANCEL(?:LED)?|BATAL(?:KAN)?)", upper):
        return ProviderUpdate(UpdateKind.CANCEL, symbol)

    # 5. Close position ("close", "close now", "tutup sekarang", "exit now")
    if re.search(r"(?:CLOSE|TUTUP|EXIT|OUT)\s*(?:NOW|SEKARANG)?", upper):
        return ProviderUpdate(UpdateKind.CLOSE, symbol)

    # 6. TP Hit
    m = re.search(r"TP\s*([1-9][0-9]*)\s+(?:HIT|REACHED|TERCAPAI)", upper)
    if m:
        return ProviderUpdate(UpdateKind.TP_HIT, symbol, tp_index=int(m.group(1)))

    return None


def safer_stop(side: str, old_sl: float, new_sl: float, entry: float) -> bool:
    """Ensure the new stop does not widen risk beyond old_sl.
    Allows tightening stop loss, moving to breakeven, and trailing into profit."""
    if new_sl <= 0.0:  # Removal / suspension
        return True
    if old_sl <= 0.0:
        return True
    side = str(side or "LONG").upper()
    if side == "LONG":
        return new_sl >= old_sl
    else:
        return new_sl <= old_sl
