import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter, defaultdict

print("==========================================================================")
print("=== AUDIT TELITI TELEGRAM SIGNALS: PURE RR (1R -> BEP, 2R -> TP 75%) ===")
print("==========================================================================\n")

# Datetime cutoff: Let's inspect all signals from the past 7 days (or since Oct 15)
START_EPOCH = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc).timestamp()

decisions_path = Path("/opt/signalcopyreal/runtime/state/committee_decisions.jsonl")
outcomes_path = Path("/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl")
pending_path = Path("/opt/signalcopyreal/runtime/state/pending_lifecycle.jsonl")
lifecycle_path = Path("/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl")
outcomes_json = Path("/opt/signalcopyreal/runtime/state/signal_outcomes.json")

signals = {}

# 1. Load from pending_lifecycle.jsonl (contains rich raw signal params)
if pending_path.exists():
    with pending_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                sid = row.get("signal_id")
                if not sid:
                    continue
                ts_str = row.get("ts")
                if ts_str:
                    try:
                        ts_epoch = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts_epoch = row.get("created_epoch", 0)
                else:
                    ts_epoch = row.get("created_epoch", 0)
                
                if ts_epoch < START_EPOCH:
                    continue
                
                entry = row.get("active_entry") or row.get("entry_low") or row.get("price")
                sl = row.get("stop_loss")
                if sid not in signals:
                    signals[sid] = {
                        "signal_id": sid,
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "ts_epoch": ts_epoch,
                        "entry": float(entry) if entry else None,
                        "sl": float(sl) if sl else None,
                        "tps": row.get("take_profits", []),
                        "source": "pending_journal"
                    }
                else:
                    if entry and not signals[sid]["entry"]:
                        signals[sid]["entry"] = float(entry)
                    if sl and not signals[sid]["sl"]:
                        signals[sid]["sl"] = float(sl)
            except Exception:
                pass

print(f"Loaded {len(signals)} signals from pending_lifecycle.jsonl")

# 2. Load from signal_outcomes.json (active/recent store)
if outcomes_json.exists():
    try:
        with outcomes_json.open("r", encoding="utf-8") as f:
            data = json.load(f)
            for sid, snap in data.items():
                ts_epoch = snap.get("created_at", 0)
                if ts_epoch < START_EPOCH:
                    continue
                if sid not in signals:
                    signals[sid] = {
                        "signal_id": sid,
                        "symbol": snap.get("symbol"),
                        "side": snap.get("side"),
                        "ts_epoch": ts_epoch,
                        "entry": float(snap.get("entry") or 0.0),
                        "sl": float(snap.get("sl") or 0.0),
                        "tps": snap.get("tps", []),
                        "score": snap.get("validation_score"),
                        "committee": snap.get("adversarial_committee"),
                        "source": "signal_outcomes_json"
                    }
                else:
                    if snap.get("entry") and not signals[sid]["entry"]:
                        signals[sid]["entry"] = float(snap["entry"])
                    if snap.get("sl") and not signals[sid]["sl"]:
                        signals[sid]["sl"] = float(snap["sl"])
                    if snap.get("validation_score"):
                        signals[sid]["score"] = snap.get("validation_score")
                    if snap.get("adversarial_committee"):
                        signals[sid]["committee"] = snap.get("adversarial_committee")
    except Exception as e:
        print(f"Error loading signal_outcomes.json: {e}")

