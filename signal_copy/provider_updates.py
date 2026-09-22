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
    UPDATE_TP = "UPDATE_TP"


@dataclass(frozen=True)
class ProviderUpdate:
    kind: UpdateKind
    symbol: str
    price: float | None = None
    tp_index: int | None = None


def _symbol(text: str, fallback: str | None = None) -> str:
    if fallback:
        return fallback.upper().replace("/", "").replace("-", "")
    
    # Check reply symbol marker injected by telegram_listener
    m_reply = re.search(r"\[REPLY_SYMBOL:\s*([A-Z0-9]+)\]", text, re.IGNORECASE)
    if m_reply:
        sym = m_reply.group(1).upper()
        return sym if sym.endswith("USDT") else sym + "USDT"

    ignored = {
        "MOVE", "MOVED", "SET", "CANCEL", "CANCELLED", "CLOSE", "CLOSED",
        "SIGNAL", "STOP", "LOSS", "SL", "BEP", "BE", "TP", "HIT", "NOW", "TO",
        "CRYPTO", "FULL", "PARTIAL", "POSITION", "TRADE", "ENTRY", "TARGET",
        "PROFIT", "SETUP", "DETAIL", "DETAILS", "ALL", "HERE", "IS", "IT",
        "HILANGKAN", "HAPUS", "SEMENTARA", "GESER", "PINDAHKAN", "TUTUP",
        "SEKARANG", "PASANG", "BATAL", "BATALKAN", "NEW", "ONE", "VIP", "FREE",
        "JOIN", "RESULTS", "RESULT", "LIVE", "OPENING", "SMALL", "FEW", "MINUTES",
    }
    # 1. Matches with # or $: e.g. #BTC, #1000PEPE, $SOL
    m_tag = re.search(r"(?:#|\$)([A-Z0-9]{2,12})(?:/USDT|USDT)?\b", text.upper())
    if m_tag and m_tag.group(1) not in ignored:
        base = m_tag.group(1)
        return base if base.endswith("USDT") else base + "USDT"

    # 2. Matches explicit USDT pair: e.g. BTC/USDT, 1000PEPEUSDT
    m_usdt = re.search(r"\b([A-Z0-9]{2,12})(?:/USDT|USDT)\b", text.upper())
    if m_usdt and m_usdt.group(1) not in ignored:
        base = m_usdt.group(1)
        return base if base.endswith("USDT") else base + "USDT"

    # 3. Matches symbol at start of line or after command: e.g. "1000pepe new tp", "Close alch"
    m_start = re.search(r"^(?:(?:CLOSE|TUTUP)\s+)?([A-Z0-9]{2,12})\b", text.upper().strip())
    if m_start and m_start.group(1) not in ignored:
        base = m_start.group(1)
        return base if base.endswith("USDT") else base + "USDT"

    return ""


def parse_provider_update(text: str, channel_id: int | None = None, reply_symbol: str | None = None) -> ProviderUpdate | None:
    if channel_id is not None and channel_id != PILOT_CHANNEL:
        return None
    normalized = " ".join((text or "").split())
    upper = normalized.upper()
    symbol = _symbol(upper, reply_symbol)
    if not symbol:
        return None

    # 1. Remove / Suspend SL temporarily ("hilangkan sl", "remove sl", "renmove sl", "take off sl", "hapus sl")
    if re.search(r"(?:HILANGKAN|REMOVE|RENMOVE|CANCEL|DELETE|TAKE\s*OFF|HAPUS)\s+(?:THE\s+)?SL|SL\s+(?:DIHILANGKAN|REMOVED|CANCELLED|SEMENTARA|OFF|DIHAPUS)", upper):
        return ProviderUpdate(UpdateKind.REMOVE_SL, symbol)

    # 2. Move SL to BE ("move sl to be", "sl be", "geser sl ke be", "bep", "sl to entry")
    if re.search(r"(?:MOVE|MOVED|SET|GESER|PINDAHKAN|PASANG)\s+(?:THE\s+)?SL\s+(?:TO|KE\s+)?(?:BE|BEP|BREAKEVEN|BREAK[ -]?EVEN|ENTRY)|SL\s*(?:MOVED\s+TO|TO|KE)?\s*(?:BE|BEP|BREAKEVEN|ENTRY)", upper):
        return ProviderUpdate(UpdateKind.MOVE_SL_BE, symbol)

    # 3. Move SL to explicit price ("SL: 0.045", "sl move to 0.045", "geser sl ke 0.045", "move sl to 0.045")
    m = re.search(r"(?:(?:MOVE|MOVED|SET|GESER|PINDAHKAN|NEW)\s+)?SL\s*(?:MOVE|MOVED|SET|GESER|PINDAHKAN)?\s*(?:TO|AT|KE|:)?\s*[:@]??\s*([0-9]+(?:\.[0-9]+)?)", upper)
    if m:
        return ProviderUpdate(UpdateKind.MOVE_SL_PRICE, symbol, float(m.group(1)))

    # 4. Cancel pending ("cancel", "batalkan", "batal")
    if re.search(r"\b(?:CANCEL(?:LED)?|BATAL(?:KAN)?)\b", upper):
        return ProviderUpdate(UpdateKind.CANCEL, symbol)

    # 5. Close position ("close", "close now", "close alch", "tutup sekarang", "exit now")
    if re.search(r"\b(?:CLOSE|TUTUP|EXIT|OUT)\b", upper):
        return ProviderUpdate(UpdateKind.CLOSE, symbol)

    # 6. TP Hit
    m = re.search(r"\bTP\s*([1-9][0-9]*)\s+(?:HIT|REACHED|TERCAPAI)\b", upper)
    if m:
        return ProviderUpdate(UpdateKind.TP_HIT, symbol, tp_index=int(m.group(1)))

    # 7. Update TP ("1000pepe new tp 1 0.005100", "Kernel 2nd tp 0.06400", "Kernel tp 3 0.07000", "new tp: 0.00445")
    m = re.search(r"(?:(?:NEW|UPDATE|ADJUST|SET)\s+)?(?:([1-9])(?:ND|RD|TH|ST)?\s+TP|TP\s*([1-9])?)\s*(?:TO|AT|:)?\s*[:@]??\s*([0-9]+(?:\.[0-9]+)?)", upper)
    if m:
        idx_str = m.group(1) or m.group(2)
        tp_idx = int(idx_str) if idx_str else 1
        price = float(m.group(3))
        return ProviderUpdate(UpdateKind.UPDATE_TP, symbol, price=price, tp_index=tp_idx)

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
