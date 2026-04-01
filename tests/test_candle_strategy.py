"""Direct end-to-end test of candle breakout strategy — no server needed."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.exchange.delta_client import DeltaClient
from backend.strategy.trend_hunter import TrendHunterStrategy

def main():
    client = DeltaClient()
    
    # 1. Fetch candles
    print("=== Fetching 6x 15m candles for BTCUSD ===")
    candles = client.get_candles("BTCUSD", "15m", 6)
    if not candles:
        print("FAILED: No candles returned")
        return
    print(f"Got {len(candles)} candles")
    for c in candles:
        print(f"  {c['time']}: O={c['open']} H={c['high']} L={c['low']} C={c['close']}")
    
    # 2. Create strategy and set levels from candles
    strategy = TrendHunterStrategy("BTCUSD", quantity=10, breakout_buffer_pct=0.1)
    ok = strategy.update_levels_from_candles(candles)
    print(f"\nLevels set: {ok}")
    
    status = strategy.get_status()
    print(f"\n=== Strategy Status ===")
    for k, v in status.items():
        print(f"  {k}: {v}")
    
    # 3. Check if new fields exist
    assert "range_high" in status, "FAIL: range_high not in status!"
    assert "candle_count" in status, "FAIL: candle_count not in status!"
    assert "high_24h" not in status, "FAIL: old high_24h still in status!"
    
    # 4. Test signal
    price = float(candles[0]["close"])
    signal = strategy.check_signal(price)
    print(f"\nCurrent price: ${price:,.2f}")
    print(f"Signal: {signal}")
    print(f"Distance to breakout UP: ${status['breakout_high'] - price:,.2f}")
    print(f"Distance to breakout DOWN: ${price - status['breakout_low']:,.2f}")
    
    # 5. Repeat for ETH
    print("\n=== Fetching 6x 15m candles for ETHUSD ===")
    eth_candles = client.get_candles("ETHUSD", "15m", 6)
    if eth_candles:
        eth_strategy = TrendHunterStrategy("ETHUSD", quantity=10, breakout_buffer_pct=0.1)
        eth_strategy.update_levels_from_candles(eth_candles)
        eth_status = eth_strategy.get_status()
        print(f"Range: ${eth_status['range_high']:,.2f} / ${eth_status['range_low']:,.2f}")
        print(f"Breakout: ${eth_status['breakout_high']:,.2f} / ${eth_status['breakout_low']:,.2f}")
        print(f"Candles: {eth_status['candle_count']}")
    
    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    main()
