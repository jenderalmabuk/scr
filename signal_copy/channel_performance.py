import json
import time
import os
from pathlib import Path
from typing import Dict, Any, List
from utils.logger import logger

DB_FILE = os.getenv("SIGNAL_COPY_CHANNEL_PERFORMANCE_STORE", "data/channel_performance.json")

class ChannelPerformanceTracker:
    def __init__(self):
        self._load()
    
    def _load(self):
        p = Path(DB_FILE)
        if p.exists():
            try:
                raw = p.read_text().strip()
                self.data = json.loads(raw) if raw else {}
            except (OSError, json.JSONDecodeError):
                self.data = {}
        else:
            self.data = {}
    
    def _save(self):
        try:
            path = Path(DB_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"[CH_PERF] Save failed: {e}")

    def record_trade(self, source_chat_id: int, source_name: str, symbol: str, 
                     pnl_pct: float, exit_reason: str) -> None:
        """Update performance after a trade is closed."""
        key = str(source_chat_id)
        if key not in self.data:
            self.data[key] = {
                "name": source_name,
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "symbols": {},
                "last_updated": time.time()
            }
        
        stats = self.data[key]
        stats["signals"] += 1
        stats["total_pnl"] += pnl_pct
        
        if pnl_pct > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        
        # Track per-symbol performance
        sym_key = symbol.upper()
        if sym_key not in stats["symbols"]:
            stats["symbols"][sym_key] = {"trades": 0, "wins": 0, "pnl": 0}
        
        stats["symbols"][sym_key]["trades"] += 1
        stats["symbols"][sym_key]["pnl"] += pnl_pct
        if pnl_pct > 0:
            stats["symbols"][sym_key]["wins"] += 1
        
        stats["last_updated"] = time.time()
        self._save()
        logger.info(f"[CH_PERF] Recorded trade for {source_name}: {pnl_pct:+.2f}% ({exit_reason})")

    def get_channel_stats(self, source_chat_id: int) -> Dict[str, Any]:
        """Get performance stats for a specific channel."""
        key = str(source_chat_id)
        stats = self.data.get(key, {})
        
        signals = stats.get("signals", 0)
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        
        win_rate = (wins / signals * 100) if signals > 0 else 0.0
        avg_pnl = stats.get("total_pnl", 0) / signals if signals > 0 else 0.0
        
        return {
            "signals": signals,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "total_pnl": stats.get("total_pnl", 0),
            "last_updated": stats.get("last_updated", 0)
        }

    def get_reputation_score(self, source_chat_id: int) -> float:
        """Calculate a reputation score (0-100) for validation weighting."""
        # Channel-specific overrides (pilot channels, high-frequency scalping, etc.)
        CHANNEL_SCORE_OVERRIDES = {
            -1001652601224: 80.0,  # Pilot scalping channel: stricter threshold
        }

        if source_chat_id in CHANNEL_SCORE_OVERRIDES:
            return CHANNEL_SCORE_OVERRIDES[source_chat_id]

        stats = self.get_channel_stats(source_chat_id)
        
        if stats["signals"] < 3:
            return 50.0  # Neutral for new channels
        
        # Score formula: 60% win rate + 40% avg PNL weight
        win_score = min(stats["win_rate"], 80) * 0.6
        
        # Normalize avg_pnl: good = >1%, bad = <-1%
        pnl_score = max(-20, min(20, stats["avg_pnl"] * 10)) * 0.4 + 8
        
        return max(0, min(100, win_score + pnl_score))

    def get_leaderboard(self, limit: int = 10) -> List[Dict]:
        """Get top performing channels."""
        results = []
        for cid, stats in self.data.items():
            if stats.get("signals", 0) > 0:
                wr = (stats["wins"] / stats["signals"] * 100) if stats["signals"] > 0 else 0
                results.append({
                    "chat_id": cid,
                    "name": stats.get("name", "Unknown"),
                    "signals": stats["signals"],
                    "win_rate": wr,
                    "total_pnl": stats.get("total_pnl", 0)
                })
        
        results.sort(key=lambda x: x["total_pnl"], reverse=True)
        return results[:limit]

    def get_report(self) -> str:
        """Generate a formatted leaderboard report."""
        board = self.get_leaderboard(10)
        if not board:
            return "Belum ada data performa channel."
        
        lines = ["🏆 <b>CHANNEL LEADERBOARD</b> (Top 10)", ""]
        
        for i, entry in enumerate(board, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
            lines.append(
                f"{medal} {i}. <b>{entry['name'][:25]}</b>\n"
                f"   • Signals: {entry['signals']} | Win Rate: {entry['win_rate']:.1f}% | "
                f"P/L: <b>{entry['total_pnl']:+.2f}%</b>"
            )
        
        return "\n".join(lines)

# Global instance
_tracker = None

def get_tracker() -> ChannelPerformanceTracker:
    global _tracker
    if _tracker is None:
        _tracker = ChannelPerformanceTracker()
    return _tracker
