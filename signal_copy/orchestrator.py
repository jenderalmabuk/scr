"""
SignalCopyOrchestrator: wires the full pipeline.

    listener(s) ──► handle_incoming_text()
                       │  parse_signal()
                       │  dedup
                       │  get_advanced_metrics()
                       │  validate_signal()
                       ├─ REJECT/WEAK ──► notify (no execution)
                       └─ VALID ──► register confirmation ──► confirm bot prompt
                                          │ user taps Ya
                                          ▼
                                   SignalExecutor.execute()

Dependencies are injected so this can run standalone (run_signal_copy.py) or be
embedded inside the main fusion bot. Anything missing degrades gracefully:
- no metrics provider  -> validation runs on empty metrics (will REJECT safely)
- no confirm bot       -> falls back to auto-execute only if explicitly enabled
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

from utils.logger import logger

from .signal_parser import parse_signal
from .signal_schema import SignalSource, ParsedSignal, SignalSide
from .validation_engine import validate_signal, Verdict
from .confirmation import ConfirmationManager, ConfirmState
from .executor import SignalExecutor, ExecutionOutcome
from .report_formatter import (
    build_validation_report,
    build_execution_result,
)
from .telegram_formatter import (
    build_parser_report,
    build_execution_message,
    build_close_message,
)
from . import signal_copy_config as scfg
from .channel_performance import get_tracker
from .conviction_sizer import ConvictionSizer
# from .vip_reporter import VIPSessionReporter  # TODO: wire VIP feature later


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class SignalCopyOrchestrator:
    def __init__(
        self,
        *,
        metrics_provider: Any = None,     # has async get_advanced_metrics(symbol)
        trader: Any = None,               # has async submit_open(**kwargs)
        risk_mgr: Any = None,             # RiskManager
        confirm_bot: Any = None,          # TelegramConfirmBot (optional)
        notifier: Any = None,             # async callable(text) for plain notices
        risk_pct: float = None,
        dry_run: bool = None,
        auto_execute: bool = None,
    ):
        self.metrics_provider = metrics_provider
        self.trader = trader
        self.risk_mgr = risk_mgr
        self.confirm_bot = confirm_bot
        self.notifier = notifier

        self.risk_pct = scfg.RISK_PCT if risk_pct is None else risk_pct
        self.dry_run = scfg.DRY_RUN if dry_run is None else dry_run
        self.auto_execute = scfg.AUTO_EXECUTE_WITHOUT_CONFIRM if auto_execute is None else auto_execute
        # Learning mode: no channel allowlist configured -> read everything and
        # surface each source's chat id so the user can pick channels one by one.
        self.learning_mode = not bool(scfg.TG_SIGNAL_CHANNELS)
        self._known_signal_sources: Dict[int, str] = {}

        self.confirmations = ConfirmationManager(default_expiry_sec=scfg.CONFIRM_EXPIRY_SEC)
        self.executor = (
            SignalExecutor(trader, risk_mgr, risk_pct=self.risk_pct)
            if (trader is not None and risk_mgr is not None) else None
        )
        self._recent: Dict[str, float] = {}   # dedup key -> ts
        self._sweeper_task: Optional[asyncio.Task] = None
        # limit setups waiting for price to reach the entry: token -> {result, created}
        self._pending_limits: Dict[str, dict] = {}
        self._pending_restored = False
        # chat ids whose messages must never be parsed as signals (e.g. our own
        # confirm bot, to prevent its prompts from re-triggering the pipeline).
        self.ignore_chat_ids: set = set()
        # channels that get parsed + notified but NEVER auto-executed
        # (calibration / forward-test mode)
        self.calib_channels: set = set()
        # whale-accumulation narratives captured for the (upcoming) accumulation
        # pipeline. Tahap 1 only records + (optionally) surfaces them.
        self._accum_log: list = []
        # Discord channels used as a READ-ONLY calibration intake (report only).
        self.calib_channels: set = set(getattr(scfg, "CALIB_DISCORD_CHANNELS", []) or [])
        self.sizer = ConvictionSizer()
        # self.vip_reporter = VIPSessionReporter(self._notify, accum_log=self._accum_log)  # TODO: wire VIP later
        self.vip_reporter = None  # Stub for now
        self.ch_perf = get_tracker()
        self._reporter_task = None
        
        # --- Dual Telegram Channels Notification Transport ---
        from .telegram_transport import (
            send_parser_notification,
            send_trades_notification,
        )
        
        self._send_parser = send_parser_notification
        self._send_trades = send_trades_notification
        self._signals_chat_id = getattr(scfg, "SIGNALS_CHAT_ID", 0)
        self._trades_chat_id = getattr(scfg, "TRADES_CHAT_ID", 0)
        
        # Post-init wiring for confirm bot (needs internal objects)
        if confirm_bot is not None:
            # Inject missing deps into TelegramConfirmBot
            if hasattr(confirm_bot, "confirmations") and confirm_bot.confirmations is None:
                confirm_bot.confirmations = self.confirmations
            if hasattr(confirm_bot, "on_decision") and confirm_bot.on_decision is None:
                confirm_bot.on_decision = self._execute_token

    async def _leaderboard_loop(self):
        while True:
            try:
                from .vip_reporter import get_minutes_to_next_anchor
                anchors = get_minutes_to_next_anchor()
                if anchors.get('DAILY_CLOSE', 999) <= 5:
                    rep = self.ch_perf.get_report()
                    await self._notify(f"🏆 <b>DAILY PERFORMANCE SUMMARY</b>\n\n{rep}")
                    await asyncio.sleep(600)
                await asyncio.sleep(60)
            except Exception:
                await asyncio.sleep(60)

    # ---------- notification helper ----------
    async def _notify(self, text: str) -> None:
        if self.notifier is None:
            logger.info("[SIGNAL_COPY][notify] %s", text.replace("\n", " | ")[:300])
            return
        try:
            res = self.notifier(text)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] notify failed: %s", exc)

    # --- Dual channel notification methods using new transports ---
    async def _notify_signals_channel(self, text: str) -> bool:
        """Send signal validation report to the signals channel (all verdicts)."""
        from .telegram_transport import send_parser_notification
        return await send_parser_notification(text)

    async def _notify_trades_channel(self, text: str) -> bool:
        """Send execution result to the trades channel (executed only)."""
        from .telegram_transport import send_trades_notification
        return await send_trades_notification(text)

    async def start_background_tasks(self):
        if self._reporter_task is None and self.vip_reporter is not None:
            self._reporter_task = asyncio.create_task(self.vip_reporter.loop())
            logger.info("[SIGNAL_COPY] Background reporter task started.")
        asyncio.create_task(self._leaderboard_loop())

    # ---------- accumulation route (Tahap 1 stub) ----------
    async def _handle_accumulation(self, text: str, source_name: str,
                                   source_chat_id: Optional[int], cls) -> None:
        """Route for whale-accumulation narratives.

        Tahap 1: record + (optionally) surface for visibility. It never trades —
        the real accumulation pipeline (find coins accumulated by whales, watch
        gradual accumulation, enter on confirmation) lands in Tahap 3.
        """
        from .parse_report import build_read_report
        logger.info("[ACCUM] %s", build_read_report(text, cls, None))
        try:
            self._accum_log.append({
                "text": (text or "")[:500],
                "source": source_name,
                "chat_id": source_chat_id,
                "reasons": list(cls.reasons),
                "ts": time.time(),
            })
            self._accum_log[:] = self._accum_log[-200:]
        except Exception:
            pass
        if getattr(scfg, "NOTIFY_ACCUM", False):
            preview = " ".join((text or "").split())[:200]
            await self._notify(
                f"📥 <b>Akumulasi terdeteksi</b> ({source_name or '-'})\n"
                f"<i>{preview}</i>\n"
                f"— belum dieksekusi (pipeline akumulasi menyusul)."
            )

    def _merge_vision(self, sig, v: dict) -> list:
        """Fill ONLY missing signal fields from chart-vision data (never override
        values the channel stated explicitly). Returns list of filled keys."""
        filled: list = []
        entry = sig.entry_mid or 0.0

        def _plausible(x) -> bool:
            try:
                x = float(x)
            except (TypeError, ValueError):
                return False
            if x <= 0:
                return False
            if entry <= 0:
                return True
            return entry * 0.1 <= x <= entry * 10.0

        sl = v.get("stop_loss")
        if sig.stop_loss is None and _plausible(sl):
            sig.stop_loss = float(sl)
            sig.sl_source = "vision"
            filled.append("sl")

        if not sig.take_profits:
            tps = [float(t) for t in (v.get("take_profits") or []) if _plausible(t)]
            if entry > 0:
                tps = [t for t in tps if (t > entry if sig.is_long else t < entry)]
            if tps:
                sig.take_profits = sorted(set(tps), reverse=not sig.is_long)
                sig.tp_source = "vision"
                filled.append("tp")

        if not getattr(sig, "timeframe", None) and v.get("timeframe"):
            sig.timeframe = str(v["timeframe"])
            filled.append("tf")

        return filled

    async def _signal_from_vision(self, image, *, text: str = "",
                                  source: SignalSource = SignalSource.TELEGRAM,
                                  source_name: str = "",
                                  source_chat_id: Optional[int] = None):
        """Build a ParsedSignal purely from a chart image when there is no
        parseable text. Needs at least a pair + side; entry falls back to the
        chart entry, else 0.0 (market) so the normalizer can fill from live price.
        Returns None if vision can't extract an actionable pair+side."""
        try:
            from .vision import analyze_chart
            v = await analyze_chart(image, symbol="", raw_text=text or "")
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] vision-only analyze failed: %s", exc)
            return None
        if not v:
            return None
        pair = (v.get("pair") or "").upper().strip()
        side_raw = (v.get("side") or "").upper().strip()
        if not pair or side_raw not in ("LONG", "SHORT"):
            return None
        from .signal_parser import _normalize_symbol
        symbol = _normalize_symbol(pair, None)
        if not symbol:
            return None

        def _num(x):
            try:
                x = float(x)
                return x if x > 0 else None
            except (TypeError, ValueError):
                return None

        entry = _num(v.get("entry")) or 0.0
        sl = _num(v.get("stop_loss"))
        tps = [t for t in (_num(x) for x in (v.get("take_profits") or [])) if t]
        sig = ParsedSignal(
            symbol=symbol,
            side=SignalSide(side_raw),
            entry_low=entry,
            entry_high=entry,
            stop_loss=sl,
            take_profits=tps,
            timeframe=(str(v["timeframe"]) if v.get("timeframe") else None),
            source=source,
            source_name=source_name,
            source_chat_id=source_chat_id,
            raw_text=(text or "")[:2000],
            tp_source="vision" if tps else "signal",
            sl_source="vision" if sl else "signal",
        )
        return sig

    async def read_only_report(self, text: str, source_name: str = "",
                               image: Optional[bytes] = None,
                               source: SignalSource = SignalSource.TELEGRAM) -> str:
        """Analyze a message WITHOUT executing: classify + parse + (optional)
        chart-vision read. Returns an HTML report. Used by the calibration bot
        and calibration Discord channels."""
        from .classifier import classify_message
        from .parse_report import build_read_report_html
        cls = classify_message(text or "")
        sig = parse_signal(text or "", source=source, source_name=source_name)
        vinfo = ""
        if image and getattr(scfg, "VISION_ENABLED", False):
            try:
                from .vision import analyze_chart
                vdata = await analyze_chart(image, symbol=(sig.symbol if sig else ""),
                                            raw_text=text or "")
                if vdata:
                    if sig is not None:
                        filled = self._merge_vision(sig, vdata)
                        vinfo += ("\n🖼️ Vision merge: "
                                  + (", ".join(filled) if filled else "(tak ada field kosong)"))
                    vinfo += (f"\n🖼️ Chart: pair={vdata.get('pair')} tf={vdata.get('timeframe')} "
                              f"side={vdata.get('side')} "
                              f"entry={vdata.get('entry')} sl={vdata.get('stop_loss')} "
                              f"tp={vdata.get('take_profits')} conf={(vdata.get('confidence') or 0):.0%}")
                else:
                    vinfo = "\n🖼️ Vision: tidak ada hasil"
            except Exception as exc:
                vinfo = f"\n🖼️ Vision gagal: {exc}"
        elif image:
            vinfo = "\n🖼️ (gambar diterima; VISION OFF)"
        return build_read_report_html(text or "", cls, sig) + vinfo

    # ---------- dedup ----------
    def _dedup_key(self, sig) -> str:
        return f"{sig.symbol}:{sig.side.value}:{round(sig.entry_mid, 6)}"

    def _is_duplicate(self, sig) -> bool:
        now = time.time()
        key = self._dedup_key(sig)
        # purge old
        for k in [k for k, ts in self._recent.items() if now - ts > scfg.DEDUP_WINDOW_SEC]:
            self._recent.pop(k, None)
        if key in self._recent and now - self._recent[key] < scfg.DEDUP_WINDOW_SEC:
            return True
        self._recent[key] = now
        return False

    # ---------- metrics ----------
    async def _fetch_metrics(self, symbol: str) -> Dict[str, Any]:
        if self.metrics_provider is None:
            return {}
        try:
            m = await self.metrics_provider.get_advanced_metrics(symbol)
            return m or {}
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] metrics fetch failed for %s: %s", symbol, exc)
            return {}

    async def _fetch_execution_quote(self, symbol: str) -> Dict[str, Any]:
        """Fresh exchange truth for route and pending lifecycle decisions."""
        from .fresh_quote import fetch_fresh_quote
        return await fetch_fresh_quote(symbol)

    async def _handle_provider_update(self, text: str, source_chat_id: Optional[int]) -> bool:
        from .provider_updates import UpdateKind, parse_provider_update
        update = parse_provider_update(text, source_chat_id)
        if update is None:
            return False
        matches = [(token, data) for token, data in self._pending_limits.items()
                   if data["result"].signal.symbol == update.symbol
                   and data["result"].signal.source_chat_id == source_chat_id]
        if update.kind == UpdateKind.CANCEL:
            for token, _ in matches:
                self._pending_limits.pop(token, None)
                await self.confirmations.mark(token, ConfirmState.EXPIRED, note="PROVIDER_CANCELLED")
            await self._notify_trades_channel(
                f"🚫 PROVIDER CANCEL {update.symbol}: pending dibatalkan={len(matches)}")
            return True
        client = getattr(self.trader, "client", None)
        if client is None:
            await self._notify_trades_channel(f"⚠️ PROVIDER UPDATE {update.symbol}: gateway unavailable")
            return True
        portfolio = await client.portfolio()
        positions = [p for p in portfolio.get("open_positions", []) if p.get("symbol") == update.symbol]
        if len(positions) != 1:
            await self._notify_trades_channel(
                f"⚠️ PROVIDER UPDATE {update.symbol}: no action, matching positions={len(positions)}")
            return True
        pos = positions[0]
        if update.kind in (UpdateKind.MOVE_SL_BE, UpdateKind.MOVE_SL_PRICE):
            new_sl = float(pos["entry_price"] if update.kind == UpdateKind.MOVE_SL_BE else (update.price or 0.0))
            result = await client.position_action(update.symbol, "MOVE_SL", new_sl)
        elif update.kind == UpdateKind.CLOSE:
            result = await client.position_action(update.symbol, "CLOSE")
        else:
            await self._notify_trades_channel(
                f"ℹ️ PROVIDER {update.kind.value} {update.symbol}: recorded; market verification required")
            return True
        await self._notify_trades_channel(
            f"{'✅' if result.get('ok') else '⚠️'} PROVIDER {update.kind.value} {update.symbol}: "
            f"{result.get('code', result)}")
        return True

    # ---------- main entry from listeners ----------
    async def handle_incoming_text(
        self,
        text: str,
        source_name: str = "",
        source_chat_id: Optional[int] = None,
        source: SignalSource = SignalSource.TELEGRAM,
        image: Optional[bytes] = None,
    ) -> None:
        # Never parse messages that originate from our own bot/components.
        if source_chat_id is not None and source_chat_id in self.ignore_chat_ids:
            return
        if await self._handle_provider_update(text, source_chat_id):
            return

        # Calibration channels: parse + validate + notify, but NEVER auto-execute.
        # Falls through to normal pipeline; execution is blocked later by
        # checking calib_channels before _execute_token.
        calib = source_chat_id is not None and source_chat_id in self.calib_channels

        # --- classify first (routing label + detailed read report) ---
        from .classifier import classify_message, MessageType
        from .parse_report import build_read_report, build_read_report_html
        cls = classify_message(text)

        sig = parse_signal(text, source=source, source_name=source_name, source_chat_id=source_chat_id)
        if sig is None and image and scfg.vision_enabled_for_channel(source_chat_id):
            # Chart-only / image-only signal: no parseable text, so read the
            # chart with vision and build the signal from what it extracts.
            # BUT skip the vision path entirely when the caption is
            # marketing/promo spam (guaranteed profits, join-now, invite links).
            from .classifier import looks_like_promo
            if not looks_like_promo(text or ""):
                sig = await self._signal_from_vision(
                    image, text=text, source=source,
                    source_name=source_name, source_chat_id=source_chat_id,
                )
                if sig is not None:
                    logger.info("[SIGNAL_COPY] vision-only signal built: %s", sig.summary())
            else:
                logger.info(
                    "[SIGNAL_COPY] promo/recruitment caption — vision path blocked: %s",
                    (text or "").replace("\n", "  ")[:120],
                )
        if sig is None:
            # Not a structured trade call — route by classification.
            if cls.type == MessageType.WHALE_ACCUM:
                await self._handle_accumulation(text, source_name, source_chat_id, cls)
            elif calib and image:
                # Calibration forward we couldn't read: still reply so the user
                # knows it was received (avoids silent no-response).
                await self._notify(
                    "🖼️ Chart diterima di channel kalibrasi tapi tidak bisa dibaca "
                    "(vision tidak mengembalikan pair/side/entry). Coba kirim chart "
                    "dengan pair & level yang jelas."
                )
            else:
                logger.info("[SIGNAL_COPY] %s", build_read_report(text, cls, None))
            return  # nothing to execute from a non-signal message

        # Structured trade call: emit a detailed read report (always logged;
        # optionally pushed to Telegram for calibration via SIGNAL_COPY_PARSE_REPORT).
        logger.info("[SIGNAL_COPY] %s", build_read_report(text, cls, sig))
        if not calib and self._is_duplicate(sig):
            logger.info("[SIGNAL_COPY] duplicate signal ignored: %s", sig.summary())
            from . import signal_lifecycle
            signal_lifecycle.record(sig.signal_id, "DUPLICATE", symbol=sig.symbol,
                                    side=sig.side.value, reason="duplicate signal ignored")
            return

        logger.info("[SIGNAL_COPY] parsed: %s", sig.summary())
        from . import signal_lifecycle
        signal_lifecycle.record(sig.signal_id, "PARSED", symbol=sig.symbol,
                                side=sig.side.value, source_chat_id=sig.source_chat_id)

        # (parse summary collected via build_parser_report after validation)

        # Learning mode: remember + surface the source channel id so the user can
        # add it to the allowlist. Notify once per new source.
        learning_banner = ""
        if self.learning_mode:
            cid = source_chat_id if source_chat_id is not None else 0
            if cid and cid not in self._known_signal_sources:
                self._known_signal_sources[cid] = source_name
                await self._notify(
                    f"🆕 <b>Channel sinyal baru terdeteksi</b>\n"
                    f"Nama: <b>{source_name or '-'}</b>\n"
                    f"ID: <code>{cid}</code>\n"
                    f"Tambahkan ke <code>SIGNAL_COPY_TG_CHANNELS</code> di .env "
                    f"jika ingin memfilter hanya channel ini."
                )
            if cid:
                learning_banner = (f"📡 Channel: {source_name or '-'} "
                                   f"(ID <code>{cid}</code>)\n")

        if not getattr(sig, "timeframe", None):
            sig.timeframe = "15m"
        metrics = await self._fetch_metrics(sig.symbol)
        # --- Multi-timeframe alignment: check 4h/daily trend structure ---
        try:
            from .mtf_aligner import MTFAligner
            mtf = MTFAligner()
            mtf_data = await mtf.analyze(sig.symbol)
            if mtf_data:
                mtf_score = await mtf.get_alignment_score(sig.symbol, sig.side.value)
                metrics["mtf_alignment"] = {
                    "score": mtf_score,
                    "entry_trend": mtf_data.get("entry_tf", {}).get("trend", "FLAT"),
                    "tf4h_trend": mtf_data.get("tf_4h", {}).get("trend", "FLAT"),
                    "d1_trend": mtf_data.get("tf_daily", {}).get("trend", "FLAT"),
                }
                logger.info("[MTF] %s side=%s mtf_score=%.1f 4h=%s d1=%s",
                           sig.symbol, sig.side.value, mtf_score,
                           metrics["mtf_alignment"]["tf4h_trend"],
                           metrics["mtf_alignment"]["d1_trend"])
        except Exception as exc:
            logger.debug("[MTF] analysis failed for %s: %s", sig.symbol, exc)

        # --- TradingView real-time indicator confluence ---
        try:
            from .tradingview_factor import TradingViewFactor
            tv = TradingViewFactor()
            tv_data = await tv.fetch(sig.symbol)
            if tv_data and tv_data.get("_ta"):
                tv_score = tv.compute_confluence(tv_data, sig.side.value)
                metrics["tradingview"] = tv_score
                logger.info("[TV] %s side=%s tv_score=%.1f",
                           sig.symbol, sig.side.value, tv_score.get("score", 0))
            else:
                logger.warning("[TV] no data returned for %s (rate limited?)", sig.symbol)
        except Exception as exc:
            logger.warning("[TV] fetch failed for %s: %s", sig.symbol, exc)

        # --- Tahap 2.5: read the chart image (vision) for EVERY image-bearing
        # signal: (a) fill missing TP/SL/timeframe, and (b) feed a chart
        # confluence factor into validation (agreement strengthens the score). ---
        if image and scfg.vision_enabled_for_channel(source_chat_id):
            try:
                from .vision import analyze_chart
                vdata = await analyze_chart(image, symbol=sig.symbol, raw_text=text)
                if vdata:
                    metrics["chart_vision"] = vdata
                    filled = self._merge_vision(sig, vdata)
                    if filled:
                        logger.info("[SIGNAL_COPY] vision filled %s -> %s", filled, sig.summary())
            except Exception as exc:
                logger.warning("[SIGNAL_COPY] vision enrich failed: %s", exc)

        # If the signal states a timeframe, recompute RSI/ATR/trend on that TF.
        if getattr(sig, "timeframe", None):
            try:
                from .timeframe import apply_timeframe_metrics
                await apply_timeframe_metrics(sig, metrics)
            except Exception as exc:
                logger.warning("[SIGNAL_COPY] timeframe metrics failed: %s", exc)
        # Make the signal executable even if the channel omitted TP/SL:
        # improvise SL from ATR and TP ladder as 1R/2R/3R from the stop.
        try:
            from .normalizer import normalize_signal
            normalize_signal(sig, metrics)
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] normalize failed: %s", exc)
        result = validate_signal(sig, metrics)
        logger.info("[SIGNAL_COPY] %s -> %s score=%.1f (tp=%s sl=%s entry=%s tf=%s) reasons=%s",
                    sig.symbol, result.verdict.value, result.score,
                    sig.tp_source, sig.sl_source, sig.entry_type, sig.timeframe or "-",
                    result.hard_blocks or [])
        signal_lifecycle.record(
            sig.signal_id,
            "VALIDATED" if result.verdict == Verdict.VALID else "VALIDATION_REJECTED",
            verdict=result.verdict.value, score=result.score,
            reason="; ".join(result.hard_blocks or []),
        )

        # --- Build ONE consolidated report and send ---
        from .telegram_formatter import build_parser_report
        _adv_verdict = ""
        chart_path = None

        # --- ADVERSARIAL CHECK (only for VALID signals) ---
        if result.verdict == Verdict.VALID and getattr(scfg, "ADVERSARIAL_ENABLED", True):
            try:
                import sys
                from pathlib import Path
                nexus_root = Path(__file__).parent.parent
                sys.path.insert(0, str(nexus_root))
                from fusionnew.clean_core.adversarial import bull_bear_check
                
                logger.info("[ADVERSARIAL] Running bull/bear debate for %s %s", sig.symbol, sig.side.value)
                
                mtf = metrics.get("mtf_alignment", {}) if isinstance(metrics.get("mtf_alignment"), dict) else {}
                tv = metrics.get("tradingview", {}) if isinstance(metrics.get("tradingview"), dict) else {}
                adv_context = {
                    "symbol": sig.symbol,
                    "side": sig.side.value,
                    "entry": sig.entry_mid,
                    "stop_loss": sig.stop_loss,
                    "take_profits": sig.take_profits,
                    "price": metrics.get("price", 0),
                    "timeframe": getattr(sig, "timeframe", None),
                    "leverage": getattr(sig, "leverage", None),
                    "rsi": metrics.get("rsi", 50),
                    "regime": metrics.get("regime_label", "UNKNOWN"),
                    "cvd_zscore": metrics.get("cvd_zscore", 0),
                    # Feed deterministic flow data already computed upstream.
                    "oi_change_5m_pct": metrics.get("oi_change_5m_pct"),
                    "oi_change_15m_pct": metrics.get("oi_change_15m_pct"),
                    "oi_change_1h_pct": metrics.get("oi_change_1h_pct"),
                    "funding_rate": metrics.get("funding_rate"),
                    "flow_direction": metrics.get("flow_direction"),
                    "qvol_5m": metrics.get("qvol_5m"),
                    "data_quality": metrics.get("data_quality"),
                    "mtf_score": mtf.get("score"),
                    "tv_score": tv.get("score"),
                    "validation_score": result.score,
                }

                approved, judge_verdict = await asyncio.to_thread(
                    bull_bear_check, sig.symbol, adv_context
                )

                _adv_mode = getattr(scfg, "ADVERSARIAL_MODE", "soft")
                _adv_floor = getattr(scfg, "ADVERSARIAL_SOFT_FLOOR", 75.0)
                if not approved:
                    _adv_verdict = judge_verdict
                    if _adv_mode == "hard":
                        # Legacy: judge NO hard-blocks the trade.
                        logger.warning("[ADVERSARIAL] REJECTED (hard) by judge: %s", judge_verdict[:200])
                        result.verdict = Verdict.REJECT
                        result.hard_blocks.append(f"Adversarial: {judge_verdict[:250]}")
                    elif _adv_mode == "off":
                        # Advisory only: never changes the verdict, just annotate.
                        logger.info("[ADVERSARIAL] NO (advisory, mode=off) — verdict kept: %s", judge_verdict[:200])
                    else:
                        # "soft" (default): high-conviction setups ride through; only
                        # weak-but-VALID ones get downgraded (not rejected) to WEAK.
                        if float(result.score) >= float(_adv_floor):
                            logger.info(
                                "[ADVERSARIAL] NO overridden — score=%.1f >= floor=%.1f, entry allowed: %s",
                                result.score, _adv_floor, judge_verdict[:160])
                        else:
                            logger.warning(
                                "[ADVERSARIAL] NO -> downgrade to WEAK (score=%.1f < floor=%.1f): %s",
                                result.score, _adv_floor, judge_verdict[:160])
                            result.verdict = Verdict.WEAK
                            # _adv_verdict (set above) carries the note into the
                            # consolidated report; no separate soft-flag field exists.
                else:
                    logger.info("[ADVERSARIAL] APPROVED: %s", judge_verdict[:200])
                   
            except Exception as exc:
                logger.warning("[ADVERSARIAL] Check failed (proceeding anyway): %s", exc)
        
        # --- Build ONE consolidated report and send ---
        consolidated = build_parser_report(
            sig, result, cls, source_name,
            calib=calib,
            adversarial_verdict=_adv_verdict,
        )
        try:
            from .chart_generator import build_chart
            chart_path = await build_chart(result)
        except Exception as exc:
            logger.warning("[SIGNAL_COPY] chart build failed: %s", exc)
        try:
            from .telegram_transport import send_parser_notification
            await send_parser_notification(consolidated, chart_path=chart_path)
        except Exception as exc:
            logger.error("❌ Consolidated notify failed: %s", exc)
        finally:
            if chart_path:
                try:
                    import os
                    os.remove(chart_path)
                except Exception:
                    pass

        # --- Snapshot for channel-quality learning (ALL verdicts w/ usable data) ---
        # Fire-and-forget execution means exits are never reported back, so we
        # track the channel's OWN entry/tp/sl call virtually — executed or not —
        # to build a fair per-channel track record. Ghosts (entry<=0 / no tp/sl /
        # already-past price) are filtered inside track().
        try:
            from .outcome_tracker import get_outcome_tracker
            _executed = (result.verdict == Verdict.VALID and not calib
                         and self.executor is not None and self.auto_execute)
            get_outcome_tracker().track(
                result.signal, metrics,
                verdict=result.verdict.value, execution_intended=bool(_executed),
            )
        except Exception as exc:
            logger.debug("[OUTCOME] snapshot skipped: %s", exc)

        # --- Stop processing for REJECT / WEAK ---
        if result.verdict == Verdict.REJECT or result.verdict == Verdict.WEAK:
            return

        # Calibration channel: validate only, never execute
        if calib:
            logger.info("[SIGNAL_COPY] calib channel — skip execution for %s", sig.symbol)
            signal_lifecycle.record(sig.signal_id, "CALIBRATION_ONLY")
            return

        if self.executor is None:
            signal_lifecycle.record(sig.signal_id, "NO_EXECUTOR")
            return  # consolidated report already sent above

        if self.auto_execute:
            await self.confirmations.register(result)
            await self.confirmations.resolve(result.signal.signal_id, approved=True)
            status = await self._execute_token(result.signal.signal_id)
            # Execution status sent via trades bot, not parser bot
            return
        signal_lifecycle.record(sig.signal_id, "AWAITING_CONFIRMATION")

        # Normal path: register + ask user to confirm via the bot
        await self.confirmations.register(result)
        if self.confirm_bot is not None:
            # Confirmation bot handles the interactive prompt
            pass

    # ---------- max-position gate (parse-but-don't-execute) ----------
    async def _at_max_positions(self) -> Tuple[bool, int, int]:
        """Return (at_max, open_count, max_allowed) from the LIVE gateway book.

        Fail-open: on any error / unknown count / no gateway (dry-run), returns
        (False, -1, cap) so a transient hiccup never blocks a fill. The gateway
        still enforces MAX_OPEN_POS_GLOBAL as a hard backstop regardless."""
        from .execution_config import MAX_OPEN_POS_GLOBAL
        cap = int(os.getenv("SIGNAL_COPY_MAX_RUNNING_POSITIONS", str(MAX_OPEN_POS_GLOBAL)))
        client = getattr(self.trader, "client", None)
        if client is None or not hasattr(client, "portfolio"):
            return (False, -1, cap)  # dry-run / no gateway -> gateway backstops
        try:
            snap = await client.portfolio()
            raw = snap.get("open_position_count")
            if raw is None:
                return (False, -1, cap)  # unknown -> fail-open
            cnt = int(raw)
            return (cnt >= cap, cnt, cap)
        except Exception as exc:
            logger.warning("[MAX_POS] portfolio check failed (%s) — fail-open", exc)
            return (False, -1, cap)

    async def _gate_on_max_positions(self, sig) -> bool:
        """True -> SKIP execution because the book is full. The signal has
        already been parsed/validated/reported; only the EXECUTION is skipped
        (the signal is never dropped). Sends a clear notice. Config-driven via
        SIGNAL_COPY_GATE_ON_MAX_POS (default True); fail-open on any error."""
        if not getattr(scfg, "GATE_EXEC_ON_MAX_POS", True):
            return False
        at_max, cnt, cap = await self._at_max_positions()
        pending = len(getattr(self, "_pending_limits", {}) or {})
        # Paper-mainnet collection cap applies to actual open positions only.
        if cnt >= 0:
            at_max = cnt >= cap
        if not at_max:
            setattr(sig, "_max_pos_notice_cap", None)
            return False
        # Pending poll runs every 15s. Notify once per continuous full-cap state,
        # not on every retry; execution remains WAITING_FOR_SLOT.
        if getattr(sig, "_max_pos_notice_cap", None) == cap:
            return True
        setattr(sig, "_max_pos_notice_cap", cap)
        logger.info("[MAX_POS] %s parsed but NOT executed: running book full (%d active; %d pending; cap=%d)",
                    sig.symbol, cnt, pending, cap)
        try:
            await self._notify_trades_channel(
                f"⏸️ {sig.symbol} {sig.side.value}: sinyal di-parse & valid, tapi "
                f"TIDAK dieksekusi — kapasitas running maksimal "
                f"({cnt}/{cap} posisi aktif; {pending} pending tidak memakai slot). "
                f"Sinyal tetap tercatat; eksekusi dilewati.")
        except Exception as exc:
            logger.error("[MAX_POS] notify failed: %s", exc)
        return True

    # ---------- execution path ----------
    async def _execute_token(self, signal_id: str) -> str:
        """Execute a confirmed signal (called by confirm bot or auto-exec)."""
        pc = await self.confirmations.get(signal_id)
        if pc is None:
            return f"Sinyal {signal_id} tidak ditemukan."
        if pc.state != ConfirmState.APPROVED:
            return f"Status sinyal: {pc.state.value}"

        if self.executor is None:
            return "Executor tidak tersedia."

        sig = pc.result.signal
        from . import signal_lifecycle
        signal_lifecycle.record(signal_id, "EXECUTION_INTENDED", execution_intended=True)

        from .entry_policy import EntryAction, route_entry
        live_metrics = await self._fetch_execution_quote(sig.symbol)
        if live_metrics.get("price_fresh") is not True:
            reason = str(live_metrics.get("price_source") or "FRESH_PRICE_UNAVAILABLE").upper()
            await self.confirmations.mark(signal_id, ConfirmState.FAILED, note=reason)
            signal_lifecycle.record(signal_id, "GATEWAY_REJECTED", gateway_accepted=False,
                                    position_confirmed=False, reason=reason,
                                    price_quote=live_metrics)
            msg = f"✅ VALIDATED tapi ❌ NOT FILLED {sig.side.value} {sig.symbol}\nReason: {reason}"
            await self._notify_trades_channel(msg)
            return msg
        price = _f(live_metrics.get("price"))
        pc.result.metrics_snapshot.update(live_metrics)
        logger.info("[EXEC_ROUTE] %s %s signal_id=%s price=%.8g source=%s",
                    sig.symbol, sig.side.value, signal_id, price,
                    live_metrics.get("price_source"))
        routing = route_entry(sig, price)
        if routing.action == EntryAction.REJECT:
            await self.confirmations.mark(signal_id, ConfirmState.EXPIRED, note=routing.code)
            from . import pending_journal
            pending_journal.record("ROUTE_REJECTED", sig, price=price, reason=routing.code,
                                   price_quote=live_metrics)
            logger.warning("[EXEC_ROUTE] rejected %s %s signal_id=%s reason=%s price=%.8g",
                           sig.symbol, sig.side.value, signal_id, routing.code, price)
            msg = f"✅ VALIDATED tapi ❌ NOT FILLED {sig.side.value} {sig.symbol}\nReason: {routing.code}"
            await self._notify_trades_channel(msg)
            return msg
        sig.active_entry = routing.entry
        # Entry-zone semantics: market only while current price is inside the
        # provider zone. Outside waits at the nearest boundary, including a
        # pullback after TP1; provider SL/TP levels remain unchanged.
        if routing.action == EntryAction.LIMIT:
            # Paper-mainnet hardened collection: pending setups are unlimited.
            # Capacity applies only when a setup becomes an actual position.
            self._pending_limits[signal_id] = {
                "result": pc.result,
                "created": time.time(),
                "path_high": price,
                "path_low": price,
            }
            from . import pending_journal
            pending_journal.record("PENDING", sig, price=price, boundary=routing.entry, reason=routing.code)
            await self.confirmations.mark(signal_id, ConfirmState.APPROVED, note="pending_limit")
            pending_msg = (
                f"⏳ LIMIT PENDING {sig.symbol} {sig.side.value} @ {routing.entry:g}. "
                f"Harga {price:g}; reason={routing.code}; "
                f"expiry={int(scfg.expiry_for_channel(getattr(sig, 'source_chat_id', None)) // 60)}m."
            )
            await self._notify_trades_channel(pending_msg)
            return pending_msg

        # Parse-but-don't-execute at max open positions: the signal is already
        # parsed/validated/reported; skip only the EXECUTION when the book is
        # full (config-driven, fail-open). Gateway is the hard backstop.
        if await self._gate_on_max_positions(sig):
            await self.confirmations.mark(
                signal_id, ConfirmState.EXPIRED, note="skipped_max_positions")
            return (f"⏸️ {sig.symbol} {sig.side.value}: sinyal tercatat, "
                    f"eksekusi dilewati (posisi maksimal).")

        # Limit setup: if price hasn't reached the entry yet, wait for it
        # (avoid chasing) — execute automatically when the price touches the limit.
        entry = getattr(sig, "active_entry", None) or sig.entry_mid
        _regime = (pc.result.metrics_snapshot or {}).get("regime_label", "")
        if self._wait_for_limit(sig, price, regime=_regime):
            self._pending_limits[signal_id] = {"result": pc.result, "created": time.time()}
            await self.confirmations.mark(
                signal_id,
                ConfirmState.APPROVED,
                note="pending_limit",
            )
            pending_msg = (
                f"⏳ Limit setup {sig.symbol} {sig.side.value} @ {entry:g}. "
                f"Harga sekarang {price:g} — menunggu harga menyentuh limit. "
                f"Akan dieksekusi otomatis saat tercapai (batas 1 jam)."
            )
            # Entry notification for the pending-limit path: the orchestrator
            # discards the returned string (auto_execute path), so send it here
            # or the user gets no entry confirmation until the limit fills.
            try:
                await self._notify_trades_channel(pending_msg)
            except Exception as exc:
                logger.error("❌ Pending-limit notify failed: %s", exc)
            return pending_msg

        outcome = await self.executor.execute(
            pc.result,
            dry_run=self.dry_run,
            risk_pct=self.sizer.calc(pc.result.signal, pc.result.metrics_snapshot or {})
        )
        await self.confirmations.mark(
            signal_id,
            ConfirmState.EXECUTED if outcome.position_confirmed else ConfirmState.FAILED,
            note=outcome.reason,
        )
        gateway_accepted = bool(getattr(outcome, "gateway_accepted", False))
        position_confirmed = bool(getattr(outcome, "position_confirmed", False))
        signal_lifecycle.record(
            signal_id,
            "POSITION_CONFIRMED" if position_confirmed else (
                "GATEWAY_ACCEPTED" if gateway_accepted else "GATEWAY_REJECTED"
            ),
            gateway_accepted=gateway_accepted,
            position_confirmed=position_confirmed,
            reason=outcome.reason,
            gateway_response=getattr(outcome, "raw", None),
        )
        if position_confirmed:
            # Position confirmed by gateway - execution successful
            # Bybit trader already sent entry notification via send_open_trade()
            # Build rich execution message for trades channel
            exec_payload = {
                "symbol": outcome.symbol,
                "side": outcome.side,
                "entry_price": outcome.entry_price,
                "notional": outcome.notional,
                "tp1": outcome.tp1,
                "tp_full": outcome.tp_full,
                "sl": outcome.sl_price,
                "risk_amount": outcome.risk_amount,
                "score": pc.result.score,
                "regime": "SIGNAL_COPY",
                "signal_id": pc.result.signal.signal_id,
            }
            # Add market data if available
            metrics = pc.result.metrics_snapshot or {}
            exec_payload.update({
                "price": metrics.get("price"),
                "cvd": metrics.get("cvd"),
                "oi_15m": metrics.get("oi_change_15m_pct") or metrics.get("oi_15m"),
                "oi_1h": metrics.get("oi_change_1h_pct") or metrics.get("oi_1h"),
                "funding": metrics.get("funding_rate") or metrics.get("funding_rate_pct") or metrics.get("funding"),
                "poc": metrics.get("poc") or metrics.get("poc_price"),
                "vol_ratio": metrics.get("vol_ratio"),
                "rsi": metrics.get("rsi"),
                "regime": metrics.get("regime_label") or "SIGNAL_COPY",
                "quadrant": metrics.get("quadrant") or "UNKNOWN",
            })
            
            # NOTE: Bybit trader already sends entry notification via send_open_trade()
            # No need to send duplicate [TRADES] notification here
            # Left commented for reference in case we need it for debugging
            
            # from signal_copy.telegram_transport import send_trades_notification
            # from signal_copy.telegram_formatter import build_execution_message
            # execution_msg = build_execution_message(outcome, pc.result.signal, pc.result)
            # try:
            #     await send_trades_notification(execution_msg)
            # except Exception as exc:
            #     logger.error(f"❌ Telegram trades notify failed: {exc}")
            
            return "OK"  # Execution successful, notification sent by trader
        
        # Failed execution: the parser card already said VALID; send the final
        # execution verdict so users can see why no position opened.
        from signal_copy.telegram_transport import send_trades_notification
        from signal_copy.telegram_formatter import build_execution_message
        execution_msg = build_execution_message(outcome, pc.result.signal, pc.result)
        try:
            await send_trades_notification(execution_msg)
        except Exception as exc:
            logger.error(f"❌ Telegram failed-exec notify failed: {exc}")
        return execution_msg

    @staticmethod
    def _entry_ref(sig) -> float:
        """Reference entry price used for both the wait decision and the
        limit-reached trigger. Prefers the active entry (closest to price)
        over the zone midpoint so RR/validation stay consistent."""
        return float(getattr(sig, "active_entry", None) or getattr(sig, "entry_mid", 0.0) or 0.0)

    def _wait_for_limit(self, sig, price: float, regime: str = "") -> bool:
        """Regime-aware entry-style decision. Returns True to HOLD as a pending
        limit (wait for pullback), False to fill NOW at market.

        Drift = how far price has run past the signal entry in the PROFIT
        direction (the 'chasing' scenario), measured in R (entry->SL distance):
          - Fresh   (<= ENTRY_DRIFT_FRESH_R): market now.
          - Lagging (fresh..ENTRY_DRIFT_MAX_R): market ONLY in a chase regime
            (trending); otherwise wait for a pullback.
          - Too far (> ENTRY_DRIFT_MAX_R): always wait for a pullback.
        Explicit limit-typed signals still wait until the entry is reached.
        Note: the executor re-sizes notional from the ACTUAL fill, so chasing
        keeps risk ~constant; the drift band protects R:R, not risk."""
        entry = self._entry_ref(sig)
        sl = float(getattr(sig, "stop_loss", 0.0) or 0.0)
        if price <= 0 or entry <= 0:
            return False

        # Explicit limit entry: wait until the entry price is touched.
        if getattr(sig, "entry_type", "market") == "limit":
            return not self._limit_reached(sig, price)

        # Drift-hold disabled (default) -> market signals fill NOW at market,
        # matching original behavior. Prevents fast scalp signals from being
        # parked as pending limits waiting for a pullback that never comes.
        if not getattr(scfg, "ENTRY_DRIFT_HOLD_ENABLED", False):
            return False

        # Market-typed: only consider waiting if price ran in the PROFIT
        # direction. At/better than entry -> fill now.
        drift_abs = (price - entry) if sig.is_long else (entry - price)
        if drift_abs <= 0:
            return False

        # Drift in R units (fallback to 1% proxy if SL missing/degenerate).
        r_dist = abs(entry - sl) if sl > 0 else entry * 0.01
        if r_dist <= 0:
            return False
        drift_r = drift_abs / r_dist

        fresh = float(getattr(scfg, "ENTRY_DRIFT_FRESH_R", 0.25))
        far = float(getattr(scfg, "ENTRY_DRIFT_MAX_R", 0.50))
        chase_regimes = getattr(scfg, "ENTRY_CHASE_REGIMES", {"TRENDING"})
        reg = str(regime or "").upper()

        if drift_r <= fresh:
            return False                    # fresh — market now
        if drift_r <= far:
            return reg not in chase_regimes # lagging — chase only in trend
        return True                         # too far — wait for pullback

    def _limit_reached(self, sig, price: float) -> bool:
        ref = self._entry_ref(sig)
        if sig.is_long:
            return price <= ref
        else:
            return price >= ref

    async def restore_pending_limits(self) -> tuple[int, int]:
        """Rebuild fresh dormant limits; terminalize stale rows after restart."""
        if self._pending_restored:
            return (0, 0)
        self._pending_restored = True
        from . import pending_journal
        from .validation_engine import ValidationResult, Verdict
        restored = expired = 0
        for row in pending_journal.load_latest_pending():
            try:
                sig = pending_journal.signal_from_row(row)
                expiry = scfg.expiry_for_channel(sig.source_chat_id)
                age = pending_journal.row_age_seconds(row)
                if age > expiry:
                    pending_journal.record("EXPIRED_ON_RESTART", sig, age_sec=int(age), expiry_sec=int(expiry))
                    expired += 1
                    continue
                result = ValidationResult(signal=sig, verdict=Verdict.VALID,
                                          score=float(row.get("score") or 0.0),
                                          metrics_snapshot={})
                pc = await self.confirmations.register(result, expires_in=max(1.0, expiry - age))
                await self.confirmations.mark(sig.signal_id, ConfirmState.APPROVED,
                                              note="restored_pending_limit")
                self._pending_limits[sig.signal_id] = {
                    "result": result,
                    "created": time.time() - age,
                    "path_high": float(row.get("path_high") or row.get("price") or sig.active_entry or sig.entry_mid),
                    "path_low": float(row.get("path_low") or row.get("price") or sig.active_entry or sig.entry_mid),
                }
                restored += 1
            except Exception as exc:
                logger.warning("[PENDING] restore skipped row %s: %s", row.get("signal_id"), exc)
        logger.info("[PENDING] restart reconciliation restored=%d expired=%d", restored, expired)
        return restored, expired

    # ---------- public: handle pending limits (call periodically) ----------
    async def check_pending_limits(self) -> None:
        """Poll pending limits and execute when price reaches the zone."""
        await self.restore_pending_limits()
        for token, data in list(self._pending_limits.items()):
            pc = data.get("result")
            if not pc:
                continue
            sig = pc.signal
            # Per-channel expiry (Opsi A): scalp/standard/swing by source channel.
            _expiry = scfg.expiry_for_channel(getattr(sig, "source_chat_id", None))
            if time.time() - float(data.get("created", 0.0)) > _expiry:
                self._pending_limits.pop(token, None)
                from . import pending_journal
                pending_journal.record("EXPIRED", sig, expiry_sec=int(_expiry),
                                       path_high=data.get("path_high"), path_low=data.get("path_low"))
                await self.confirmations.mark(
                    token, ConfirmState.EXPIRED,
                    note=f"limit_expired:{int(_expiry)}s")
                await self._notify_trades_channel(
                    f"⌛ LIMIT EXPIRED {sig.symbol} {sig.side.value} @ {getattr(sig, 'active_entry', sig.entry_mid):g} "
                    f"({int(_expiry // 60)}m tanpa fill)")
                continue
            metrics = await self._fetch_execution_quote(sig.symbol)
            price = _f(metrics.get("price"))
            if price <= 0 or metrics.get("price_fresh") is not True:
                logger.warning("[PENDING] skip stale/unavailable price %s source=%s age=%s",
                               sig.symbol, metrics.get("price_source"), metrics.get("price_age_sec"))
                continue
            data["path_high"] = max(float(data.get("path_high", price)), price)
            data["path_low"] = min(float(data.get("path_low", price)), price)
            if self._limit_reached(sig, price):
                from .entry_policy import pending_fill_allowed
                allowed, drift_r = pending_fill_allowed(sig, price)
                boundary = float(getattr(sig, "active_entry", None) or sig.entry_mid)
                adverse = price < boundary if sig.is_long else price > boundary
                if adverse and not allowed:
                    from . import pending_journal
                    pending_journal.record("DRIFT_WAITING", sig, price=price,
                                           drift_r=round(drift_r, 4))
                    continue
                # Thesis-aware revalidation: historical SL/effective-target
                # invalidation is permanent. Snapshot indicator rescoring is
                # intentionally not a single-factor veto on a provider limit.
                if getattr(scfg, "ENTRY_REVALIDATE_ON_FILL", True):
                    try:
                        from .entry_policy import PricePath, revalidate_pending
                        revalid = revalidate_pending(
                            sig,
                            PricePath(high=data["path_high"], low=data["path_low"]),
                            price,
                        )
                        if not revalid.ok:
                            self._pending_limits.pop(token, None)
                            from . import pending_journal
                            pending_journal.record(
                                "THESIS_REJECTED", sig, price=price,
                                path_high=data["path_high"], path_low=data["path_low"],
                                reason=revalid.code,
                            )
                            logger.warning(
                                "[PENDING] rejected %s %s signal_id=%s reason=%s "
                                "price=%.8g path_high=%.8g path_low=%.8g",
                                sig.symbol, sig.side.value, token, revalid.code, price,
                                data["path_high"], data["path_low"],
                            )
                            await self.confirmations.mark(
                                token, ConfirmState.EXPIRED, note=revalid.code)
                            await self._notify_trades_channel(
                                f"✅ VALID tapi ❌ NOT EXECUTED {sig.side.value} {sig.symbol}\n"
                                f"Reason: {revalid.code}")
                            continue
                        pc.metrics_snapshot = metrics
                    except Exception as exc:
                        logger.warning("[PENDING] re-validation error for %s: %s "
                                       "(proceeding with entry)", sig.symbol, exc)
                # Capacity applies at actual entry. Pending itself uses no slot.
                if await self._gate_on_max_positions(sig):
                    from . import pending_journal
                    pending_journal.record("WAITING_FOR_SLOT", sig, price=price,
                                           reason="MAX_OPEN_POSITIONS")
                    await self.confirmations.mark(token, ConfirmState.APPROVED,
                                                  note="WAITING_FOR_SLOT")
                    continue
                await self.confirmations.mark(
                    token,
                    ConfirmState.APPROVED,
                    note="limit_reached",
                )
                outcome = await self.executor.execute(
                    pc,
                    dry_run=self.dry_run,
                    risk_pct=self.sizer.calc(pc.signal, pc.metrics_snapshot or {}),
                )
                from . import pending_journal
                if "MAX_OPEN" in str(outcome.reason).upper():
                    pending_journal.record("WAITING_FOR_SLOT", sig, price=price,
                                           reason=outcome.reason)
                    await self.confirmations.mark(token, ConfirmState.APPROVED,
                                                  note="WAITING_FOR_SLOT")
                    continue
                self._pending_limits.pop(token, None)
                await self.confirmations.mark(
                    token,
                    ConfirmState.EXECUTED if outcome.ok else ConfirmState.FAILED,
                    note=outcome.reason,
                )
                pending_journal.record(
                    "FILLED" if outcome.position_confirmed else "GATEWAY_REJECTED", pc.signal,
                    price=price, fill=outcome.entry_price, notional=outcome.notional,
                    risk_amount=outcome.risk_amount, drift_r=round(drift_r, 4),
                    rr_tp1=getattr(outcome, "rr_tp1", 0.0), reason=outcome.reason,
                    gateway_accepted=bool(outcome.gateway_accepted),
                    position_confirmed=bool(outcome.position_confirmed),
                    gateway_response=getattr(outcome, "raw", None),
                )
                if outcome.position_confirmed and outcome.notional > 0:
                    exec_payload = {
                        "symbol": outcome.symbol,
                        "side": outcome.side,
                        "entry_price": outcome.entry_price,
                        "notional": outcome.notional,
                        "tp1": outcome.tp1,
                        "tp_full": outcome.tp_full,
                        "sl": outcome.sl_price,
                        "risk_amount": outcome.risk_amount,
                        "score": pc.score,
                        "regime": "SIGNAL_COPY",
                        "signal_id": pc.signal.signal_id,
                    }
                    metrics = pc.metrics_snapshot or {}
                    exec_payload.update({
                        "price": metrics.get("price"),
                        "cvd": metrics.get("cvd"),
                        "oi_15m": metrics.get("oi_change_15m_pct") or metrics.get("oi_15m"),
                        "oi_1h": metrics.get("oi_change_1h_pct") or metrics.get("oi_1h"),
                        "funding": metrics.get("funding_rate") or metrics.get("funding_rate_pct") or metrics.get("funding"),
                        "poc": metrics.get("poc") or metrics.get("poc_price"),
                        "vol_ratio": metrics.get("vol_ratio"),
                        "rsi": metrics.get("rsi"),
                        "regime": metrics.get("regime_label") or "SIGNAL_COPY",
                        "quadrant": metrics.get("quadrant") or "UNKNOWN",
                    })
                    
                    execution_msg = build_execution_message(outcome, pc.signal, pc)
                    await self._notify_trades_channel(execution_msg)