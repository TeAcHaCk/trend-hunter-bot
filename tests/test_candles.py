import requests, time

now = int(time.time())
start = now - (6 * 15 * 60)  # Last 6 candles = 1.5 hours

for symbol in ["BTCUSD", "ETHUSD"]:
    r = requests.get(
        "https://cdn-ind.testnet.deltaex.org/v2/history/candles",
        params={"resolution": "15m", "symbol": symbol, "start": str(start), "end": str(now)},
        timeout=10,
    )
    data = r.json()
    candles = data.get("result", [])
    
    print(f"\n{symbol}: Got {len(candles)} candles (last 1.5 hours)")
    for c in candles:
        print(f"  H={c['high']:>10.1f}  L={c['low']:>10.1f}  C={c['close']:>10.1f}")
    
    if candles:
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        h = max(highs)
        l = min(lows)
        print(f"  Range High: ${h:,.2f}")
        print(f"  Range Low:  ${l:,.2f}")
        print(f"  Spread:     ${h - l:,.2f} ({(h-l)/l*100:.2f}%)")
        print(f"  Current:    ${candles[0]['close']:,.2f}")
        buf = 0.1
        bh = h * (1 + buf / 100)
        bl = l * (1 - buf / 100)
        print(f"  Breakout H: ${bh:,.2f} (price needs to go UP ${bh - candles[0]['close']:,.2f})")
        print(f"  Breakout L: ${bl:,.2f} (price needs to go DOWN ${candles[0]['close'] - bl:,.2f})")
