import requests
import time

# Start the bot
print("Starting bot...")
r = requests.post("http://localhost:8000/api/bot/start", timeout=15)
print(r.json())

# Wait for first strategy check (30s interval)
print("\nWaiting 35 seconds for first strategy check...\n")
time.sleep(35)

# Check status
r = requests.get("http://localhost:8000/api/status", timeout=10)
d = r.json()["result"]
print(f"State: {d['state']}")
print(f"Last Check: {d.get('last_check_time')}")

for sym, s in d["strategies"].items():
    print(f"\n{'='*50}")
    print(f"{sym}:")
    print(f"  Last Price:    ${s.get('last_price', 0) or 0:,.2f}")
    print(f"  Range High:    ${s.get('range_high', 0) or 0:,.2f}")
    print(f"  Range Low:     ${s.get('range_low', 0) or 0:,.2f}")
    print(f"  Breakout High: ${s.get('breakout_high', 0) or 0:,.2f}")
    print(f"  Breakout Low:  ${s.get('breakout_low', 0) or 0:,.2f}")
    print(f"  Candle Count:  {s.get('candle_count', 0)}")

    if s.get('breakout_high') and s.get('last_price'):
        bh = s['breakout_high']
        bl = s['breakout_low']
        lp = s['last_price']
        print(f"  Distance UP:   ${bh - lp:,.2f}")
        print(f"  Distance DOWN: ${lp - bl:,.2f}")
        spread = bh - bl
        print(f"  Total Spread:  ${spread:,.2f} ({spread/lp*100:.2f}%)")
