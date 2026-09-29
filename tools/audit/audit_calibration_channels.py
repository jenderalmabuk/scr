import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta

# 1. Read channel names from Telethon session
channel_names = {}
session_path = "/opt/signalcopyreal/runtime/state/signal_copy_session.session"
if os.path.exists(session_path):
    try:
        conn = sqlite3.connect(session_path)
        cur = conn.cursor()
        cur.execute("SELECT id, username, name FROM entities;")
        for r in cur.fetchall():
            raw_id = r[0]
            uname = r[1] or ""
            name = r[2] or uname or f"Channel_{raw_id}"
            
            # Format possible keys: raw_id, -100{raw_id}, -{raw_id}
            channel_names[str(raw_id)] = name
            channel_names[f"-100{raw_id}"] = name
            channel_names[f"-{raw_id}"] = name
    except Exception as e:
        print("Note on session db:", e)

# 2. Read .env for calibration and live channels
calib_ids = []
live_ids = []
env_path = "/opt/signalcopyreal/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("SIGNAL_COPY_CALIBRATION_CHANNELS="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                calib_ids = [c.strip() for c in val.split(",") if c.strip()]
            elif line.startswith("SIGNAL_COPY_TG_CHANNELS="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                live_ids = [c.strip() for c in val.split(",") if c.strip()]

# Add manual test channel if not present
if "-1003988458515" not in calib_ids:
    calib_ids.append("-1003988458515")

# 3. Read channel_performance.json
perf_path = "/opt/signalcopyreal/runtime/state/channel_performance.json"
perf_data = {}
if os.path.exists(perf_path):
    try:
        with open(perf_path) as f:
            perf_data = json.load(f)
    except Exception as e:
        print("Error reading channel_performance.json:", e)

# 4. Read committee_outcomes.jsonl or signal_outcomes.json to see recent outcomes
outcomes_path = "/opt/signalcopyreal/runtime/state/committee_outcomes.jsonl"
recent_channel_outcomes = {}
if os.path.exists(outcomes_path):
    try:
        with open(outcomes_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    cid = str(row.get("channel_id") or row.get("source_chat_id") or "")
                    if cid not in recent_channel_outcomes:
                        recent_channel_outcomes[cid] = []
                    recent_channel_outcomes[cid].append(row)
                except Exception:
                    pass
    except Exception as e:
        print("Note on committee_outcomes:", e)

# 5. Read signal_lifecycle.jsonl to count signals per channel and verdict distribution
lifecycle_path = "/opt/signalcopyreal/runtime/state/signal_lifecycle.jsonl"
verdicts_per_channel = {}
if os.path.exists(lifecycle_path):
    try:
        with open(lifecycle_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    cid = str(row.get("source_chat_id") or row.get("channel_id") or "")
                    event = row.get("event")
                    verdict = row.get("verdict")
                    if cid:
                        if cid not in verdicts_per_channel:
                            verdicts_per_channel[cid] = {"VALID": 0, "WEAK": 0, "REJECT": 0, "TOTAL": 0}
                        verdicts_per_channel[cid]["TOTAL"] += 1
                        if verdict in verdicts_per_channel[cid]:
                            verdicts_per_channel[cid][verdict] += 1
                except Exception:
                    pass
    except Exception as e:
        print("Note on lifecycle:", e)

print("="*100)
print(f"AUDIT PERFORMA CHANNEL KALIBRASI (Total Channel Kalibrasi: {len(calib_ids)})")
print("="*100)

results = []

for cid in calib_ids:
    name = channel_names.get(cid, "Unknown / Private Channel")
    perf = perf_data.get(cid, {})
    
    signals_count = perf.get("signals", 0)
    wins = perf.get("wins", 0)
    losses = perf.get("losses", 0)
    total_pnl = perf.get("total_pnl", 0.0)
    symbols = perf.get("symbols", {})
    last_upd = perf.get("last_updated", 0)
    
    last_dt_str = "Never"
    if last_upd > 0:
        dt = datetime.fromtimestamp(last_upd, tz=timezone(timedelta(hours=8)))
        last_dt_str = dt.strftime("%Y-%m-%d %H:%M")
        
    resolved_trades = wins + losses
    win_rate = (wins / resolved_trades * 100) if resolved_trades > 0 else 0.0
    
    # Verdict stats from lifecycle
    v_stats = verdicts_per_channel.get(cid, {"VALID": 0, "WEAK": 0, "REJECT": 0, "TOTAL": 0})
    
    # Top profitable symbol
    top_syms = sorted(symbols.items(), key=lambda x: x[1].get("pnl", 0.0), reverse=True)
    top_sym_str = f"{top_syms[0][0]} (+{top_syms[0][1]['pnl']:.1f}%)" if top_syms and top_syms[0][1].get("pnl", 0) > 0 else "None"
    
    results.append({
        "cid": cid,
        "name": name,
        "signals": signals_count,
        "wins": wins,
        "losses": losses,
        "resolved": resolved_trades,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "last_updated": last_dt_str,
        "valid_count": v_stats["VALID"],
        "weak_count": v_stats["WEAK"],
        "reject_count": v_stats["REJECT"],
        "symbols_count": len(symbols),
        "top_symbol": top_sym_str,
        "raw_perf": perf
    })

# Sort by win_rate descending, then total_pnl descending
results_sorted = sorted(results, key=lambda x: (x["win_rate"] >= 50.0, x["total_pnl"], x["win_rate"]), reverse=True)

print(f"{'No':2} | {'Channel Name':28} | {'Channel ID':16} | {'Trades':7} | {'W/L':7} | {'WinRate':7} | {'Tot PnL %':10} | {'VALID/REJ':10} | {'Last Signal':16}")
print("-" * 115)

for i, r in enumerate(results_sorted, 1):
    wl_str = f"{r['wins']}/{r['losses']}"
    vr_str = f"{r['valid_count']}V/{r['reject_count']}R"
    pnl_str = f"{r['total_pnl']:+.2f}%"
    print(f"{i:2} | {r['name'][:28]:28} | {r['cid']:16} | {r['resolved']:7} | {wl_str:7} | {r['win_rate']:6.1f}% | {pnl_str:10} | {vr_str:10} | {r['last_updated']:16}")

print("\n" + "="*100)
print("ANALISIS MENDALAM TIAP CHANNEL KALIBRASI:")
print("="*100)

for r in results_sorted:
    print(f"\n📡 [{r['name']}] (ID: {r['cid']})")
    print(f"   • Total Sinyal Masuk: {r['signals']} | Resolved Virtual Trades: {r['resolved']}")
    print(f"   • Win: {r['wins']} | Loss: {r['losses']} | Win Rate: {r['win_rate']:.1f}%")
    print(f"   • Total Akumulasi PnL Virtual: {r['total_pnl']:+.2f}%")
    print(f"   • Filter Klasifikasi: VALID={r['valid_count']}, WEAK={r['weak_count']}, REJECT={r['reject_count']}")
    print(f"   • Simbol Terbaik: {r['top_symbol']}")
    
    # Rekomendasi
    if r['cid'] == "-1003988458515":
        print("   👉 REKOMENDASI: TETAP KALIBRASI (Channel ini adalah Sandbox Test Pribadi).")
    elif r['resolved'] >= 10 and r['win_rate'] >= 60.0 and r['total_pnl'] > 5.0:
        print("   🌟 REKOMENDASI: SANGAT LAYAK DITINGKATKAN KE REAL! (Kriteria terpenuhi: sample > 10, winrate >= 60%, PnL positif signifikan).")
    elif r['resolved'] >= 5 and r['win_rate'] >= 50.0 and r['total_pnl'] > 0:
        print("   ⚡ REKOMENDASI: KANDIDAT POTENSIAL (Bisa dipromosikan dengan alokasi risk kecil / tier rendah).")
    elif r['resolved'] > 0 and (r['win_rate'] < 45.0 or r['total_pnl'] < 0):
        print("   ⛔ REKOMENDASI: JANGAN DITINGKATKAN (Performa buruk / net negatif, biarkan di kalibrasi agar modal aman).")
    else:
        print("   ⏳ REKOMENDASI: PERLU DATA LEBIH BANYAK (Sinyal masih terlalu sedikit untuk dinilai secara statistik).")

print("="*100)
