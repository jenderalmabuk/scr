const tv = require("@mathieuc/tradingview");

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", async () => {
  try {
    const { symbol } = JSON.parse(input);
    if (!symbol) {
      console.log(JSON.stringify({ error: "No symbol" }));
      process.exit(0);
    }

    const result = {};
    const tvSymbol = `BINANCE:${symbol.replace("USDT", "USDT")}`;

    // Technical Analysis Summary (RSI, MA, MACD recommendations)
    try {
      const ta = await tv.getTA(tvSymbol);
      if (ta) result["_ta"] = ta;
    } catch (e) {
      result["_ta"] = { error: e.message };
    }

    // Search and get RSI indicator value
    try {
      const indicators = await tv.searchIndicator("Relative Strength Index");
      if (indicators && indicators.length > 0) {
        const rsiScript = indicators[0];
        const rsi = await tv.getIndicator(rsiScript.scriptIdPart, rsiScript.version);
        result["RSI"] = rsi;
      }
    } catch (e) {
      result["RSI"] = { error: e.message };
    }

    // Get EMA directly using known built-in ID
    try {
      const ema20 = await tv.getIndicator("STD;EMA", 1, { length: 20 });
      if (ema20) result["EMA_20"] = ema20;
    } catch (e) { /* ignore */ }

    console.log(JSON.stringify(result));
    process.exit(0);
  } catch (e) {
    console.log(JSON.stringify({ error: e.message }));
    process.exit(1);
  }
});
