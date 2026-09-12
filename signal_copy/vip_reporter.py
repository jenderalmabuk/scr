import asyncio
import time
from typing import Dict, Any, List
from utils.logger import logger
from signals.vip_fast_lane import get_minutes_to_next_anchor
from signals.silent_accumulation import get_accumulated_coins
from signals.silent_accumulation import get_silent_accumulation_details
from whales.redis_db import get_pending_whales

class VIPSessionReporter:
    def __init__(self, notifier_func, accum_log: list = None):
        self.notifier = notifier_func
        self._accum_log = accum_log if accum_log is not None else []
        self._accum_log = accum_log if accum_log is not None else []
        self._last_report_session = None
        self._report_window_min = 30  # Report 30 mins before session

    async def get_top_accumulated_report(self) -> str:
        """Analyze and build a report of the most accumulated coins."""
        import re
        # Parse symbols and USD values from accumulation messages
        sym_data = {}
        for entry in self._accum_log[-200:]:
            text = entry.get("text", "")
            syms = re.findall(r'\$([A-Z]{2,15})\b', text)
            usd_total = 0.0
            for m in re.finditer(r'\$([0-9,.]+)\s*([MKBT]?)(?:\s*USD)?', text):
                try:
                    val = float(m.group(1).replace(",", ""))
                    s = m.group(2)
                    if s == "M": val *= 1000000
                    elif s == "K": val *= 1000
                    elif s == "B": val *= 1000000000
                    usd_total += val
                except: pass
            for sym in syms:
                if sym not in sym_data:
                    sym_data[sym] = {"count": 0, "usd": 0.0}
                sym_data[sym]["count"] += 1
                sym_data[sym]["usd"] += usd_total / max(len(syms), 1)
            # Check known coin symbols
            for m in re.finditer(r'\b(ETH|BTC|SOL|XRP|BNB|ADA|DOT|AVAX|HYPE|ONDO|PENDLE)\b', text):
                sym = m.group(1)
                if sym not in sym_data:
                    sym_data[sym] = {"count": 0, "usd": 0.0}
                sym_data[sym]["count"] += 1

        coins = get_accumulated_coins(include_smc=True)
        
        # Generate report from BOTH sources
        whale_lines = []
        if sym_data:
            sorted_syms = sorted(sym_data.items(), key=lambda x: x[1]["count"], reverse=True)
            whale_lines.append("\n🐋 <b>Whale Mentions (24h)</b>")
            for sym, data in sorted_syms[:5]:
                c = data["count"]
                u = data["usd"]
                if u >= 1000000: us = f" ${u/1000000:.1f}M"
                elif u >= 1000: us = f" ${u/1000:.0f}K"
                else: us = ""
                whale_lines.append(f"  • <b>{sym}</b>: {c}x{us}")
        
        if not coins:
            base = "Tidak ada koin dengan akumulasi signifikan saat ini."
            if whale_lines:
                return base + "\n" + "\n".join(whale_lines)
            return base
            
        whale_data = get_pending_whales()
        analysis = []
        
        for symbol in coins:
            # Get silent accumulation score
            acc_details = get_silent_accumulation_details(symbol)
            acc_score = acc_details.get('accumulation_score', 0)
            acc_state = acc_details.get('accumulation_state', 'NONE')
            
            # Count recent whale activities
            whales_count = len([w for w in whale_data if w.get('symbol') == symbol])
            
            # Combine into a session conviction score
            conviction_score = acc_score + (whales_count * 5)
            
            analysis.append({
                'symbol': symbol,
                'state': acc_state,
                'whales': whales_count,
                'score': conviction_score,
                'reason': acc_details.get('reason', 'N/A')
            })
            
        # Sort by conviction score
        analysis.sort(key=lambda x: x['score'], reverse=True)
        top_3 = analysis[:5]
        
        lines = ["📊 <b>VIP ACCUMULATION BRIEFING</b>"]
        lines.append(f"<i>Laporan akumulasi sebelum pembukaan sesi/candle</i>\n")
        lines.extend(whale_lines)
        if whale_lines:
            lines.append("")
        
        for item in top_3:
            badge = "🔥" if item['score'] > 40 else "💎"
            lines.append(f"{badge} <b>{item['symbol']}</b> (Score: {item['score']:.1f})")
            lines.append(f"   • State: {item['state']}")
            lines.append(f"   • Whale Activity: {item['whales']} alerts")
            lines.append(f"   • Focus: {item['reason']}\n")
            
        return "\n".join(lines)

    async def get_distribution_alert(self) -> List[Dict]:
        """Check for high sell pressure (distribution/dumping) in the last 4 hours."""
        from whales.redis_db import get_whale_pressure
        
        coins = get_accumulated_coins(include_smc=True)
        alerts = []
        
        for symbol in coins:
            pressure = get_whale_pressure(symbol, window_minutes=240) # 4 hours
            
            # If sell volume exceeds buy volume significantly (> 3x), mark as distribution risk
            if pressure['sell_vol'] > 0 and pressure['buy_vol'] > 0:
                ratio = pressure['sell_vol'] / pressure['buy_vol']
                if ratio > 3.0:
                    alerts.append({
                        'symbol': symbol,
                        'risk': 'HIGH_DISTRIBUTION',
                        'sell_vol': pressure['sell_vol'],
                        'buy_vol': pressure['buy_vol'],
                        'ratio': ratio
                    })
            elif pressure['sell_vol'] > (pressure['buy_vol'] + 500000):
                # Pure selling pressure > $500k in 4 hours
                alerts.append({
                    'symbol': symbol,
                    'risk': 'PURE_SELLING',
                    'sell_vol': pressure['sell_vol'],
                    'net_vol': pressure['net_vol']
                })
                
        return alerts

    async def check_and_notify_distribution(self):
        """Run distribution check and notify user immediately if high risk detected."""
        alerts = await self.get_distribution_alert()
        if alerts and self.notifier:
            lines = ["⚠️ <b>DISTRIBUTION ALERT DETECTED</b>"]
            lines.append("<i>Whale selling pressure exceeds accumulation</i>\n")
            
            for alert in alerts[:3]:
                lines.append(f"🚨 <b>{alert['symbol']}</b> - {alert['risk']}")
                lines.append(f"   • Sell Vol: ${alert.get('sell_vol', 0):,.0f}")
                if 'ratio' in alert:
                    lines.append(f"   • Sell/Buy Ratio: {alert['ratio']:.1f}x\n")
                else:
                    lines.append(f"   • Net Volume: ${alert.get('net_vol', 0):,.0f}\n")
            
            await self.notifier("\n".join(lines))

    async def loop(self):
        """Main background loop to monitor session timing."""
        logger.info("[VIP_REPORTER] Aggregator loop started.")
        while True:
            try:
                anchors = get_minutes_to_next_anchor()
                for session_name, minutes_left in anchors.items():
                    # Check if we are within the 30-min reporting window
                    if 25 <= minutes_left <= 30:
                        if self._last_report_session != session_name:
                            logger.info(f"[VIP_REPORTER] Preparing report for {session_name} ({minutes_left}m left)")
                            report = await self.get_top_accumulated_report()
                            header = f"🔔 <b>PERSIAPAN SESI {session_name}</b>\n"
                            if self.notifier:
                                await self.notifier(header + report)
                            self._last_report_session = session_name
                
                # Also run distribution check every 15 minutes
                await asyncio.sleep(15) # check every 15 seconds (accelerated for testing) 
                # TODO: Change to 900 (15 min) in production
                await self.check_and_notify_distribution()
                
            except Exception as exc:
                logger.error(f"[VIP_REPORTER] loop error: {exc}")
                await asyncio.sleep(10)
