"""Quick test: start bot, wait, check if limit orders were placed."""
import requests
import time
import json
import sys

BASE = "http://localhost:8000"

# 1. Check server
try:
    r = requests.get(f"{BASE}/api/status", timeout=5)
    r.raise_for_status()
except Exception as e:
    print(f"Server not reachable: {e}")
    sys.exit(1)

status = r.json().get("result", {})
print(f"Bot state: {status.get('state')}")

# 2. Start bot if not running (long timeout for WS connect)
if status.get("state") != "RUNNING":
    print("Starting bot (may take 30s for WS connect)...")
    try:
        r = requests.post(f"{BASE}/api/bot/start", timeout=60)
        print(f"Start result: {r.json()}")
    except Exception as e:
        print(f"Start failed/timeout: {e}")
        # Check if it actually started
        r = requests.get(f"{BASE}/api/status", timeout=5)
        status = r.json().get("result", {})
        print(f"After start attempt, state: {status.get('state')}")
    time.sleep(3)

# 3. Wait for strategy checks
print("Waiting 20s for strategy checks...")
time.sleep(20)

# 4. Check status
r = requests.get(f"{BASE}/api/status", timeout=5)
status = r.json().get("result", {})
print(f"\nBot state: {status.get('state')}")
print(f"Last check: {status.get('last_check_time')}")
print(f"Signals: {status.get('total_signals')}")

for sym in ["BTCUSD", "ETHUSD"]:
    s = status.get("strategies", {}).get(sym, {})
    print(f"\n--- {sym} ---")
    print(f"  Price: {s.get('last_price')}")
    print(f"  Range: {s.get('range_low')} - {s.get('range_high')}")
    print(f"  Breakout: {s.get('breakout_low')} - {s.get('breakout_high')}")
    print(f"  Orders placed: {s.get('orders_placed')}")
    print(f"  Long order ID: {s.get('long_order_id')}")
    print(f"  Short order ID: {s.get('short_order_id')}")
    print(f"  In trade: {s.get('in_trade')}")
