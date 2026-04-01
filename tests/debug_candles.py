"""Direct test of the candle fetch to debug why it's failing in the strategy check."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.exchange.delta_client import DeltaClient
from backend.config import settings

def main():
    client = DeltaClient()
    print(f"Base URL: {client.base_url}")
    
    # Test get_candles directly
    print("\n--- Testing get_candles() ---")
    candles = client.get_candles("BTCUSD", "15m", 6)
    print(f"Type: {type(candles)}")
    print(f"Length: {len(candles)}")
    if candles:
        print(f"First candle: {candles[0]}")
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        print(f"Range High: ${max(highs):,.2f}")
        print(f"Range Low: ${min(lows):,.2f}")
    else:
        print("!!! NO CANDLES RETURNED !!!")
        
    # Test the raw _request call
    print("\n--- Testing raw _request ---")
    import time
    now = int(time.time())
    start = now - (6 * 900) - 900
    result = client._request("GET", "/v2/history/candles", params={
        "resolution": "15m",
        "symbol": "BTCUSD",
        "start": str(start),
        "end": str(now),
    })
    print(f"Result type: {type(result)}")
    print(f"Result keys: {result.keys() if isinstance(result, dict) else 'N/A'}")
    if isinstance(result, dict):
        if "result" in result:
            print(f"result count: {len(result['result'])}")
        if "success" in result:
            print(f"success: {result['success']}")
        if "error" in result:
            print(f"error: {result['error']}")

if __name__ == "__main__":
    main()