# 3. Load / Enrich from committee_decisions.jsonl
if decisions_path.exists():
    with decisions_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                sid = row.get("signal_id")
                ts_epoch = row.get("ts", 0)
                if ts_epoch < START_EPOCH:
                    continue
                
                committee = row.get("committee") or {}
                votes = committee.get("votes", [])
                
                entry_evidence = {}
                geom_evidence = {}
                for v in votes:
                    if v.get("specialist") == "entry_freshness":
                        entry_evidence = v.get("evidence", {})
                    elif v.get("specialist") == "geometry_rr":
                        geom_evidence = v.get("evidence", {})
                
                side = row.get("side")
                market_price = (row.get("market") or {}).get("price")
                entry_val = entry_evidence.get("entry") or entry_evidence.get("price") or market_price
                sl_pct = geom_evidence.get("sl_pct")
                
                sl_val = None
                if entry_val and sl_pct:
                    try:
                        entry_val = float(entry_val)
                        sl_pct = float(sl_pct)
                        if side == "LONG":
                            sl_val = entry_val * (1.0 - (sl_pct / 100.0))
                        else:
                            sl_val = entry_val * (1.0 + (sl_pct / 100.0))
                    except Exception:
                        pass
                        
                if sid not in signals:
                    signals[sid] = {
                        "signal_id": sid,
                        "symbol": row.get("symbol"),
                        "side": side,
                        "ts_epoch": ts_epoch,
                        "entry": entry_val,
                        "sl": sl_val,
                        "score": row.get("validation_score"),
                        "verdict": row.get("validation_verdict"),
                        "committee": committee,
                        "source": "committee_decisions"
                    }
                else:
                    if not signals[sid].get("entry") and entry_val:
                        signals[sid]["entry"] = entry_val
                    if not signals[sid].get("sl") and sl_val:
                        signals[sid]["sl"] = sl_val
                    signals[sid]["score"] = row.get("validation_score")
                    signals[sid]["verdict"] = row.get("validation_verdict")
                    signals[sid]["committee"] = committee
            except Exception:
                pass

print(f"Total unique Telegram signals aggregated: {len(signals)}")

# Filter signals with valid symbol, side, entry, and sl
valid_signals = []
for sid, s in signals.items():
    sym = s.get("symbol")
    side = s.get("side")
    entry = s.get("entry")
    sl = s.get("sl")
    if sym and side in ("LONG", "SHORT") and entry and sl:
        try:
            entry = float(entry)
            sl = float(sl)
            risk = abs(entry - sl)
            # Sanity checks
            if risk > 0 and (risk / entry) < 0.20 and (risk / entry) > 0.002:
                s["entry"] = entry
                s["sl"] = sl
                s["risk"] = risk
                s["risk_pct"] = (risk / entry) * 100.0
                valid_signals.append(s)
        except Exception:
            pass

print(f"Signals with valid geometry for price tracking: {len(valid_signals)}")

