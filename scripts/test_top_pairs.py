import asyncio
from bots.autonomous_market_scanner import AutonomousMarketScanner

async def test_top_pairs():
    scanner = AutonomousMarketScanner()
    try:
        btc_info = await scanner.get_btc_regime()
        print("BTC Regime:", btc_info)
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "SUIUSDT", "DOGEUSDT", "AVAXUSDT"]
        print("\nDiagnosing top pairs:")
        for sym in pairs:
            k1h = await scanner.fetch_klines(sym, tf="1h", limit=60)
            k15m = await scanner.fetch_klines(sym, tf="15m", limit=60)
            if not k1h or not k15m:
                print(f"  {sym}: No klines")
                continue
            closes_1h = [float(k["close"]) for k in k1h]
            closes_15m = [float(k["close"]) for k in k15m]
            from bots.autonomous_market_scanner import compute_ema, compute_rsi
            ema50_1h = compute_ema(closes_1h, 50)
            ema200_1h = compute_ema(closes_1h, min(len(closes_1h), 200))
            ema20_15m = compute_ema(closes_15m, 20)
            rsi_15m = compute_rsi(closes_15m, 14)
            curr = closes_15m[-1]
            bias = "BULL" if curr > ema50_1h else "BEAR"
            print(f"  {sym}: Price={curr:.4f} | 1h Trend={bias} (EMA50={ema50_1h:.4f}, EMA200={ema200_1h:.4f}) | 15m EMA20={ema20_15m:.4f} | RSI15m={rsi_15m:.1f}")
    finally:
        await scanner.close()

if __name__ == "__main__":
    asyncio.run(test_top_pairs())
