"""
Parser for free-text trade-call signals from Telegram groups / Discord.

Handles the common "crypto signal" layout, tolerant to emojis, bold markdown,
arrows, and varied labels. Returns a ParsedSignal or None if the text does not
look like an actionable trade call.

Design goals:
- Be strict enough to avoid false positives (chatter, news, ads).
- Be lenient on formatting (emoji, **bold**, →, -, :, multiple TPs).
- Never raise on bad input; return None instead.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

from .signal_schema import ParsedSignal, SignalSide, SignalSource

# --- known quote assets, longest first so matching is greedy-correct ---
_QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")

# Pair like "ZEC/USDT", "ZECUSDT", "BTC-USDT", "$ZEC"
_PAIR_RE = re.compile(
    r"(?:pair|coin|symbol|ticker)\s*[:\-]?\s*"
    r"[*_`]*\$?([A-Z0-9]{2,15})\s*[\/\-]?\s*(USDT|USDC|BUSD|USD)?[*_`]*",
    re.IGNORECASE,
)

# Fallback pair detection anywhere: "ZEC/USDT", "$ZEC", or bare "ZECUSDT"
_PAIR_INLINE_RE = re.compile(
    r"[#\$]?\b([A-Z0-9]{1,15})\s*[\/\-]\s*(USDT|USDC|BUSD|USD)\b",
    re.IGNORECASE,
)
# Concatenated form: "ZECUSDT", "BTCUSDT PERPETUAL"
_PAIR_CONCAT_RE = re.compile(
    r"\$?\b([A-Z0-9]{2,12}?)(USDT|USDC|BUSD)\b",
    re.IGNORECASE,
)
# Cashtag form: "$ZETA", "$BEAT", "$MOCA" (no quote suffix) -> append USDT
_PAIR_CASHTAG_RE = re.compile(r"[#$]([A-Za-z][A-Za-z0-9]{1,14})\b")

_SIDE_RE = re.compile(
    r"(?:position|side|direction|type|signal|setup(?:\s*utama)?)\s*[:\-]?\s*[*_`(]*\s*"
    r"(LONG|SHORT|BUY|SELL)\b",
    re.IGNORECASE,
)
_SIDE_INLINE_RE = re.compile(r"\b(LONG|SHORT)\b", re.IGNORECASE)

_LEV_X = r"[xX×]"
_LEVERAGE_RE = re.compile(
    r"(?:leverage|lev|cross|isolated)\s*[:\-]?\s*[*_`]*\s*(?:cross\s*)?(\d{1,3}(?:\.\d+)?)\s*" + _LEV_X
    + r"(?:\s*[_/\-]\s*(\d{1,3}(?:\.\d+)?)\s*" + _LEV_X + r")?",
    re.IGNORECASE,
)
_LEVERAGE_INLINE_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*" + _LEV_X + r"\b")
_LEVERAGE_RANGE_RE = re.compile(
    r"\b(\d{1,3}(?:\.\d+)?)\s*" + _LEV_X
    + r"(?:\s*(?:[_/\-~]|to|till)\s*(\d{1,3}(?:\.\d+)?)\s*" + _LEV_X + r")?",
    re.IGNORECASE,
)

# Entry zone: "Entry: 358 - 350", "Entry Zone:\n• 358-350", "ENTRY AREA (LONG) 376.0 - 378.5"
# The filler between the label and the price must NOT cross a competing label
# (Stop Loss / Target / TP / Take Profit / Risk) — otherwise "Entry Long Now
# Stop Loss : 0.4154" would grab the SL value as the entry.
_ENTRY_FILLER = (r"(?:(?!\b(?:stop\s*loss|stoploss|stop|sl|target|targets|tp|"
                 r"take\s*profit|take|profit|risk|lev|leverage)\b)[^\d]){0,40}?")
_ENTRY_RE = re.compile(
    r"(?:entr(?:y|ies)(?:\s*zone)?(?:\s*area)?|entry\s*price|buy\s*zone|enter)\s*[:\-@]?"
    + _ENTRY_FILLER + r"\$?([\d.,]+)\s*(?:[-–—~]+|\bto\b)\s*\$?([\d.,]+)"
    r"|(?:entr(?:y|ies)(?:\s*zone)?(?:\s*area)?|entry\s*price|buy\s*zone|enter)\s*[:\-@]?"
    + _ENTRY_FILLER + r"\$?([\d.,]+)",
    re.IGNORECASE,
)
# Enumerated entry zone, common in Telegram channels:
# "Entry Price: 1) 1.963 2) 1.904".
_ENTRY_ENUM_RE = re.compile(
    r"(?:entry(?:\s*zone)?(?:\s*area)?|entry\s*price|buy\s*zone|enter)\s*[:\-@]?"
    + _ENTRY_FILLER + r"(?:\d{1,2}\s*[\).]\s*)\$?([\d.,]+)\s+"
    r"(?:\d{1,2}\s*[\).]\s*)\$?([\d.,]+)",
    re.IGNORECASE,
)
# Hybrid market/zone form: "Entry: now/388-376".
_ENTRY_NOW_ZONE_RE = re.compile(
    r"\bentry\b[^\n\d]{0,24}?\b(?:now|market|cmp)\b\s*[\/|,]?\s*"
    r"\$?([\d.,]+)\s*(?:[-–—~]+|\bto\b)\s*\$?([\d.,]+)",
    re.IGNORECASE,
)
# "Entry Targets: 0.07100" — price on same line; the word "Targets" here labels
# the entry itself, so the generic _ENTRY_RE (which rejects a following "target")
# skips it. Handle explicitly.
# Price must be on the SAME line as the label ([^\S\n] = horizontal ws only),
# otherwise a numbered-list "Entry Targets:\n1) 0.0019" would grab the index "1".
_ENTRY_TARGETS_INLINE_RE = re.compile(
    r"entry[^\S\n]*targets?[^\S\n]*[:\-@]?[^\S\n]*\$?(\d[\d.,]*)",
    re.IGNORECASE,
)
# Numbered-list entry format like "Entry Targets:\n1) 2.24\n2) 2.30"
# This format has no prices on the same line as the label.
_ENTRY_LIST_LABEL_RE = re.compile(
    r"(?:entry\s*targets?|entry\s*zone|entry\s*points?|entry\s*price|entries)\s*:\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ENTRY_LIST_NUMBER_RE = re.compile(r"\b\d{1,2}\s*[.)]\s*(?:[➡️🔼⛔️⚠️]*\s*)\$?(\d+(?:\.\d+)?)", re.IGNORECASE)
# Market entry with NO explicit price: "Entry Long now", "Entry market", "CMP".
_ENTRY_MARKET_RE = re.compile(
    r"\bentry\b[^\n\d]{0,18}?\b(now|market|market\s*price|cmp|sekarang)\b",
    re.IGNORECASE,
)

_SL_RE = re.compile(
    r"(?:stop\s*loss|stoploss|stop\s*target|stop|sl)\s*[:\\-–—]*\s*[\-–—]?\s*[*_`]*\$?([\d.,]+)(?!\s*[.)])",
    re.IGNORECASE,
)
# New: numbered-list SL format like "Stop Target:\n1) 2.37"
_SL_LIST_LABEL_RE = re.compile(
    r"(?:stop\s*target|stop\s*loss|sl)\s*:\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SL_LIST_NUMBER_RE = re.compile(r"\b\d{1,2}\s*[.)]\s*(?:[➡️🔼⛔️⚠️]*\s*)\$?(\d+(?:\.\d+)?)", re.IGNORECASE)

# Take profits: capture the whole targets block then extract numbers.
_TP_BLOCK_RE = re.compile(
    r"(?:take\s*profits?|targets?|tps?)\b(.*?)(?:stop\s*loss|stoploss|\bstop\b|\bsl\b|⛔|$)",
    re.IGNORECASE | re.DOTALL,
)
# A line that carries a take-profit value, e.g. "TP1 365", "TP 1 → 365",
# "TP1: 386.0", "Target 2 - 415", "Take Profit 365".
_TP_LINE_RE = re.compile(r"(?:take\s*profits?|targets?|\btp)\s*(\d{1,2})?\b", re.IGNORECASE)
_TP_LIST_LABEL_RE = re.compile(r"(?:take\s*-?\s*profits?|tp)\s*targets?\s*:\s*$", re.IGNORECASE | re.MULTILINE)
# "Take Profits👇" or "Take-Profit Targets👇" header with one price per
# following line, prefixed by keycap-emoji (1️⃣) or "1)". NOT "Entry Targets".
_TP_HEADER_RE = re.compile(r"(?:take|tp)\s*-?\s*profits?\s*(?!targets?)[:\-]?\s*[👇🎯💵]*\s*$", re.IGNORECASE | re.MULTILINE)
# Keycap emoji price line: "1️⃣0.07300" or "2️⃣ 0.0755" or plain "1) 0.073".
# Only a keycap emoji (1️⃣) or "1)" counts as an index — NOT "1." (that is a
# decimal like 45.97, whose fractional digits must not be mistaken for a value).
_TP_KEYCAP_RE = re.compile(r"(?:[\u0031-\u0039][\ufe0f]?[\u20e3]|\b\d{1,2}\s*\))\s*\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_TP_LIST_NUMBER_RE = re.compile(r"\b\d{1,2}\s*[.)]\s*(?:[🔼➡️⛔️⚠️]*\s*)\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\$?([\d][\d.,]*\d|\d)")

# Bare ticker at the start of a line: "ETH", "😀😀 ETH". Last-resort symbol
# fallback used ONLY when the rest of the call is well-formed (entry + a level).
_BARE_TICKER_RE = re.compile(r"^\s*(?:[^\w\s]+\s*)?([A-Z]{2,6})\b", re.MULTILINE)
_BARE_TICKER_STOP = frozenset({
    "LONG", "SHORT", "BUY", "SELL", "TP", "SL", "SETUP", "ENTRY", "TARGET",
    "TARGETS", "STOP", "LOSS", "MARKET", "USDT", "USDC", "PAIR", "COIN", "NAME",
    "POSITION", "LEVERAGE", "CROSS", "PROFIT", "PROFITS", "RISK", "VIP", "FREE",
    "OPEN", "NOW", "PERP", "FUTURES", "FUTURE", "SIGNAL", "TRADE", "THE", "AND",
})

# Strong indicator that text is a trade call (need at least entry+side+pair).
_SIGNAL_HINT_RE = re.compile(
    r"(entry|long|short|target|tp\d|stop\s*loss|leverage)",
    re.IGNORECASE,
)

# Messages that are NOT fresh entries (cancels, closes, quoted previews, results).
_NOISE_RE = re.compile(
    r"\b(cancel|cancell?ed|canceled|closed|close\s+position|stop(?:ped)?\s*out|"
    r"tp\s*hit|sl\s*hit|full\s*tp|all\s*tp|invalidat|batal|tutup\s*posisi|"
    r"book(?:ed)?\s*profit|target\s*hit|all\s*targets?\s*(?:hit|done)|"
    r"congr|terbang)\b",
    re.IGNORECASE,
)
# Truncated quote preview, e.g. '... Target : di ch..."'
_QUOTE_PREVIEW_RE = re.compile(r'\.\.\.\s*["”]|…\s*["”]')

# Entry type: limit vs market.
_LIMIT_RE = re.compile(
    r"\b(entry\s*limit|limit\s*entry|buy\s*limit|sell\s*limit)\b|"
    r"\blimit\b(?=.*\bentry\b)|\bentry\b(?=.*\blimit\b)",
    re.IGNORECASE | re.DOTALL,
)

# Timeframe: "Time frame : 1h", "TF: 30m", "30m", "1H", "4 hour", "15min"
_TIMEFRAME_LABEL_RE = re.compile(
    r"(?:time\s*frame|timeframe|\btf)\s*[:\-]?\s*(\d{1,2})\s*(m|min|mins|minute|h|hr|hour|d|day|w|week)\b",
    re.IGNORECASE,
)
_TIMEFRAME_INLINE_RE = re.compile(
    r"\b(\d{1,2})\s*(m|min|h|hr|d|w)\b",
    re.IGNORECASE,
)


def _normalize_tf(num: str, unit: str) -> Optional[str]:
    try:
        n = int(num)
    except (TypeError, ValueError):
        return None
    u = (unit or "").lower()
    if u.startswith("m"):  # m, min, mins, minute
        return f"{n}m"
    if u.startswith("h"):  # h, hr, hour
        return f"{n}h"
    if u.startswith("d"):
        return f"{n}d"
    if u.startswith("w"):
        return f"{n}w"
    return None


def _to_float(raw: str) -> Optional[float]:
    if raw is None:
        return None
    s = raw.strip().replace(" ", "")
    if not s:
        return None
    # Decimal-first: signal texts usually mean literal prices, not scaled ints.
    # Keep commas only when they clearly act as decimal separators.
    if "," in s:
        if "." in s:
            s = s.replace(",", "")
        elif s.count(",") == 1:
            left, right = s.split(",", 1)
            if left and left != "0" and len(right) == 3:
                s = left + right
            else:
                s = left + "." + right
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


_BINANCE_THOUSAND = {
    "PEPEUSDT": "1000PEPEUSDT",
    "BONKUSDT": "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
    "SHIBUSDT": "1000SHIBUSDT",
    "LUNCUSDT": "1000LUNCUSDT",
    "PEPE": "1000PEPEUSDT",
    "BONK": "1000BONKUSDT",
    "FLOKI": "1000FLOKIUSDT",
    "SHIB": "1000SHIBUSDT",
    "LUNC": "1000LUNCUSDT",
}

def _normalize_symbol(base: str, quote: Optional[str]) -> str:
    base = (base or "").upper().strip().lstrip("$")
    # Clean Bybit .P suffix (e.g. BSBUSDT.P -> BSBUSDT)
    if base.endswith(".P"):
        base = base[:-2]
    quote = (quote or "USDT").upper().strip()
    if not base:
        return ""
    # If base already ends with a quote (e.g. "ZECUSDT"), keep as-is.
    for q in _QUOTES:
        if base.endswith(q) and base != q:
            result = base
            # Normalize Binance thousand-lot pairs (PEPEUSDT → 1000PEPEUSDT)
            result = _BINANCE_THOUSAND.get(result, result)
            return result
    if quote == "PERP" or quote == "USD":
        quote = "USDT"
    result = f"{base}{quote}"
    result = _BINANCE_THOUSAND.get(result, result)
    return result


def _normalize_side(token: str) -> Optional[SignalSide]:
    t = (token or "").upper().strip()
    if t in ("LONG", "BUY"):
        return SignalSide.LONG
    if t in ("SHORT", "SELL"):
        return SignalSide.SHORT
    return None


def _extract_take_profits(text: str) -> List[float]:
    tps: List[float] = []

    # "Take Profits👇" header followed by one price per line, prefixed by a
    # keycap-emoji index (1️⃣ 2️⃣ …) or "1)". Consume until the SL line.
    m_hdr = _TP_HEADER_RE.search(text)
    if m_hdr:
        block = text[m_hdr.end():]
        block = re.split(r"\n\s*(?:stop\s*loss|stoploss|\bsl\b|🛑|⛔)", block, maxsplit=1, flags=re.IGNORECASE)[0]
        for nm in _TP_KEYCAP_RE.finditer(block):
            v = _to_float(nm.group(1))
            if v is not None and v > 0:
                tps.append(v)
        if tps:
            return tps

    # Numbered-list TP format: "Take-Profit Targets:\n1) 0.116\n2) 0.120".
    m_list = _TP_LIST_LABEL_RE.search(text)
    if m_list:
        block = text[m_list.end():]
        block = re.split(r"\n\s*(?:stop\s*target|stop\s*loss|sl|trailing\s*configuration)\s*:\s*", block, maxsplit=1, flags=re.IGNORECASE)[0]
        for nm in _TP_LIST_NUMBER_RE.finditer(block):
            v = _to_float(nm.group(1))
            if v is not None and v > 0:
                tps.append(v)
        if tps:
            return tps

    # Line-based: each TP line contributes its price(s); drop a leading index
    # number that comes from the label itself (e.g. the "1" in "TP1 365").
    for line in text.splitlines():
        m = _TP_LINE_RE.search(line)
        if not m:
            continue
        line_for_numbers = re.sub(r"\b\d{1,2}\s*\)\s*(?=\d)", "", line)
        line_for_numbers = re.sub(
            r"\bTP\s*\d{1,2}\b\s*[:\-–—]*", "", line_for_numbers, flags=re.IGNORECASE
        )
        nums = []
        # Compact TP ladders use commas as delimiters ("TP 3400,3300,3200").
        # Two or more commas are unambiguous here; one comma may be thousands/decimal.
        value_text = line_for_numbers[m.end():]
        compact = [part.strip() for part in value_text.split(",")]
        if len(compact) >= 3 and all(re.fullmatch(r"\$?\d+(?:\.\d+)?", part) for part in compact):
            nums = [_to_float(part) for part in compact]
        else:
            for nm in _NUMBER_RE.finditer(line_for_numbers):
                v = _to_float(nm.group(1))
                if v is not None:
                    nums.append(v)
        if not nums:
            continue
        if m.group(1):  # label had an index like TP1 / TP 1
            idx = _to_float(m.group(1))
            if idx is not None and nums and abs(nums[0] - idx) < 1e-9:
                nums = nums[1:]
        tps.extend(nums)
    if tps:
        return tps

    # Fallback: numbers inside the targets block.
    block = _TP_BLOCK_RE.search(text)
    if block:
        block_text = re.sub(r"\b\d{1,2}\s*\)\s*(?=\d)", "", block.group(1))
        block_text = re.sub(r"\bTP\s*\d{1,2}\b\s*[:\-–—]*", "", block_text, flags=re.IGNORECASE)
        for m in _NUMBER_RE.finditer(block_text):
            v = _to_float(m.group(1))
            if v is not None and v > 0:
                tps.append(v)
    return tps


def _clean_symbol_text(text: str) -> str:
    t = re.sub(r"\b(?:below|above|only|cross|isolated|perp|future|futures)\b", " ", text, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:to|till|till\s+|till\s*$)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:entry\s*price|entry\s*zone|take\s*profits?|targets?|stop\s*loss|stoploss|stop\s*target|risk\s*management|disclaimer)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[\|•·~—–]+", " ", t)
    return t


def parse_signal(
    text: str,
    *,
    source: SignalSource = SignalSource.TELEGRAM,
    source_name: str = "",
    source_chat_id: Optional[int] = None,
) -> Optional[ParsedSignal]:
    """Parse free-text into a ParsedSignal, or None if not a trade call."""
    if not text or not text.strip():
        return None
    text = unicodedata.normalize("NFKC", text)
    # Drop truncated zero tails like "0.000129, 0.000"; they are Telegram/OCR noise, not a second entry.
    text = re.sub(r"(?<=\d),\s*0+(?:\.0+)?(?=\s*(?:\n|$))", "", text)
    if not _SIGNAL_HINT_RE.search(text):
        return None
    # Skip cancels/closes/results and truncated quote previews — not fresh entries.
    if _NOISE_RE.search(text) or _QUOTE_PREVIEW_RE.search(text):
        return None

    # --- side ---
    side: Optional[SignalSide] = None
    m = _SIDE_RE.search(text)
    if m:
        side = _normalize_side(m.group(1))
    if side is None:
        m = _SIDE_INLINE_RE.search(text)
        if m:
            side = _normalize_side(m.group(1))
    # Note: side may still be None here — inferred from SL/TP geometry after
    # those are parsed (some channels post charts/tickers without a direction word).

    clean_text = _clean_symbol_text(text)

    # --- pair ---
    base = quote = None
    m = _PAIR_RE.search(clean_text)
    if m and m.group(1).upper() not in ("LONG", "SHORT", "BUY", "SELL", "TP", "SL", "NAME", "POSITION", "PAIR", "COIN"):
        base, quote = m.group(1), m.group(2)
    if not base:
        m = _PAIR_INLINE_RE.search(text)
        if m:
            base, quote = m.group(1), m.group(2)
    if not base:
        m = _PAIR_CONCAT_RE.search(text)
        if m and m.group(1).upper() not in ("LONG", "SHORT", "BUY", "SELL", "TP", "SL"):
            base, quote = m.group(1), m.group(2)
    if not base:
        m = _PAIR_CASHTAG_RE.search(text)
        if m and m.group(1).upper() not in ("LONG", "SHORT", "BUY", "SELL", "TP", "SL", "RR"):
            base, quote = m.group(1), None
    # Last-resort: bare ticker at the start (e.g. "ETH") — only accepted below,
    # after we confirm the call has an entry AND a stop/target (well-formed).
    bare_candidate = None
    if not base:
        m = _BARE_TICKER_RE.search(text)
        if m and m.group(1).upper() not in _BARE_TICKER_STOP:
            bare_candidate = m.group(1)
    symbol = _normalize_symbol(base, quote) if base else ""
    if not symbol and not bare_candidate:
        return None

    # --- entry zone (or market entry when no explicit price is given) ---
    m = _ENTRY_NOW_ZONE_RE.search(text)
    entry_low = entry_high = None
    if m:
        entry_a = _to_float(m.group(1))
        entry_b = _to_float(m.group(2))
        if entry_a is not None and entry_b is not None:
            entry_low = min(entry_a, entry_b)
            entry_high = max(entry_a, entry_b)
    if entry_low is None:
        m = _ENTRY_ENUM_RE.search(text)
        if m:
            entry_a = _to_float(m.group(1))
            entry_b = _to_float(m.group(2))
            if entry_a is not None and entry_b is not None:
                entry_low = min(entry_a, entry_b)
                entry_high = max(entry_a, entry_b)
    if entry_low is None:
        m = _ENTRY_TARGETS_INLINE_RE.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                entry_low = entry_high = v
    if entry_low is None:
        m = _ENTRY_RE.search(text)
    if m and entry_low is None:
        # Regex has two alternatives: groups (1,2) = range, group (3) = single.
        if m.group(1) is not None:
            entry_a = _to_float(m.group(1))
            entry_b = _to_float(m.group(2)) if m.group(2) else None
        else:
            entry_a = _to_float(m.group(3))
            entry_b = None
        if entry_a is not None:
            entry_low = entry_a if entry_b is None else min(entry_a, entry_b)
            entry_high = entry_a if entry_b is None else max(entry_a, entry_b)
    if entry_low is None:
        # Numbered-list entry format: "Entry Targets:\n1) 2.24\n2) 2.30"
        m = _ENTRY_LIST_LABEL_RE.search(text)
        if m:
            remainder = text[m.end():]
            remainder = re.split(r"\n\s*(?:take\s*-?\s*profit|tp|stop\s*target|stop\s*loss|sl|trailing\s*configuration)\s*:\s*", remainder, maxsplit=1, flags=re.IGNORECASE)[0]
            entries = []
            for nm in _ENTRY_LIST_NUMBER_RE.finditer(remainder):
                v = _to_float(nm.group(1))
                if v is not None:
                    entries.append(v)
                if len(entries) >= 2:
                    break
            if entries:
                entry_low = min(entries)
                entry_high = max(entries)
    if entry_low is None:
        # "Entry now/market" with no price -> market entry; the live price is
        # filled in later by the normalizer (needs metrics). Require a stop or
        # target so it is still a real, actionable call.
        if _ENTRY_MARKET_RE.search(text):
            entry_low = entry_high = 0.0
        else:
            return None

    # --- stop loss ---
    sl = None
    m = re.search(r"(?:stop\s*loss|stoploss)\s*[:\-–—]*\s*\$?([\d.,]+)", text, re.IGNORECASE)
    if m:
        sl = _to_float(m.group(1))
    if sl is None:
        m = _SL_RE.search(text)
        if m:
            candidate = _to_float(m.group(1))
            # Reject bare list indices (1,2,3...), but keep decimal SLs like 1.888.
            raw_sl = m.group(1).strip()
            if candidate is not None and (candidate > 10 or "." in raw_sl or "," in raw_sl):
                sl = candidate
    # Numbered-list SL format: "Stop Target:\n1) 2.37"
    if sl is None and _SL_LIST_LABEL_RE.search(text):
        m = _SL_LIST_LABEL_RE.search(text)
        if m:
            remainder = text[m.end():]
            for nm in _SL_LIST_NUMBER_RE.finditer(remainder):
                v = _to_float(nm.group(1))
                if v is not None:
                    sl = v
                    break

    # --- take profits ---
    take_profits = _extract_take_profits(text)
    # Drop implausible values (e.g. leftover TP indices like 1,2,3 or junk):
    # a real target sits within an order of magnitude of the entry zone.
    if take_profits and entry_high > 0:
        lo_bound = entry_high * 0.1
        hi_bound = entry_high * 10.0
        take_profits = [t for t in take_profits if lo_bound <= t <= hi_bound]

    # --- adopt bare ticker only if the call is well-formed ---
    # Requires a real entry price plus a stop or at least one target, so a stray
    # capitalized word can't masquerade as a signal.
    if not symbol and bare_candidate:
        if entry_high and entry_high > 0 and (sl is not None or take_profits):
            symbol = _normalize_symbol(bare_candidate, None)
    if not symbol:
        return None

    # --- side inference (only when the channel omitted a direction word) ---
    # Infer from geometry: entry vs stop-loss (primary) or entry vs targets.
    # SL below entry => LONG; SL above entry => SHORT.
    if side is None:
        ref = entry_high if entry_high else (entry_low or 0.0)
        if ref > 0 and sl is not None:
            side = SignalSide.LONG if sl < ref else SignalSide.SHORT
        elif ref > 0 and take_profits:
            med = sorted(take_profits)[len(take_profits) // 2]
            side = SignalSide.LONG if med > ref else SignalSide.SHORT
    # Still no side => not enough evidence for a real trade call.
    if side is None:
        return None

    # --- leverage ---
    leverage = None
    m = _LEVERAGE_RANGE_RE.search(text)
    if m:
        a = _to_float(m.group(1))
        b = _to_float(m.group(2)) if m.group(2) else None
        if a is not None:
            leverage = max(a, b) if b is not None else a
    if leverage is None:
        m = _LEVERAGE_RE.search(text)
        if m:
            leverage = _to_float(m.group(1))
    if leverage is None:
        m = _LEVERAGE_INLINE_RE.search(text)
        if m:
            leverage = _to_float(m.group(1))

    # A line like "Leverage: Cross (50X)" should not become 0 when extra text is present.

    # --- entry type (limit vs market) ---
    entry_type = "limit" if _LIMIT_RE.search(text) else "market"

    # --- timeframe ---
    timeframe = None
    m = _TIMEFRAME_LABEL_RE.search(text)
    if m:
        timeframe = _normalize_tf(m.group(1), m.group(2))
    if timeframe is None:
        m = _TIMEFRAME_INLINE_RE.search(text)
        if m:
            timeframe = _normalize_tf(m.group(1), m.group(2))

    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=sl,
        take_profits=take_profits,
        leverage=leverage,
        entry_type=entry_type,
        timeframe=timeframe,
        source=source,
        source_name=source_name,
        source_chat_id=source_chat_id,
        raw_text=text[:2000],
    )
