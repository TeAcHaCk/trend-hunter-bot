"""Start bot, wait, then check strategy status & debug log."""
import requests, time, json, os

print("Starting bot...")
r = requests.post("http://localhost:8000/api/bot/start", timeout=15)
print(r.json())

print("\nWaiting 35 seconds for first strategy check cycle...\n")
time.sleep(35)

# Check strategy status
r = requests.get("http://localhost:8000/api/status", timeout=10)
d = r.json()["result"]
print(f"State: {d['state']}")
print(f"Last Check: {d.get('last_check_time')}")

for sym, s in d["strategies"].items():
    print(f"\n=== {sym} ===")
    print(f"  Fields: {list(s.keys())}")
    for k, v in s.items():
        print(f"  {k}: {v}")

# Check debug log
log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_debug.log")
print(f"\n=== Debug Log ({log_path}) ===")
if os.path.exists(log_path):
    with open(log_path, "r", encoding="utf-8") as f:
        print(f.read())
else:
    print("FILE DOES NOT EXIST")
