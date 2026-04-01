import requests, json

r = requests.get("http://localhost:8000/api/status", timeout=10)
d = r.json()["result"]
print(f"State: {d['state']}")
print(f"WS Connected: {d['ws_connected']}")
print(f"Last Check: {d.get('last_check_time')}")

for sym, s in d["strategies"].items():
    print(f"\n{sym}:")
    print(f"  Last Price:    ${s.get('last_price', 0) or 0:,.2f}")
    print(f"  Range High:    ${s.get('range_high', 0) or 0:,.2f}")
    print(f"  Range Low:     ${s.get('range_low', 0) or 0:,.2f}")
    print(f"  Breakout High: ${s.get('breakout_high', 0) or 0:,.2f}")
    print(f"  Breakout Low:  ${s.get('breakout_low', 0) or 0:,.2f}")
    print(f"  Candle Count:  {s.get('candle_count', 0)}")
    
    if s.get('breakout_high') and s.get('last_price'):
        dist_up = s['breakout_high'] - s['last_price']
        dist_down = s['last_price'] - s['breakout_low']
        print(f"  Distance Up:   ${dist_up:,.2f}")
        print(f"  Distance Down: ${dist_down:,.2f}")
