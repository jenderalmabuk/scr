"""
Adversarial v2 — 12-agent pipeline with model pool round-robin.
Limit-entry aware: higher latency acceptable because orders rest in orderbook.

Architecture:
    Pool (14 models) → round-robin across 12 agents
    Agent 1-6   (scoring):    pool round-robin
    Agent 7-8   (bull/bear):  tier-1 pool (gemini-pro, nemotron-ultra, gpt-4.1)
    Agent 9     (judge):      FIXED gc/gemini-2.5-pro (must output strict YES/NO)
    Agent 10-12 (guard):      pool round-robin

Usage:
    from clean_core.adversarial_v2 import adversarial_check_v2
    ok, reason, journal = adversarial_check_v2(symbol, setup, context)
"""
import json, os, time, threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── LLM config ──────────────────────────────────────────────────
LLM_API_KEY = os.getenv("NINE_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""
LLM_BASE = os.getenv("NINE_ROUTER_BASE") or "http://127.0.0.1:20128/v1"

# Active model pool (user-supplied list, round-robin)
ACTIVE_MODELS_RAW = os.getenv("ADVERSARIAL_MODEL_POOL", "")
if ACTIVE_MODELS_RAW:
    MODEL_POOL = [m.strip() for m in ACTIVE_MODELS_RAW.split(",") if m.strip()]
else:
    # Default: 14 active models on 9router
    MODEL_POOL = [
        "gh/gpt-4.1",
        "mmf/mimo-auto",
        "gc/gemini-2.5-pro",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/deepseek-ai/deepseek-v4-flash",
        "nvidia/minimaxai/minimax-m3",
        "cf/@cf/qwen/qwen2.5-coder-32b-instruct",
        "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "ollama/gpt-oss:120b",
        "ollama/minimax-m3",
        "gemini/gemini-2.5-flash",
        "gemini/gemma-4-31b-it",
        "cerebras/zai-glm-4.7",
        "groq/openai/gpt-oss-120b",
    ]

# Tier-1 pool (bull, bear — need reasoning quality). Env-configurable.
_TIER1_RAW = os.getenv("ADVERSARIAL_TIER1_POOL", "")
if _TIER1_RAW:
    TIER1_POOL = [m.strip() for m in _TIER1_RAW.split(",") if m.strip()]
else:
    TIER1_POOL = [
        "cerebras/gpt-oss-120b",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
        "gh/gpt-4.1",
    ]

# Judge model — FIXED, must output strict YES/NO
JUDGE_MODEL = os.getenv("ADVERSARIAL_JUDGE_MODEL", "cerebras/gpt-oss-120b")

# Round-robin counters (thread-safe via lock)
_pool_idx = 0
_tier1_idx = 0
_lock = threading.Lock()

# Fallback model if pool exhausted
FALLBACK_MODEL = os.getenv("ADVERSARIAL_MODEL_FALLBACK", "cerebras/gpt-oss-120b")


def _next_model(pool: List[str], tier1: bool = False) -> str:
    """Round-robin through pool. Thread-safe."""
    global _pool_idx, _tier1_idx
    with _lock:
        if tier1:
            idx = _tier1_idx
            _tier1_idx = (idx + 1) % len(TIER1_POOL)
            return TIER1_POOL[idx]
        idx = _pool_idx
        _pool_idx = (idx + 1) % len(pool)
        return pool[idx]


# ── Agent prompts ───────────────────────────────────────────────

SCANNER_PROMPT = """Role: Pair Screener
Task: Rate {symbol} for intraday tradability.
Volume: {volume:.0f} USDT | Spread: {spread}% | Turnover: {turnover:.0f} USDT
Current price: {current_price:.6g}
Rate tradability 1-10 (1=illiquid/avoid, 10=excellent).
Reply ONLY with the integer number. No explanation."""

TECHNICAL_PROMPT = """Role: Technical Analyst
Task: Rate {symbol} {tier} {side} setup technical strength.
Entry: {entry:.6g} | SL: {sl:.6g} | TP: {tp:.6g} | RR: 1:{rr}
RSI(14): {rsi} | EMA distance: {ema_dist}% | ATR%: {atr:.1f}
Choppiness: {chop} | Efficiency Ratio: {er}
Pattern: orderblock + imbalance on {tier}
Rate technical confluence 1-10. Reply ONLY with the integer number."""

SMART_MONEY_PROMPT = """Role: Smart Money Analyst
Task: Analyze smart money flow for {symbol} {side} on {tier}.
CVD z-score: {cvd_z} | OI delta: {oi_delta}% | Funding: {funding}%
Flow verdict: {flow_verdict}

WHALE CONTEXT (on-chain):
- Bias: {whale_bias}
- Event: {whale_event_type}
- Value: ${whale_value_usd:,.0f}
- Age: {whale_age_minutes} minutes ago

SILENT ACCUMULATION:
- State: {accumulation_state}
- Score: {accumulation_score}/10

VIP FAST LANE:
- Status: {vip_status}
- Score: {vip_score}/100
- Trigger Ready: {vip_trigger_ready}

Rate smart money alignment 1-10 (1=hostile/against trade, 10=strongly aligned).
Consider: whale flow (on-chain confirmation), silent accumulation (compression), CVD/OI/funding confluence.
Reply ONLY with the integer number."""

LIQUIDITY_PROMPT = """Role: Liquidity Analyst
Task: Assess slippage risk for {symbol} limit entry at {entry:.6g}.
QVOL 5m: {qvol:.0f} | Spread: {spread}% | Orderbook depth: {depth}
Rate liquidity quality 1-10 (1=high slippage risk, 10=deep liquid).
Reply ONLY with the integer number."""

MACRO_PROMPT = """Role: Macro Analyst
Task: Assess macro tailwind/headwind for {symbol} {side} entry.
BTC regime: {btc_regime} | BTC dominance: {btc_dom}%
DXY: {dxy} | VIX: {vix}
Rate macro backdrop 1-10 (1=strong headwind, 10=strong tailwind).
Reply ONLY with the integer number."""

SENTIMENT_PROMPT = """Role: Sentiment Analyst
Task: Assess sentiment contrarian opportunity for {symbol} {side}.
Funding rate: {funding}% | Long/Short ratio: {ls_ratio}
Fear & Greed: {fng}
Rate sentiment setup 1-10 (1=extremely crowded/capitulation-wrong, 10=contrarian opportunity).
Reply ONLY with the integer number."""

BULL_PROMPT = """Role: Bull Analyst
Task: Argue why {symbol} {side} on {tier} is a GOOD entry.
Entry: {entry:.6g} | SL: {sl:.6g} | TP: {tp:.6g} | RR: 1:{rr}
Imbalance: {imb_side} | Complete: {t_complete}
Analyst scores — Scanner:{scanner}/10 Tech:{tech}/10 Flow:{flow}/10
Liq:{liq}/10 Macro:{macro}/10 Sentiment:{sent}/10
Keep under 100 words. Be specific about what confirms this trade."""

BEAR_PROMPT = """Role: Bear Analyst
Task: Argue why {symbol} {side} on {tier} is a BAD entry.
Entry: {entry:.6g} | SL: {sl:.6g} | TP: {tp:.6g} | RR: 1:{rr}
Imbalance: {imb_side} | Complete: {t_complete}
Analyst scores — Scanner:{scanner}/10 Tech:{tech}/10 Flow:{flow}/10
Liq:{liq}/10 Macro:{macro}/10 Sentiment:{sent}/10
Focus on: fakeout risk, conflicting scores, adverse conditions.
Keep under 100 words. Be specific about what invalidates this trade."""

JUDGE_PROMPT = """Role: Trade Judge (CRITICAL — strict format required)
Two analysts debated this setup:

BULL: {bull_arg}
BEAR: {bear_arg}

Background scores — Scanner:{scanner}/10 Tech:{tech}/10 Flow:{flow}/10
Liq:{liq}/10 Macro:{macro}/10 Sentiment:{sent}/10

Decide: should we take this trade?

GUIDELINES:
- Individual low scores (1-3) are NOT auto-reject if other factors strong
- Focus on: technical confluence, smart money alignment, risk/reward
- Scanner/Liquidity issues acceptable if flow supportive + technical solid
- Reject only if: multiple critical scores low OR bear argument overwhelmingly strong

Reply ONLY with YES or NO.
No explanation. No punctuation. Only YES or NO."""

RISK_MANAGER_PROMPT = """Role: Risk Manager
Task: {symbol} {side} {tier} — entry at {entry:.6g}, SL at {sl:.6g}, TP at {tp:.6g}.
Risk per trade: {risk_pct:.1f}% of {equity:.0f} USDT = {risk_usdt:.2f} USDT
Current positions: {cur_pos}/{max_pos} | Daily P&L: {daily_pnl:+.1f}%
Max DD allowed: {max_dd:.0f}%
Size this trade at {risk_pct:.1f}%? Reply ONLY YES or NO."""

EXECUTION_GUARD_PROMPT = """Role: Execution Guard
Task: {symbol} LIMIT entry at {entry:.6g}.
Current price: {current_price:.6g} | Bid/Ask: {bid:.6g}/{ask:.6g}
Time to candle close: {time_to_close}s
Unusual volume spike: {vol_spike}
Recent news/event: {news_flag}
Is this limit entry executable safely? Reply ONLY YES or NO."""

TRADE_JOURNAL_PROMPT = """Role: Trade Journalist
Task: Document this {symbol} {tier} {side} trade decision.
Analyst scores: S={scanner}/10 T={tech}/10 F={flow}/10 L={liq}/10 M={macro}/10 S={sent}/10
Judge: {judge_verdict} | Risk: {risk_ok} | Exec: {exec_ok}
Write a ONE-sentence journal entry. Include key reason for decision.
Keep under 50 words. No formatting."""


# ── LLM caller ──────────────────────────────────────────────────

def _call_llm(prompt: str, model: str, timeout: int = 15, max_tokens: int = 512) -> str:
    """Single LLM call via 9router/OpenRouter-compatible API."""
    import requests
    if not LLM_API_KEY:
        return "NO_API_KEY"
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
                # 512 default (not 120): reasoning models (cerebras/nemotron) spend
                # the budget on reasoning and emit no `content` at 120 -> empty/KeyError.
                # Judge passes a higher budget so it can finish reasoning + emit verdict.
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return f"HTTP_{resp.status_code}"
        text = resp.text.strip()
        # SSE streaming response
        if text.startswith("data:"):
            chunks = []
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                choice = obj.get("choices", [{}])[0]
                delta = choice.get("delta") or {}
                msg = choice.get("message") or {}
                # Reasoning models may omit `content`; fall back to `reasoning_content`
                # then `reasoning` (raw) if still empty.
                val = (msg.get("content") or msg.get("reasoning_content")
                       or msg.get("reasoning") or "")
                if val:
                    chunks.append(val)
            return "".join(chunks).strip()
        # Non-streaming JSON response — use raw_decode to tolerate trailing data
        # (some upstreams append "data: [DONE]" or newlines after the JSON body).
        data = json.JSONDecoder().raw_decode(text.lstrip())[0]
        msg = data["choices"][0]["message"]
        # Same fallback chain for non-streaming: content → reasoning_content → reasoning
        content = (msg.get("content") or msg.get("reasoning_content")
                   or msg.get("reasoning") or "")
        return content.strip()
    except Exception as e:
        return f"ERR:{e}"


def _call_agent(prompt: str, tier1: bool = False) -> Tuple[str, str]:
    """Call an agent with round-robin model. Returns (model_used, response)."""
    model = _next_model(MODEL_POOL, tier1=tier1)
    result = _call_llm(prompt, model)
    # Retry with fallback if the pool model failed OR returned empty output.
    # Some upstreams return HTTP 200 with no content; without this guard the
    # empty string silently becomes neutral score 5 / auto-approve.
    if (not result.strip()) or result.startswith(("ERR", "HTTP", "NO_API_KEY")):
        result = _call_llm(prompt, FALLBACK_MODEL)
        model = FALLBACK_MODEL
    return model, result


def _extract_score(response: str) -> int:
    """Extract first integer from agent response (for scoring agents)."""
    import re
    match = re.search(r'\b(\d+)\b', str(response))
    if match:
        val = int(match.group(1))
        return max(1, min(10, val))
    return 5  # neutral default


def _extract_yes_no(response: str, default: bool = True) -> bool:
    """Robust YES/NO parsing (aligned with v1 _parse_judge): normalize then
    regex \\b(YES|NO)\\b at the start of the response. Ambiguous output
    fail-opens to `default` instead of biasing toward reject."""
    import re
    text = str(response or "").strip().upper()
    if not text:
        print("[WARN] ADV_JUDGE_AMBIGUOUS: '' (empty) — fail-open")
        return default
    # 1. Explicit verdict marker wins (VERDICT:/ANSWER:/DECISION:/FINAL: YES|NO)
    m = re.search(r"\b(?:VERDICT|ANSWER|DECISION|FINAL)\s*[:\-]?\s*(YES|NO)\b", text)
    if m:
        return m.group(1) == "YES"
    # 2. Verdict at the very start (clean models)
    m = re.match(r"^\W*(YES|NO)\b", text)
    if m:
        return m.group(1) == "YES"
    # 3. Reasoning models conclude at the END — take the LAST standalone YES/NO
    tokens = re.findall(r"\b(YES|NO)\b", text)
    if tokens:
        return tokens[-1] == "YES"
    print(f"[WARN] ADV_JUDGE_AMBIGUOUS: {text[:80]!r} — fail-open")
    return default


# ── Main pipeline ────────────────────────────────────────────────

def adversarial_check_v2(
    symbol: str,
    setup: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    12-agent adversarial pipeline with model pool round-robin.

    Returns:
        ok: bool — whether to proceed with trade
        reason: str — short reason string
        journal: dict — full agent outputs for logging
    """
    if not LLM_API_KEY:
        return True, "no_llm", {}

    c = context or {}
    s = setup

    # ── Extract setup fields ─────────────────────────────────────
    tier = s.get("tier", "M30")
    side_raw = s.get("side", "BULL")
    side = "LONG" if side_raw == "BULL" else "SHORT"
    entry = s.get("entry", 0)
    sl = s.get("sl", 0)
    tp = s.get("tp", 0)
    imb_side = s.get("imb_side", side_raw)
    t_complete = str(s.get("t_complete", "?"))[:19]
    # Compute RR from entry/sl/tp instead of hardcoded 2.0
    if sl and entry and tp:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = round(reward / risk, 2) if risk > 0 else 2.0
    else:
        rr = 2.0

    # ── Extract context fields (from engine + ltf) ──────────────
    # Price/volume
    current_price = c.get("current_price", entry)
    volume = c.get("volume", 0)
    turnover = c.get("turnover", 0)
    qvol = c.get("qvol", 0)
    spread = c.get("spread", 0.02)
    depth = c.get("depth", "normal")
    vol_spike = c.get("vol_spike", "none")
    news_flag = c.get("news_flag", "none")

    # Technical
    rsi = c.get("rsi", 50)
    ema_dist = c.get("ema_dist", 0)
    atr = c.get("atr", 3)
    chop = c.get("chop", 0.5)
    er = c.get("er", 0.3)

    # Flow
    cvd_z = c.get("cvd_z", 0)
    oi_delta = c.get("oi_delta", 0)
    funding = c.get("funding", 0)
    flow_verdict = c.get("flow_verdict", "neutral")

    # Macro
    btc_regime = c.get("btc_regime", "neutral")
    btc_dom = c.get("btc_dom", 55)
    dxy = c.get("dxy", 100)
    vix = c.get("vix", 15)

    # Sentiment
    ls_ratio = c.get("ls_ratio", 1.0)
    fng = c.get("fng", 50)

    # Execution
    bid = c.get("bid", current_price * 0.999)
    ask = c.get("ask", current_price * 1.001)
    time_to_close = c.get("time_to_close", 300)

    # Risk
    equity = c.get("equity", 1000)
    risk_pct = c.get("risk_pct", 1.0)
    risk_usdt = equity * risk_pct / 100
    cur_pos = c.get("cur_pos", 0)
    max_positions = c.get("max_positions", 6)
    daily_pnl = c.get("daily_pnl", 0)
    max_dd = c.get("max_dd", 20)

    # ── Format dict for prompts ──────────────────────────────────
    fmt = dict(
        symbol=symbol, side=side, tier=tier,
        entry=entry, sl=sl, tp=tp, rr=rr,
        imb_side=imb_side, t_complete=t_complete,
        current_price=current_price, volume=volume, turnover=turnover,
        qvol=qvol, spread=spread, depth=depth,
        vol_spike=vol_spike, news_flag=news_flag,
        rsi=rsi, ema_dist=ema_dist, atr=atr, chop=chop, er=er,
        cvd_z=cvd_z, oi_delta=oi_delta, funding=funding,
        flow_verdict=flow_verdict,
        # Whale / accumulation / VIP enrichment (supplied by engine context.update)
        whale_bias=c.get("whale_bias", "NEUTRAL"),
        whale_event_type=c.get("whale_event_type", "NONE"),
        whale_value_usd=c.get("whale_value_usd", 0) or 0,
        whale_age_minutes=c.get("whale_age_minutes", None),
        accumulation_state=c.get("accumulation_state", "NONE"),
        accumulation_score=c.get("accumulation_score", 0),
        vip_status=c.get("vip_status", "NORMAL"),
        vip_score=c.get("vip_score", 0),
        vip_trigger_ready=c.get("vip_trigger_ready", False),
        btc_regime=btc_regime, btc_dom=btc_dom, dxy=dxy, vix=vix,
        ls_ratio=ls_ratio, fng=fng,
        bid=bid, ask=ask, time_to_close=time_to_close,
        equity=equity, risk_pct=risk_pct, risk_usdt=risk_usdt,
        cur_pos=cur_pos, max_pos=max_positions,
        daily_pnl=daily_pnl, max_dd=max_dd,
    )

    journal: Dict[str, Any] = {
        "symbol": symbol, "tier": tier, "side": side,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agents": {},
    }

    # ── Phase 1: Scoring agents (parallel, 6 calls) ──────────────
    score_agents = [
        ("scanner",      SCANNER_PROMPT,      False),
        ("technical",    TECHNICAL_PROMPT,    False),
        ("smart_money",  SMART_MONEY_PROMPT,  False),
        ("liquidity",    LIQUIDITY_PROMPT,    False),
        ("macro",        MACRO_PROMPT,        False),
        ("sentiment",    SENTIMENT_PROMPT,    False),
    ]

    scores: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_call_agent, prompt.format(**fmt), tier1): name
            for name, prompt, tier1 in score_agents
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                model, resp = fut.result(timeout=20)
                score = _extract_score(resp)
                scores[name] = score
                journal["agents"][name] = {"model": model, "response": resp[:200], "score": score}
            except Exception as e:
                scores[name] = 5
                journal["agents"][name] = {"model": "TIMEOUT", "response": str(e), "score": 5}
    
    # ── Pre-filter: Skip obviously bad candidates (save tokens) ──
    # Reject if BOTH scanner AND liquidity are critically low (<= 2)
    # Allow entry if at least ONE is acceptable (>= 3)
    scanner_score = scores.get("scanner", 5)
    liquidity_score = scores.get("liquidity", 5)
    
    if scanner_score <= 2 and liquidity_score <= 2:
        reason = f"pre_filter:REJECT scanner={scanner_score} liquidity={liquidity_score} (both critically low)"
        journal["pre_filter"] = {"passed": False, "reason": reason}
        return False, reason, journal
    
    journal["pre_filter"] = {"passed": True, "scanner": scanner_score, "liquidity": liquidity_score}

    # ── Phase 2: Bull & Bear (parallel, tier-1 pool) ─────────────
    # Map score keys to prompt variable names
    score_map = {
        "scanner": "scanner", "technical": "tech", "smart_money": "flow",
        "liquidity": "liq", "macro": "macro", "sentiment": "sent",
    }
    fmt_bull = {**fmt}
    for score_key, prompt_key in score_map.items():
        fmt_bull[prompt_key] = scores.get(score_key, 5)
    fmt_bear = dict(fmt_bull)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_bull = ex.submit(_call_agent, BULL_PROMPT.format(**fmt_bull), True)
        fut_bear = ex.submit(_call_agent, BEAR_PROMPT.format(**fmt_bear), True)
        try:
            bull_model, bull_arg = fut_bull.result(timeout=25)
        except Exception:
            bull_model, bull_arg = "TIMEOUT", "Bull analysis timed out."
        try:
            bear_model, bear_arg = fut_bear.result(timeout=25)
        except Exception:
            bear_model, bear_arg = "TIMEOUT", "Bear analysis timed out."

    journal["agents"]["bull"] = {"model": bull_model, "response": bull_arg[:300]}
    journal["agents"]["bear"] = {"model": bear_model, "response": bear_arg[:300]}

    # ── Phase 3: Judge (FIXED model, sequential) ─────────────────
    judge_prompt = JUDGE_PROMPT.format(
        bull_arg=bull_arg, bear_arg=bear_arg,
        scanner=scores.get("scanner", 5),
        tech=scores.get("technical", 5),
        flow=scores.get("smart_money", 5),
        liq=scores.get("liquidity", 5),
        macro=scores.get("macro", 5),
        sent=scores.get("sentiment", 5),
    )
    import re as _re
    def _has_verdict(txt: str) -> bool:
        """True if txt contains a parseable YES/NO (not just reasoning preamble)."""
        if not txt or not txt.strip():
            return False
        if txt.startswith(("ERR", "HTTP", "NO_API_KEY")):
            return False
        return bool(_re.search(r"\b(YES|NO)\b", txt.upper()))

    # Judge needs a bigger token budget: reasoning models (cerebras/nemotron via
    # Free-Tiers) burn ~500 tokens thinking before emitting the verdict. At 512
    # they leak the reasoning preamble with no YES/NO -> ambiguous fail-open.
    judge_model_used = JUDGE_MODEL
    judge_resp = _call_llm(judge_prompt, JUDGE_MODEL, max_tokens=1024)
    # Judge has no round-robin; retry once with the (non-reasoning) fallback if the
    # primary gives no parseable verdict — otherwise it fail-opens to APPROVE.
    if not _has_verdict(judge_resp):
        judge_model_used = FALLBACK_MODEL
        judge_resp = _call_llm(judge_prompt, FALLBACK_MODEL, max_tokens=1024)
    trade_ok = _extract_yes_no(judge_resp)
    journal["agents"]["judge"] = {
        "model": judge_model_used, "response": judge_resp[:100],
        "verdict": "YES" if trade_ok else "NO",
    }

    if not trade_ok:
        return False, f"judge:NO (scores={scores})", journal

    # ── Phase 4: Guard agents (parallel, 3 calls) ────────────────
    guard_agents = [
        ("risk_manager",    RISK_MANAGER_PROMPT,    False),
        ("execution_guard", EXECUTION_GUARD_PROMPT, False),
    ]

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_call_agent, prompt.format(**fmt), tier1): name
            for name, prompt, tier1 in guard_agents
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                model, resp = fut.result(timeout=15)
                ok = _extract_yes_no(resp)
                journal["agents"][name] = {
                    "model": model, "response": resp[:100], "passed": ok,
                }
                if not ok and name == "execution_guard":
                    return False, f"exec_guard:{resp[:80]}", journal
                if not ok and name == "risk_manager":
                    return False, f"risk_manager:{resp[:80]}", journal
            except Exception as e:
                # Timeout or error → pass: None (unknown), will reject below
                journal["agents"][name] = {
                    "model": "TIMEOUT", "response": str(e), "passed": None,
                }

    # After all agents: any timeout/error (passed=None) → reject (fail-close)
    for name in ("risk_manager", "execution_guard"):
        if journal["agents"].get(name, {}).get("passed") is None:
            return False, f"{name}:TIMEOUT", journal

    # ── Phase 5: Trade Journal (final, pool) ─────────────────────
    risk_ok = journal["agents"].get("risk_manager", {}).get("passed", True)
    exec_ok = journal["agents"].get("execution_guard", {}).get("passed", True)

    journal_prompt = TRADE_JOURNAL_PROMPT.format(
        **fmt_bull,
        judge_verdict="YES" if trade_ok else "NO",
        risk_ok="YES" if risk_ok else "NO",
        exec_ok="YES" if exec_ok else "NO",
    )
    journal_model, journal_text = _call_agent(journal_prompt)
    journal["agents"]["trade_journal"] = {
        "model": journal_model, "response": journal_text[:200],
    }
    journal["summary"] = journal_text[:200]

    return True, f"approved (scores={scores})", journal


def adversarial_model_status() -> str:
    """Return current model pool info."""
    return (
        f"pool={len(MODEL_POOL)} models, tier1={len(TIER1_POOL)}, "
        f"judge={JUDGE_MODEL}, fallback={FALLBACK_MODEL}"
    )
