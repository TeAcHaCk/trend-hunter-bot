"""Quick test of the new strategy logic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.exchange.delta_client import DeltaClient
from backend.strategy.trend_hunter import TrendHunterStrategy

def main():
    client = DeltaClient()
    
    # Fetch candles
    print("=== Fetching 6x 15m candles for BTCUSD ===")
    candles = client.get_candles("BTCUSD", "15m", 6)
    if not candles:
        print("FAILED: No candles"); return
    print(f"Got {len(candles)} candles")
    
    # Create strategy
    strat = TrendHunterStrategy("BTCUSD", quantity=10, breakout_buffer_pct=0.1)
    strat.update_levels_from_candles(candles)
    
    print(f"\nRange: ${strat.range_high:,.2f} - ${strat.range_low:,.2f}")
    print(f"Breakout: UP ${strat._breakout_high:,.2f} / DOWN ${strat._breakout_low:,.2f}")
    
    # Test confirmation candle check
    signal = strat.check_confirmation_candle(candles)
    print(f"\nConfirmation signal: {signal}")
    
    # Test SL/TP calculation
    entry = float(candles[0]["close"])
    for direction in ["LONG", "SHORT"]:
        levels = strat.calculate_sl_tp(entry, direction)
        print(f"\n{direction} @ ${entry:,.2f}:")
        print(f"  SL: ${levels['stop_loss']:,.2f}")
        print(f"  TP: ${levels['take_profit']:,.2f}")
        print(f"  Risk: ${levels['risk']:,.2f}")
        print(f"  RR: 1:1")
    
    # Test trade state management
    strat.enter_trade("LONG", entry, levels["stop_loss"], levels["take_profit"])
    assert strat.is_in_trade() == True
    assert strat.check_confirmation_candle(candles) is None  # Should skip while in trade
    strat.exit_trade()
    assert strat.is_in_trade() == False
    
    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    main()