# 4. Fetch klines via local FastAPI or fallback Bybit Linear REST
def get_klines_bybit_or_binance(symbol, start_ms, limit=120):
    # Try local FastAPI first
    url = f"http://127.0.0.1:8000/klines/bybit/{symbol}?tf=15m&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            raw_candles = data.get("data", [])
            if raw_candles:
                # Format: [{"open_time": "...", "open": ..., "high": ..., "low": ..., "close": ...}]
                parsed = []
                for c in raw_candles:
                    ot_str = c.get("open_time")
                    ot_epoch = datetime.fromisoformat(ot_str.replace("Z", "+00:00")).timestamp() * 1000
                    parsed.append((ot_epoch, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])))
                return sorted(parsed, key=lambda x: x[0])
    except Exception:
        pass

    # Try Binance REST
    try:
        url_bin = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=15m&startTime={int(start_ms)}&limit={limit}"
        req = urllib.request.Request(url_bin, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = json.loads(resp.read().decode())
            if isinstance(raw, list) and raw:
                return [(float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in raw]
    except Exception:
        pass

    # Try Bybit Linear REST
    try:
        url_bybit = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=15&start={int(start_ms)}&limit={limit}"
        req = urllib.request.Request(url_bybit, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = json.loads(resp.read().decode())
            candles = raw.get("result", {}).get("list", [])
            if candles:
                parsed = [(float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in candles]
                return sorted(parsed, key=lambda x: x[0])
    except Exception:
        pass

    return []

# 5. Simulate each signal:
# Rules:
# - Target 1: 1.0R (entry +/- 1*risk). If hit -> move SL to entry (BEP). Trade is RISK-FREE.
# - Target 2: 2.0R (entry +/- 2*risk). If hit -> Take Profit 75% at 2.0R (+1.50R locked). Remaining 25% runner trails.
# - If price hits SL before 1.0R -> Loss -1.0R.
# - If price hits 1.0R but reverses to entry before 2.0R -> Exit at BEP (+0.0R, scratch).

print("\nTracking price excursion candle by candle for all valid signals...")
cached_klines = {}
sim_results = []

for s in valid_signals:
    sym = s["symbol"]
    side = s["side"]
    entry = s["entry"]
    sl = s["sl"]
    risk = s["risk"]
    ts_epoch = s["ts_epoch"]
    start_ms = (ts_epoch - 300) * 1000  # 5 min before signal
    
    if sym not in cached_klines:
        cached_klines[sym] = get_klines_bybit_or_binance(sym, start_ms=start_ms, limit=120)
        
    all_candles = cached_klines[sym]
    if not all_candles:
        continue
        
    post_candles = [c for c in all_candles if c[0] >= (ts_epoch - 900) * 1000]
    if len(post_candles) < 2:
        continue
        
    tp1_price = entry + risk if side == "LONG" else entry - risk
    tp2_price = entry + (2.0 * risk) if side == "LONG" else entry - (2.0 * risk)
    tp3_price = entry + (3.0 * risk) if side == "LONG" else entry - (3.0 * risk)

    hit_1r = False
    hit_2r = False
    hit_3r = False
    hit_sl = False
    hit_bep = False
    max_mfe_r = 0.0
    outcome = "UNKNOWN"

    for c in post_candles:
        ot, o, h, l, cl = c
        if side == "LONG":
            cur_mfe = (h - entry) / risk
        else:
            cur_mfe = (entry - l) / risk
            
        if cur_mfe > max_mfe_r:
            max_mfe_r = cur_mfe

        if not hit_1r:
            is_sl = (l <= sl) if side == "LONG" else (h >= sl)
            is_1r = (h >= tp1_price) if side == "LONG" else (l <= tp1_price)
            if is_sl and not is_1r:
                hit_sl = True
                outcome = "DIRECT_SL"
                break
            elif is_1r:
                hit_1r = True
                # SL moves to BEP

        if hit_1r and not hit_2r:
            is_bep = (l <= entry) if side == "LONG" else (h >= entry)
            is_2r = (h >= tp2_price) if side == "LONG" else (l <= tp2_price)
            if is_2r:
                hit_2r = True
                outcome = "HIT_2R"
                # Check if it reached 3R+
                is_3r = (h >= tp3_price) if side == "LONG" else (l <= tp3_price)
                if is_3r:
                    hit_3r = True
                break
            elif is_bep:
                hit_bep = True
                outcome = "REVERSED_TO_BEP"
                break

    if outcome == "UNKNOWN":
        if hit_2r: outcome = "HIT_2R"
        elif hit_1r: outcome = "REVERSED_TO_BEP" # conservative: assumed stopped at BEP
        elif hit_sl: outcome = "DIRECT_SL"
        else: outcome = "IN_PROGRESS"

    s_res = dict(s)
    s_res["max_mfe_r"] = max_mfe_r
    s_res["hit_1r"] = hit_1r
    s_res["hit_2r"] = hit_2r
    s_res["hit_3r"] = hit_3r
    s_res["hit_bep"] = hit_bep
    s_res["hit_sl"] = hit_sl
    s_res["outcome"] = outcome
    sim_results.append(s_res)

print(f"Evaluated {len(sim_results)} signals successfully with historical price action!\n")

def print_group_analysis(label, dataset):
    print(f"==========================================================================")
    print(f"=== {label} (N = {len(dataset)}) ===")
    print(f"==========================================================================")
    if not dataset:
        print("No signals in this group.\n")
        return
        
    cnt = Counter(r["outcome"] for r in dataset)
    n = len(dataset)
    for oc, c in cnt.most_common():
        print(f"  {oc:20}: {c:3} ({c/n*100:.1f}%)")
        
    r_1r = sum(1 for r in dataset if r["hit_1r"])
    r_2r = sum(1 for r in dataset if r["hit_2r"])
    r_3r = sum(1 for r in dataset if r["hit_3r"])
    r_bep = sum(1 for r in dataset if r["outcome"] == "REVERSED_TO_BEP")
    r_sl = sum(1 for r in dataset if r["outcome"] == "DIRECT_SL")
    
    print(f"\n  Milestones Breakdown:")
    print(f"  * Reaching >= 1.0R (Risk-Free BEP): {r_1r:3} ({r_1r/n*100:.1f}%)")
    print(f"  * Reaching >= 2.0R (TP 75% Hit):    {r_2r:3} ({r_2r/n*100:.1f}%)")
    print(f"  * Reaching >= 3.0R (Runner Bonanza):{r_3r:3} ({r_3r/n*100:.1f}%)")
    print(f"  * Stopped at BEP ($0 loss):         {r_bep:3} ({r_bep/n*100:.1f}%)")
    print(f"  * Direct SL (-1.0R loss):           {r_sl:3} ({r_sl/n*100:.1f}%)")
    
    if r_1r > 0:
        conv = (r_2r / r_1r) * 100.0
        print(f"  * CONVERSION 1.0R -> 2.0R:           {conv:.1f}%")
        
    # Financial Expectancy on proposed TP/SL:
    # 2R Hit: 75% @ 2R (+1.50R) + 25% trailing (~2.0R) = +2.00R (minimum)
    # BEP: $0.00 (Risk-Free)
    # SL: -1.00R
    net_r = (r_2r * 2.00) + (r_bep * 0.0) - (r_sl * 1.00)
    profit_per_trade = net_r / n
    risk_usd = 2.00
    net_usd = net_r * risk_usd
    
    print(f"\n  FINANCIAL EXPECTANCY (Risk = ${risk_usd:.2f} per trade):")
    print(f"  * Total R-Multiple Net: {net_r:+.2f}R")
    print(f"  * Expectancy Per Trade: {profit_per_trade:+.2f}R")
    print(f"  * Net Profit ($):       ${net_usd:+.2f} USD")
    print(f"  * Profit Factor:        {(r_2r * 2.0) / (r_sl * 1.0) if r_sl > 0 else 999.0:.2f}")
    print()

# Group 1: All Telegram Signals (No filters)
print_group_analysis("GROUP 1: ALL TELEGRAM SIGNALS (UNFILTERED)", sim_results)

# Group 2: Liquid Pairs Whitelist (Anti-Shitcoin)
LOW_LIQUID = {"AKEUSDT", "GIGGLEUSDT", "EPICUSDT", "CAPUSDT", "KAITOUSDT", "BANKUSDT", "CYSUSDT", "BEATUSDT", "COTIUSDT", "UAIUSDT", "BTWUSDT", "BLESSUSDT", "USELESSUSDT", "APRUSDT"}
liquid_signals = [r for r in sim_results if r["symbol"] not in LOW_LIQUID]
print_group_analysis("GROUP 2: LIQUID PAIRS ONLY (ANTI-SHITCOIN)", liquid_signals)

# Group 3: Valid ML Score + Adversarial Committee Approved (The User's Proposed Standard)
# Score >= 65 and Committee != 'NO' (i.e. YES or WARN, no hard veto)
def is_adversarial_ok(r):
    comm = r.get("committee") or {}
    final_vote = comm.get("final_vote")
    no_votes = comm.get("no_votes", 0)
    # If committee evaluated, must not have final_vote == 'NO' or no_votes >= 2
    if final_vote == "NO" or no_votes >= 2:
        return False
    return True

adv_supported = [r for r in liquid_signals if is_adversarial_ok(r)]
print_group_analysis("GROUP 3: LIQUID PAIRS + ADVERSARIAL COMMITTEE APPROVED", adv_supported)

# Group 4: Score >= 65 + Adversarial Committee Approved
high_quality = [r for r in adv_supported if (r.get("score") or 0.0) >= 65.0]
if len(high_quality) >= 3:
    print_group_analysis("GROUP 4: HIGH SCORE (>=65) + ADVERSARIAL COMMITTEE APPROVED", high_quality)

# Display Top Winners reaching 2.0R+
winners = [r for r in liquid_signals if r["hit_2r"]]
print(f"==========================================================================")
print(f"=== DAFTAR CONTOH SINYAL TELEGRAM YANG BERHASIL MENEMBUS 2.0R+ ===")
print(f"==========================================================================")
for w in winners[:15]:
    dt = datetime.fromtimestamp(w["ts_epoch"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    mfe = w["max_mfe_r"]
    score = w.get("score", "-")
    score_str = f"{score:.1f}" if isinstance(score, (int, float)) else str(score)
    print(f"  [{dt}] {w['symbol']:12} {w['side']:5} | Entry: {w['entry']:.5g} | SL: {w['sl']:.5g} (Risk: {w['risk_pct']:.2f}%) | Max Excursion: +{mfe:.2f}R | Score: {score_str}")


print("==========================================================================")
print("=== ANALISIS SENSITIVITAS SCORE THRESHOLD TERHADAP WIN RATE & EXPECTANCY ===")
print("==========================================================================")

for min_score in [0, 50, 55, 60, 65, 70]:
    subset = [r for r in liquid_signals if (r.get("score") or 0.0) >= min_score and is_adversarial_ok(r)]
    if not subset:
        continue
    n = len(subset)
    r_1r = sum(1 for r in subset if r["hit_1r"])
    r_2r = sum(1 for r in subset if r["hit_2r"])
    r_bep = sum(1 for r in subset if r["outcome"] == "REVERSED_TO_BEP")
    r_sl = sum(1 for r in subset if r["outcome"] == "DIRECT_SL")
    
    net_r = (r_2r * 2.00) + (r_bep * 0.0) - (r_sl * 1.00)
    pf = (r_2r * 2.0) / (r_sl * 1.0) if r_sl > 0 else 999.0
    
    print(f"Threshold Score >= {min_score:2d} | Trades: {n:3d} | Reached 1R: {r_1r/n*100:4.1f}% | Hit 2R: {r_2r/n*100:4.1f}% | SL: {r_sl/n*100:4.1f}% | Net: {net_r:+5.1f}R (${net_r*2.0:+6.1f}) | PF: {pf:.2f}")


print("==========================================================================")
print("=== PENGARUH FILTER 1 (MACRO GUARD SHORT) PADA SINYAL TELEGRAM ===")
print("==========================================================================")

# Check side distribution of direct SLs in liquid signals
sl_trades = [r for r in liquid_signals if r["outcome"] == "DIRECT_SL"]
sl_sides = Counter(r["side"] for r in sl_trades)
print(f"Direct SL Breakdown: SHORT={sl_sides['SHORT']} ({sl_sides['SHORT']/len(sl_trades)*100:.1f}%), LONG={sl_sides['LONG']} ({sl_sides['LONG']/len(sl_trades)*100:.1f}%)")

hit2r_trades = [r for r in liquid_signals if r["outcome"] == "HIT_2R"]
hit2r_sides = Counter(r["side"] for r in hit2r_trades)
print(f"Hit 2R Breakdown:    SHORT={hit2r_sides['SHORT']} ({hit2r_sides['SHORT']/len(hit2r_trades)*100:.1f}%), LONG={hit2r_sides['LONG']} ({hit2r_sides['LONG']/len(hit2r_trades)*100:.1f}%)")

# Filter out SHORTs (or evaluate LONGs only during BTC Bullish run)
longs_only = [r for r in liquid_signals if r["side"] == "LONG"]
print_group_analysis("SUBGROUP: LONG SIGNALS ONLY (ALIGNED WITH BTC BULLISH MACRO)", longs_only)

shorts_only = [r for r in liquid_signals if r["side"] == "SHORT"]
print_group_analysis("SUBGROUP: SHORT SIGNALS ONLY (COUNTER-TREND)", shorts_only)
