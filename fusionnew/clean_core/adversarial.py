"""
Adversarial entry check — bull vs bear debate before committing a trade.
Inspired by TradingAgents multi-agent architecture.

Usage:
    from clean_core.adversarial import bull_bear_check
    ok, reason = bull_bear_check(symbol, setup_dict)
    if not ok:
        skip entry — bear argument wins
"""
import json, os, re, time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Tuple

# Per-call LLM timeout (was 15s; sequential bull+bear+judge could stall a cycle)
LLM_TIMEOUT_SEC = float(os.getenv("ADV_LLM_TIMEOUT_SEC", "8"))

# LLM config — use same provider as main config or env
LLM_API_KEY = os.getenv("NINE_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("CUSTOM_API_KEY") or ""
LLM_MODEL_PRIMARY = os.getenv("ADVERSARIAL_MODEL", "gc/gemini-2.5-pro")
LLM_MODEL_FALLBACK = os.getenv("ADVERSARIAL_MODEL_FALLBACK", "gc/gemini-2.5-pro")
LLM_BASE = os.getenv("NINE_ROUTER_BASE") or os.getenv("OPENROUTER_BASE") or "http://127.0.0.1:20128/v1"

# Track which model is currently active (starts with primary, falls back on error)
_current_model = LLM_MODEL_PRIMARY
_fallback_active = False

BULL_PROMPT = """You are a bull analyst. Given this trading setup, argue why this is a GOOD entry.
Focus on: technical confluence, momentum, volume, pattern completion, risk/reward.
Setup:
{symbol} {side} on {tier}
Entry: {entry} | SL: {sl} | TP: {tp} | RR: 1:{rr}
Imbalance: {imb_side} | Complete: {t_complete}
Market data (deterministic validation): {data_block}
Ground your case in the market data above. Keep response under 90 words. Be specific about what confirms this trade."""

BEAR_PROMPT = """You are a bear analyst. Given this trading setup, argue why this is a BAD entry.
Focus on: potential fakeout, resistance, low conviction, conflicting signals, bad timing.
Setup:
{symbol} {side} on {tier}
Entry: {entry} | SL: {sl} | TP: {tp} | RR: 1:{rr}
Imbalance: {imb_side} | Complete: {t_complete}
Market data (deterministic validation): {data_block}
Ground your case in the market data above. Keep response under 90 words. Be specific about what invalidates this trade."""

JUDGE_PROMPT = """You are a trade judge. Two analysts debated this setup:

BULL: {bull_arg}
BEAR: {bear_arg}

Market data (deterministic validation): {data_block}

Weigh both arguments against the data. Decide: should we take this trade?
Reply in exactly this format:
YES - one short reason
or
NO - one short reason
Keep the reason specific to the market data."""


def _call_llm(prompt: str) -> str:
    """Call LLM via OpenRouter-compatible API with fallback."""
    global _current_model, _fallback_active
    import requests
    if not LLM_API_KEY:
        return "NO_API_KEY"
    # Try primary first, then fallback on any error
    models_to_try = (
        [LLM_MODEL_PRIMARY, LLM_MODEL_FALLBACK]
        if LLM_MODEL_PRIMARY != LLM_MODEL_FALLBACK
        else [LLM_MODEL_PRIMARY]
    )
    # Retry the whole model list once with a short backoff: the Free-Tiers alias
    # round-robins across free providers and transiently rate-limits under burst
    # (parallel bull+bear). A single 200-response call is ~0.5s, so one retry is
    # cheap insurance against a transient failure silently fail-opening to APPROVE.
    for attempt in range(2):
        for model in models_to_try:
            try:
                resp = requests.post(
                    f"{LLM_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {LLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        # 512 (not 120): Free-Tiers routes to reasoning models
                        # (cerebras/nemotron) that spend the budget reasoning and emit
                        # no content at 120 -> empty -> AMBIGUOUS fail-open (approve-all).
                        "max_tokens": 512,
                        "temperature": 0.3,
                    },
                    timeout=LLM_TIMEOUT_SEC,
                )
                if resp.status_code == 200:
                    text = resp.text.strip()
                    if text.startswith("data:"):
                        chunks = []
                        for line in text.splitlines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            obj = json.loads(payload)
                            choice = obj.get("choices", [{}])[0]
                            delta = choice.get("delta") or {}
                            msg = choice.get("message") or {}
                            # Reasoning models may omit `content`; fall back to
                            # `reasoning_content` so the response is never silently empty.
                            if delta.get("content"):
                                chunks.append(delta["content"])
                            elif msg.get("content"):
                                chunks.append(msg["content"])
                            elif delta.get("reasoning_content"):
                                chunks.append(delta["reasoning_content"])
                            elif msg.get("reasoning_content"):
                                chunks.append(msg["reasoning_content"])
                        result = "".join(chunks).strip()
                    else:
                        # Gateway may append "data: [DONE]" or newlines after JSON.
                        # Use raw_decode to parse the FIRST JSON object only,
                        # ignoring any trailing garbage (whitespace, "data: [DONE]").
                        text = text.lstrip()
                        decoder = json.JSONDecoder()
                        data, _ = decoder.raw_decode(text)
                        msg = data["choices"][0].get("message") or {}
                        # Non-SSE reasoning models may also omit content.
                        result = (msg.get("content")
                                  or msg.get("reasoning_content") or "").strip()
                    # Only accept a non-empty result; empty -> try next / retry
                    if result:
                        if _fallback_active and model == LLM_MODEL_PRIMARY:
                            _fallback_active = False
                        _current_model = model
                        return result
                # Non-200 or empty: try next model
            except Exception:
                # Exception: try next model
                pass
        # Whole list failed this pass — short backoff before one retry.
        if attempt == 0:
            time.sleep(1.0)
    # All models failed
    return "ERR:all_models_failed"


