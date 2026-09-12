"""
Pluggable chart-image vision client (Tahap 2).

Reads the chart/outlook image attached to a signal and returns structured data
(timeframe, trend, entry, SL, TP, S/R) so the bot can "read like a human".

Backends (set SIGNAL_COPY_VISION_BACKEND):
- "openai" (default): OpenAI-compatible API (OpenRouter, Google Gemini openai-endpoint,
  OpenAI, similar gateways). Uses VISION_OPENAI_BASE_URL, VISION_OPENAI_API_KEY,
  VISION_OPENAI_MODEL. Fast + offloads VPS CPU. No local ollama needed.
- "n8n": POST the image+context to an n8n webhook that runs the vision step and
  returns the same JSON contract. Lets you iterate prompts/models in n8n's UI
  without redeploying the bot.

Design: never raises; returns a dict or None. All network I/O is async (httpx)
so a slow cloud inference never blocks other coroutines.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from typing import Any, Dict, Optional

import httpx

from utils.logger import logger
from . import signal_copy_config as scfg


def _downscale(image: bytes, max_dim: int = 768) -> bytes:
    """Shrink large chart images so vision is faster (fewer image tokens).
    Keeps aspect ratio; returns original bytes on any failure."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) <= max_dim:
            return image
        scale = max_dim / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as exc:
        logger.debug("[VISION] downscale skipped: %s", exc)
        return image


