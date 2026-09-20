#!/usr/bin/env python3
"""
ACE/USDT Position Monitor
Real-time monitoring untuk sisa 50% long position dengan alert ke Telegram.
"""
import os, sys, time, json, requests
from datetime import datetime, timezone

# Config
SYMBOL = "ACEUSDT"
CHECK_INTERVAL = 30  # seconds
MONITOR_FILE = "/home/fusion_omega/fusion_omega_nexus/runtime/ace_position_monitor.json"
TELEGRAM_TOKEN = os.getenv("ANALYST_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "578305627"

def get_price_data():
    """Fetch current price + OI + funding dari Binance Futures"""
    try:
        # Price
        ticker = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={SYMBOL}", timeout=5).json()
        price = float(ticker["lastPrice"])
        change_24h = float(ticker["priceChangePercent"])
        volume_24h = float(ticker["quoteVolume"])
        
        # OI
        oi_data = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL}", timeout=5).json()
        oi = float(oi_data["openInterest"]) * price
        
        # Funding
        funding = requests.get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={SYMBOL}&limit=1", timeout=5).json()[0]
        funding_rate = float(funding["fundingRate"]) * 100
        
        return {
            "price": price,
            "change_24h": change_24h,
            "volume_24h": volume_24h,
            "oi": oi,
            "funding_rate": funding_rate,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        print(f"Error fetching data: {e}")
        return None

def send_telegram(message, urgent=False):
    """Send alert ke Telegram"""
    if not TELEGRAM_TOKEN:
        print(f"[TELEGRAM] {message}")
        return
    
    prefix = "🚨 URGENT" if urgent else "📊"
    full_message = f"{prefix} ACE/USDT Monitor\n\n{message}"
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": full_message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        print(f"Telegram send failed: {e}")

def check_alerts(data, config):
    """Check price alerts dan kirim notifikasi"""
    price = data["price"]
    alerts_triggered = []
    
    for alert in config["alerts"]:
        if not alert["enabled"]:
            continue
            
        trigger_price = alert["price"]
        
        if alert["trigger"] == "below" and price < trigger_price:
            alerts_triggered.append(alert)
            alert["enabled"] = False  # Fire once
        elif alert["trigger"] == "above" and price > trigger_price:
            alerts_triggered.append(alert)
            alert["enabled"] = False  # Fire once
    
    return alerts_triggered

def format_alert_message(alert, data):
    """Format alert message"""
    level_emoji = {
        "CRITICAL": "🔴",
        "WARNING": "🟡",
        "OPPORTUNITY": "🟢",
        "TARGET": "🎯"
    }
    
    emoji = level_emoji.get(alert["level"], "📍")
    
    msg = f"{emoji} <b>{alert['level']}</b>\n\n"
    msg += f"Price: <b>${data['price']:.5f}</b>\n"
    msg += f"Alert: ${alert['price']:.5f}\n"
    msg += f"24h Change: {data['change_24h']:+.2f}%\n"
    msg += f"Funding: {data['funding_rate']:.4f}%\n\n"
    msg += f"<b>ACTION:</b>\n{alert['action']}\n\n"
    msg += f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    
    return msg

def check_checkpoints(config):
    """Check scheduled checkpoints"""
    now = datetime.now(timezone.utc)
    alerts = []
    
    for cp in config["checkpoints"]:
        cp_time = datetime.fromisoformat(cp["time"].replace("Z", "+00:00"))
        
        # Alert 5 menit sebelum checkpoint
        time_diff = (cp_time - now).total_seconds()
        
        if 0 < time_diff < 300 and not cp.get("notified_5min"):
            alerts.append({
                "type": "checkpoint_reminder",
                "checkpoint": cp,
                "time_to": f"{int(time_diff/60)} minutes"
            })
            cp["notified_5min"] = True
        
        # Alert saat checkpoint
        if -60 < time_diff < 60 and not cp.get("notified"):
            alerts.append({
                "type": "checkpoint_now",
                "checkpoint": cp
            })
            cp["notified"] = True
    
    return alerts

def main():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] ACE/USDT Monitor Started")
    print(f"Symbol: {SYMBOL}")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print(f"Telegram: {'✓ Enabled' if TELEGRAM_TOKEN else '✗ Disabled (logs only)'}\n")
    
    # Load config
    with open(MONITOR_FILE) as f:
        config = json.load(f)
    
    # Send startup notification
    send_telegram(
        f"Monitor started for <b>{SYMBOL}</b>\n"
        f"Position: 50% long remaining\n"
        f"SL: ${config['position']['current_sl']}\n\n"
        f"<b>Active Alerts:</b>\n"
        + "\n".join([f"• ${a['price']:.5f} {a['trigger']} - {a['level']}" for a in config['alerts'] if a['enabled']])
    )
    
    last_price_log = 0
    
    try:
        while True:
            data = get_price_data()
            if not data:
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Check price alerts
            triggered = check_alerts(data, config)
            for alert in triggered:
                msg = format_alert_message(alert, data)
                send_telegram(msg, urgent=(alert["level"] == "CRITICAL"))
                print(f"\n🚨 ALERT: {alert['level']} @ ${data['price']:.5f}")
            
            # Check checkpoints
            cp_alerts = check_checkpoints(config)
            for cp_alert in cp_alerts:
                if cp_alert["type"] == "checkpoint_reminder":
                    cp = cp_alert["checkpoint"]
                    msg = (
                        f"⏰ <b>Checkpoint Reminder</b>\n\n"
                        f"In {cp_alert['time_to']}: {cp['type']}\n\n"
                        f"<b>Action:</b>\n{cp['action']}"
                    )
                    send_telegram(msg)
                elif cp_alert["type"] == "checkpoint_now":
                    cp = cp_alert["checkpoint"]
                    msg = (
                        f"🔔 <b>Checkpoint NOW</b>\n\n"
                        f"Type: {cp['type']}\n"
                        f"Price: <b>${data['price']:.5f}</b>\n"
                        f"24h: {data['change_24h']:+.2f}%\n"
                        f"Funding: {data['funding_rate']:.4f}%\n\n"
                        f"<b>Action Required:</b>\n{cp['action']}"
                    )
                    send_telegram(msg, urgent=True)
            
            # Log price every 5 minutes
            now = time.time()
            if now - last_price_log > 300:
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"${data['price']:.5f} ({data['change_24h']:+.2f}%) "
                      f"Funding: {data['funding_rate']:.4f}%")
                last_price_log = now
            
            # Save config updates
            config["last_check"] = data["timestamp"]
            with open(MONITOR_FILE, "w") as f:
                json.dump(config, f, indent=2)
            
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user")
        send_telegram("Monitor stopped manually")
    except Exception as e:
        print(f"\n\nError: {e}")
        send_telegram(f"⚠️ Monitor crashed: {e}", urgent=True)
        raise

if __name__ == "__main__":
    main()