def adversarial_model_status() -> str:
    """Return current model name and whether fallback is active."""
    return f"{_current_model}{' (fallback)' if _fallback_active else ''}"


def _num(v: Any, default: float = 0.0) -> float:
    """Coerce to float, tolerating None/str/bad input."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _build_data_block(s: Dict[str, Any]) -> str:
    """Render the deterministic market data (already computed upstream) into a
    compact one-line context string for the debate prompts. Only includes keys
    that are actually present, so the engine.py caller (geometry-only) degrades
    to 'geometry only' rather than a wall of n/a."""
    parts = []
    if "price" in s and s.get("price") is not None:
        parts.append(f"price={_num(s.get('price')):.6g}")
    if "timeframe" in s and s.get("timeframe"):
        parts.append(f"tf={s.get('timeframe')}")
    if "leverage" in s and s.get("leverage") is not None:
        parts.append(f"lev={_num(s.get('leverage')):.0f}x")
    if "rsi" in s and s.get("rsi") is not None:
        parts.append(f"RSI(14)={_num(s.get('rsi'), 50):.0f}")
    if "cvd_zscore" in s and s.get("cvd_zscore") is not None:
        parts.append(f"CVD z={_num(s.get('cvd_zscore')):.2f}")
    oi5 = s.get("oi_change_5m_pct")
    oi15 = s.get("oi_change_15m_pct")
    oi1h = s.get("oi_change_1h_pct", s.get("oi_delta"))
    if oi5 is not None or oi15 is not None or oi1h is not None:
        parts.append(
            f"OI 5m/15m/1h={_num(oi5):+.2f}%/"
            f"{_num(oi15):+.2f}%/{_num(oi1h):+.2f}%"
        )
    _funding = s.get("funding_rate", s.get("funding"))
    if _funding is not None:
        parts.append(f"funding={_num(_funding):+.4f}%")
    if s.get("flow_direction"):
        parts.append(f"flow={s.get('flow_direction')}")
    if s.get("qvol_5m") is not None:
        parts.append(f"qVol5m=${_num(s.get('qvol_5m')):.0f}")
    if s.get("data_quality"):
        parts.append(f"data={s.get('data_quality')}")
    if s.get("regime") or s.get("regime_label"):
        parts.append(f"regime={s.get('regime') or s.get('regime_label')}")
    if "mtf_score" in s and s.get("mtf_score") is not None:
        parts.append(f"MTF={_num(s.get('mtf_score')):.0f}/100")
    if "tv_score" in s and s.get("tv_score") is not None:
        parts.append(f"TV={_num(s.get('tv_score')):.0f}/100")
    if "validation_score" in s and s.get("validation_score") is not None:
        parts.append(f"confluence={_num(s.get('validation_score')):.0f}/100")
    return " | ".join(parts) if parts else "geometry only (no market metrics supplied)"


def bull_bear_check(symbol: str, s: Dict[str, Any]) -> Tuple[bool, str]:
    """Run bull vs bear debate. Returns (ok, reason)."""
    global _current_model, _fallback_active
    if not LLM_API_KEY:
        # No LLM configured — allow all trades (default behavior)
        return True, "no_llm"

    tier = s.get("tier", "M30")
    side = s.get("side", "BULL")
    entry = _num(s.get("entry", 0))
    # Accept both v1 keys (sl/tp) and orchestrator keys (stop_loss/take_profits).
    sl = _num(s.get("sl", s.get("stop_loss", 0)))
    _tp_raw = s.get("tp", s.get("take_profits", 0))
    if isinstance(_tp_raw, (list, tuple)):
        _tp_raw = _tp_raw[0] if _tp_raw else 0  # first TP target
    tp = _num(_tp_raw)
    imb_side = s.get("imb_side", "?")
    t_complete = s.get("t_complete", "?")

    # Deterministic market data already computed by validation_engine — feed it
    # to the debate so bull/bear/judge reason on real OI/CVD/funding/RSI, not
    # just geometry. Missing values render as 'n/a' (engine.py caller omits them).
    data_block = _build_data_block(s)

    fmt = dict(
        symbol=symbol, side=side, tier=tier,
        entry=f"{entry:.6g}", sl=f"{sl:.6g}", tp=f"{tp:.6g}",
        rr=2, imb_side=imb_side, t_complete=str(t_complete)[:19],
        data_block=data_block,
    )

    # Bull & bear run in PARALLEL (matches v2) — halves debate wall time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_bull = pool.submit(_call_llm, BULL_PROMPT.format(**fmt))
        fut_bear = pool.submit(_call_llm, BEAR_PROMPT.format(**fmt))
        try:
            bull_arg = fut_bull.result(timeout=LLM_TIMEOUT_SEC + 5)
        except Exception:
            bull_arg = "ERR:timeout"
        try:
            bear_arg = fut_bear.result(timeout=LLM_TIMEOUT_SEC + 5)
        except Exception:
            bear_arg = "ERR:timeout"

    if bull_arg.startswith("ERR") or bull_arg.startswith("HTTP"):
        # If primary failed, mark fallback and retry once
        if not _fallback_active and LLM_MODEL_PRIMARY != LLM_MODEL_FALLBACK:
            _fallback_active = True
            _current_model = LLM_MODEL_FALLBACK
            bull_arg = _call_llm(BULL_PROMPT.format(**fmt))
        if bull_arg.startswith("ERR") or bull_arg.startswith("HTTP"):
            return True, f"llm_err:{bull_arg}"

    if bear_arg.startswith("ERR") or bear_arg.startswith("HTTP"):
        return True, f"llm_err:{bear_arg}"

    judge = _call_llm(JUDGE_PROMPT.format(bull_arg=bull_arg, bear_arg=bear_arg, data_block=data_block))
    ok, verdict = _parse_judge(judge)
    if verdict == "AMBIGUOUS":
        # Fail-open on unparsable judge output — never reject on formatting noise.
        print(f"[WARN] ADV_JUDGE_AMBIGUOUS {symbol}: {judge[:80]!r} — fail-open")
        return True, f"ADV_JUDGE_AMBIGUOUS (fail-open): {judge[:80]}"
    return ok, judge[:300]


def _parse_judge(response: str) -> Tuple[bool, str]:
    """Robust judge parsing: strip/uppercase and look for a YES/NO word boundary
    at the START of the response (exact-prefix matching biased toward reject).
    Returns (ok, verdict) where verdict is YES | NO | AMBIGUOUS."""
    text = str(response or "").strip().upper()
    if not text:
        return False, "AMBIGUOUS"
    # 1. Explicit verdict marker wins (VERDICT:/ANSWER:/DECISION:/FINAL: YES|NO)
    m = re.search(r"\b(?:VERDICT|ANSWER|DECISION|FINAL)\s*[:\-]?\s*(YES|NO)\b", text)
    if m:
        return m.group(1) == "YES", m.group(1)
    # 2. Verdict at the very start (clean models)
    m = re.match(r"^\W*(YES|NO)\b", text)
    if m:
        return m.group(1) == "YES", m.group(1)
    # 3. Reasoning models conclude at the END — take the LAST standalone YES/NO
    toks = re.findall(r"\b(YES|NO)\b", text)
    if toks:
        return toks[-1] == "YES", toks[-1]
    return False, "AMBIGUOUS"