# JSON contract we ask the model to fill (numbers as numbers, null if unknown).
_PROMPT = (
    "You are reading a CRYPTO TRADING chart image (an analyst outlook). "
    "Extract ONLY what is actually visible/derivable. Do not invent values. "
    "Return a JSON object with EXACTLY these keys: "
    "pair (the trading pair/symbol shown on the chart, e.g. \"ATUSDT\", "
    "\"BTCUSDT\" — strip exchange suffixes like \".P\"/PERP; or null), "
    "timeframe (string like \"1h\",\"15m\",\"4h\" or null), "
    "side (\"LONG\",\"SHORT\" or null), "
    "trend (\"UP\",\"DOWN\",\"SIDEWAYS\" or null), "
    "entry (number or null), stop_loss (number or null), "
    "take_profits (array of numbers, may be empty), "
    "support (array of numbers, may be empty), "
    "resistance (array of numbers, may be empty), "
    "patterns (array of short strings, may be empty), "
    "confidence (number 0..1), notes (short string). "
    "Numbers must be plain numbers (no quotes, no currency symbols)."
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# Serialize all vision calls: avoids rate-limit bursts and CPU thrash.
_LOCK = asyncio.Lock()


def _coerce_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    for candidate in (text,):
        try:
            return json.loads(candidate)
        except Exception:
            pass
    m = _JSON_FENCE.search(text) or _JSON_OBJ.search(text)
    if m:
        frag = m.group(1) if m.re is _JSON_FENCE else m.group(0)
        try:
            return json.loads(frag)
        except Exception:
            return None
    return None


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the model's raw dict into our canonical shape (defensive)."""
    out: Dict[str, Any] = {}

    def _num(v):
        try:
            if v is None or isinstance(v, bool):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    def _numlist(v):
        if not isinstance(v, list):
            return []
        res = []
        for x in v:
            n = _num(x)
            if n is not None:
                res.append(n)
        return res

    tf = data.get("timeframe")
    out["timeframe"] = str(tf).strip().lower() if isinstance(tf, str) and tf.strip() else None
    pair = data.get("pair") or data.get("symbol")
    if isinstance(pair, str) and pair.strip():
        p = pair.strip().upper().lstrip("$").replace("/", "").replace("-", "")
        p = p.replace(".P", "").replace("PERP", "").replace("PERPETUAL", "")
        out["pair"] = p or None
    else:
        out["pair"] = None
    side = data.get("side")
    out["side"] = str(side).strip().upper() if isinstance(side, str) and side.strip() else None
    trend = data.get("trend")
    out["trend"] = str(trend).strip().upper() if isinstance(trend, str) and trend.strip() else None
    out["entry"] = _num(data.get("entry"))
    out["stop_loss"] = _num(data.get("stop_loss"))
    out["take_profits"] = _numlist(data.get("take_profits"))
    out["support"] = _numlist(data.get("support"))
    out["resistance"] = _numlist(data.get("resistance"))
    pats = data.get("patterns")
    out["patterns"] = [str(p) for p in pats] if isinstance(pats, list) else []
    out["confidence"] = _num(data.get("confidence")) or 0.0
    notes = data.get("notes")
    out["notes"] = str(notes)[:300] if notes else ""
    return out


async def _via_n8n(image_b64: str, symbol: str, raw_text: str) -> Optional[Dict[str, Any]]:
    if not scfg.N8N_WEBHOOK_URL:
        logger.warning("[VISION] n8n backend selected but SIGNAL_COPY_N8N_WEBHOOK_URL is empty")
        return None
    body = {"symbol": symbol, "raw_text": raw_text[:1000], "image_b64": image_b64}
    async with httpx.AsyncClient(timeout=scfg.VISION_TIMEOUT_SEC) as client:
        resp = await client.post(scfg.N8N_WEBHOOK_URL, json=body)
        resp.raise_for_status()
        data = resp.json()
    # n8n may wrap the result; accept either the object or {"result": {...}}
    if isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
        data = data["result"]
    return data if isinstance(data, dict) else None


async def _via_openai(image_b64: str, symbol: str, raw_text: str) -> Optional[Dict[str, Any]]:
    """OpenAI-compatible chat/completions with image — works with OpenRouter,
    Google Gemini (OpenAI-compat endpoint), OpenAI, and similar gateways."""
    if not scfg.VISION_OPENAI_BASE_URL or not scfg.VISION_OPENAI_API_KEY:
        logger.warning("[VISION] openai backend selected but base_url/api_key empty")
        return None
    prompt = _PROMPT
    if symbol:
        prompt += f"\nThe pair is likely {symbol}."
    if raw_text:
        prompt += f"\nAccompanying text (context only): {raw_text[:400]}"
    body = {
        "model": scfg.VISION_OPENAI_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]}],
        "max_tokens": 4096,
        "temperature": 0,
        "stream": False,
    }
    url = scfg.VISION_OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {scfg.VISION_OPENAI_API_KEY}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=scfg.VISION_TIMEOUT_SEC) as client:
        data = None
        for attempt in range(2):
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 429:  # free-tier rate limit
                ra = (resp.headers.get("retry-after") or "").strip()
                wait = float(ra) if ra.replace(".", "", 1).isdigit() else 8.0
                wait = min(max(wait, 4.0), 15.0)
                logger.warning("[VISION] openai 429 rate-limited; retry in %.0fs (attempt %d/2)",
                               wait, attempt + 1)
                if attempt < 1:
                    await asyncio.sleep(wait)
                    continue
                return None  # exhausted -> let analyze_chart fall back
            resp.raise_for_status()
            data = resp.json()
            break
    if data is None:
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("[VISION] openai: no content in response: %s", str(data)[:200])
        return None
    parsed = _coerce_json(content)
    if parsed is None:
        fr = (data.get("choices") or [{}])[0].get("finish_reason")
        logger.warning("[VISION] openai: JSON parse failed (finish=%s) content=%r",
                       fr, (content or "")[:200])
    return parsed


async def analyze_chart(image: bytes, *, symbol: str = "",
                        raw_text: str = "") -> Optional[Dict[str, Any]]:
    """Analyze a chart image. Uses OpenAI-compat (OpenRouter) backend. Never raises."""
    if not getattr(scfg, "VISION_ENABLED", False) or not image:
        return None
    backend = (getattr(scfg, "VISION_BACKEND", "openai") or "openai").lower()
    try:
        image_b64 = base64.b64encode(_downscale(image)).decode()
        async with _LOCK:  # one vision request at a time (anti-429 / anti-thrash)
            if backend == "n8n":
                raw = await _via_n8n(image_b64, symbol, raw_text)
            else:  # "openai" or anything else -> treat as openai
                raw = await _via_openai(image_b64, symbol, raw_text)
        if not isinstance(raw, dict):
            return None
        data = _normalize(raw)
        logger.info("[VISION] %s backend=%s pair=%s tf=%s side=%s entry=%s sl=%s tps=%s conf=%.2f",
                    symbol or "?", backend, data.get("pair"), data["timeframe"], data["side"],
                    data["entry"], data["stop_loss"], data["take_profits"], data["confidence"])
        return data
    except Exception as exc:
        logger.warning("[VISION] analyze_chart failed (%s): %s: %r",
                       backend, type(exc).__name__, exc)
        return None