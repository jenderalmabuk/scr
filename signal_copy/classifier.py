"""
Message classifier for the signal-copy ingest front-end.

Every incoming Telegram/Discord message is classified BEFORE deep parsing so we
can route it to the right processor and surface a clear, detailed read of what
the bot understood:

    TRADE_SIGNAL  -> structured trade call (pair/side/entry/...) -> trade pipeline
    WHALE_ACCUM   -> accumulation / on-chain / smart-money narrative -> accumulation route
    NEWS          -> news / listing / announcement -> (informational)
    NOISE         -> cancels, results, chatter, quoted previews -> ignore
    UNKNOWN       -> nothing actionable recognized

The classifier is deliberately heuristic and side-effect free so it is trivial
to unit-test. The authoritative TRADE_SIGNAL decision is still made by
parse_signal(); this classifier provides the routing label + a human-readable
reason list for the read report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class MessageType(str, Enum):
    TRADE_SIGNAL = "TRADE_SIGNAL"
    WHALE_ACCUM = "WHALE_ACCUM"
    NEWS = "NEWS"
    NOISE = "NOISE"
    UNKNOWN = "UNKNOWN"


@dataclass
class ClassifyResult:
    type: MessageType
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.type.value


# --- Patterns ---------------------------------------------------------------

# Cancels / results / closes / quoted previews -> not a fresh actionable message.
_NOISE_RE = re.compile(
    r"\b(cancel|cancell?ed|canceled|closed|close\s+position|stop(?:ped)?\s*out|"
    r"tp\s*hit|sl\s*hit|full\s*tp|all\s*tp|invalidat|batal|tutup\s*posisi|"
    r"book(?:ed)?\s*profit|target\s*hit|all\s*targets?\s*(?:hit|done)|"
    r"congr|terbang|gain\s*\+?\d|profit\s*\+?\d)\b",
    re.IGNORECASE,
)
_QUOTE_PREVIEW_RE = re.compile(r'\.\.\.\s*["”]|…\s*["”]')

# Marketing / promo / recruitment posts (guaranteed profits, join-now funnels,
# "N spots left", invite links). These often ship with a mock chart image, so
# without this gate the vision path mistakes the picture for a real setup.
_PROMO_RE = re.compile(
    r"\b(guaranteed|guarantee)\b|"
    r"\bprofits?\s+daily\b|\bdaily\s+profits?\b|"
    r"\bjoin\s+(now|us|here|today|vip)\b|"
    r"\blimited\s+(time|spots?|seats?)\b|"
    r"\bhurry\b|"
    r"\bonly\s+\d+\s+(spots?|people|seats?|places?)\b|"
    r"\b\d+\s+spots?\b|"
    r"\bpremium\s+signals?\b|\bprivate\s+signals?\b|"
    r"t\.me/\+",
    re.IGNORECASE,
)


def looks_like_promo(text: str) -> bool:
    """True if the message reads like a marketing/recruitment post rather than
    a trade call. Used to gate the vision path so mock-chart promos don't get
    turned into fake signals."""
    return bool(text and _PROMO_RE.search(text))

# Structured trade-call cues (used as a hint; parse_signal is authoritative).
_SIDE_RE = re.compile(r"\b(long|short|buy|sell)\b", re.IGNORECASE)
_PAIR_RE = re.compile(r"\$[A-Za-z]{2,15}\b(?<!\d)(?!\d+[MKBT]\b)|\b[A-Z0-9]{2,15}\s*[\/\-]?\s*(?:USDT|USDC|BUSD|USD)\b")
_TRADE_FIELD_RE = re.compile(
    r"\b(entry|entry\s*zone|take\s*profit|targets?|tp\d?|stop\s*loss|sl|leverage|lev)\b",
    re.IGNORECASE,
)

# Whale / accumulation / on-chain / smart-money narrative.
_ACCUM_RE = re.compile(
    r"\b(accumulat\w*|akumulasi|diakumulasi|diborong|dikoleksi|"
    r"whale[s]?|smart\s*money|big\s*(?:buyer|wallet|player)|dumped|dumping|bought|buying\s+(?:more|up|again|the\s*dip)|sold|selling\s*(?:off|pressure|spree)|open(?:ed)?\s+(?:a\s+)?\d+[xX]\s+(?:long|short)\s+on|wallet\s*(?:baru|linked|address|0x)?|menarik|withdr\w*|transfer(?:ed|ring)?\s*(?:from|to)|"
    r"on[\-\s]?chain|netflow|net\s*flow|inflow|outflow|exchange\s*flow|"
    r"absorb\w*|spot\s*inflow|wallet[s]?\s*(?:buy|accumulat)|"
    r"institution\w*|holder[s]?\s*(?:naik|increas|add))\b",
    re.IGNORECASE,
)

# News / announcements.
_NEWS_RE = re.compile(
    r"\b(listing|listed|delisting|partnership|mainnet|testnet\s*launch|"
    r"airdrop|launch(?:es|ed|ing)?|upgrade|hard\s*fork|"
    r"\betf\b|\bsec\b|regulat\w*|lawsuit|hack(?:ed|ing)?|exploit|breach|"
    r"breaking|announce\w*|pengumuman|berita|rumor|rumour)\b",
    re.IGNORECASE,
)


_EMOJI_NOISE_RE = re.compile(r"[⚡️🔼📈⛔️🔥⚠️🚨✅❌🟢🔴🧪]+")


def _classifier_text(text: str) -> str:
    # Keep raw parser input intact; only classification gets a cleaned view.
    return _EMOJI_NOISE_RE.sub(" ", text or "")


def classify_message(text: str) -> ClassifyResult:
    """Classify a raw message into a routing label with reasons + confidence."""
    if not text or not text.strip():
        return ClassifyResult(MessageType.NOISE, 1.0, ["empty"])

    text = _classifier_text(text)
    reasons: List[str] = []

    # 1) Noise / results / quoted previews first.
    if _QUOTE_PREVIEW_RE.search(text):
        return ClassifyResult(MessageType.NOISE, 0.9, ["quoted_preview"])
    noise_hit = _NOISE_RE.search(text)

    # 2) Structured trade-call cues.
    has_side = bool(_SIDE_RE.search(text))
    has_pair = bool(_PAIR_RE.search(text))
    has_field = bool(_TRADE_FIELD_RE.search(text))
    trade_score = sum((has_side, has_pair, has_field))

    # A cancel/result that still mentions a pair/side is NOISE (a past trade
    # update), not a fresh signal.
    if noise_hit:
        return ClassifyResult(MessageType.NOISE, 0.85, [f"noise:{noise_hit.group(0).lower()}"])

    # 3) Whale / accumulation narrative (check BEFORE trade signal)
    am = _ACCUM_RE.search(text)
    if am:
        return ClassifyResult(MessageType.WHALE_ACCUM, 0.7, [f"accum:{am.group(0).lower()}"])

    if trade_score >= 2 and (has_side and (has_pair or has_field)):
        reasons.append("structured:" + "+".join(
            n for n, ok in (("side", has_side), ("pair", has_pair), ("field", has_field)) if ok))
        return ClassifyResult(MessageType.TRADE_SIGNAL, min(0.5 + 0.2 * trade_score, 0.99), reasons)

    # 4) News.
    nm = _NEWS_RE.search(text)
    if nm:
        return ClassifyResult(MessageType.NEWS, 0.6, [f"news:{nm.group(0).lower()}"])

    # 5) Weak trade cue (a lone side/pair) -> still try as signal, low confidence.
    if has_side and (has_pair or has_field):
        return ClassifyResult(MessageType.TRADE_SIGNAL, 0.45, ["weak_trade_cue"])

    return ClassifyResult(MessageType.UNKNOWN, 0.3, ["no_actionable_cue"])
