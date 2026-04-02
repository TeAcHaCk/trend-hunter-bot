"""
Trend Hunter Strategy — Limit orders at breakout levels.

Strategy:
  1. Calculate breakout range from N recent candles (high/low)
  2. Place limit BUY at breakout_high and limit SELL at breakout_low
  3. When one fills → cancel the other → attach bracket SL/TP (1:1 RR)
  4. If not filled in 30 min → cancel and recalculate
  5. Only 1 trade at a time
"""
import time
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class TrendHunterStrategy:
    """Limit order breakout strategy with 1:1 RR bracket orders."""

    def __init__(self, symbol: str, quantity: int = 10,
                 breakout_buffer_pct: float = 0.1):
        self.symbol = symbol
        self.quantity = quantity
        self.breakout_buffer_pct = breakout_buffer_pct
        self.enabled: bool = True
        self.current_position: Optional[str] = None

        # Price tracking
        self.last_price: Optional[float] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.candle_count: int = 0

        # Computed breakout levels
        self._breakout_high: Optional[float] = None
        self._breakout_low: Optional[float] = None
        self._levels_set: bool = False

        # Limit order tracking
        self._long_order_id: Optional[int] = None   # Limit buy at breakout_high
        self._short_order_id: Optional[int] = None   # Limit sell at breakout_low
        self._orders_placed: bool = False
        self._orders_placed_time: float = 0  # Unix timestamp
        self._order_expiry_seconds: int = 1800  # 30 minutes

        # Trade state — 1 trade at a time
        self._in_trade: bool = False
        self._trade_direction: Optional[str] = None
        self._entry_price: Optional[float] = None
        self._stop_loss: Optional[float] = None
        self._take_profit: Optional[float] = None
        self._bracket_placed: bool = False

    def update_levels_from_candles(self, candles: List[Dict]) -> bool:
        """Calculate breakout levels from recent candles."""
        if not candles or len(candles) < 2:
            return False

        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        self.range_high = max(highs)
        self.range_low = min(lows)
        self.candle_count = len(candles)

        # Breakout levels with buffer
        self._breakout_high = self.range_high * (1 + self.breakout_buffer_pct / 100)
        self._breakout_low = self.range_low * (1 - self.breakout_buffer_pct / 100)
        self._levels_set = True

        spread = self.range_high - self.range_low
        spread_pct = (spread / self.range_low * 100) if self.range_low else 0

        logger.info(
            f"[{self.symbol}] Candle range ({len(candles)} candles) | "
            f"H: ${self.range_high:,.2f} L: ${self.range_low:,.2f} | "
            f"Spread: ${spread:,.2f} ({spread_pct:.2f}%)"
        )

        return True

    def needs_new_orders(self) -> bool:
        """Check if we need to place new limit orders."""
        if self._in_trade:
            return False  # Already in a trade
        if not self._levels_set:
            return False
        if not self._orders_placed:
            return True  # No orders placed yet
        return False

    def are_orders_expired(self) -> bool:
        """Check if existing orders have been waiting too long (30 min)."""
        if not self._orders_placed:
            return False
        elapsed = time.time() - self._orders_placed_time
        return elapsed >= self._order_expiry_seconds

    def calculate_sl_tp(self, entry_price: float, direction: str) -> Dict[str, float]:
        """
        Calculate SL/TP for 1:1 risk-reward.
        Uses half the candle range spread as distance.
        """
        spread = self.range_high - self.range_low
        half_spread = spread / 2

        # Minimum distance (0.1% of entry)
        min_distance = entry_price * 0.001
        half_spread = max(half_spread, min_distance)

        if direction == "LONG":
            sl = round(entry_price - half_spread, 2)
            tp = round(entry_price + half_spread, 2)
        else:
            sl = round(entry_price + half_spread, 2)
            tp = round(entry_price - half_spread, 2)

        logger.info(
            f"[{self.symbol}] {direction} | Entry: ${entry_price:,.2f} | "
            f"SL: ${sl:,.2f} | TP: ${tp:,.2f} | Risk: ${half_spread:,.2f} | RR: 1:1"
        )

        return {"stop_loss": sl, "take_profit": tp, "risk": half_spread}

    def record_orders_placed(self, long_order_id: int = None, short_order_id: int = None):
        """Record that limit orders have been placed."""
        self._long_order_id = long_order_id
        self._short_order_id = short_order_id
        self._orders_placed = True
        self._orders_placed_time = time.time()
        logger.info(
            f"[{self.symbol}] Limit orders placed | "
            f"BUY #{long_order_id} @ ${self._breakout_high:,.2f} | "
            f"SELL #{short_order_id} @ ${self._breakout_low:,.2f}"
        )

    def clear_orders(self):
        """Clear order tracking (after cancel or fill)."""
        self._long_order_id = None
        self._short_order_id = None
        self._orders_placed = False
        self._orders_placed_time = 0

    def enter_trade(self, direction: str, entry_price: float,
                    stop_loss: float, take_profit: float):
        """Record that we've entered a trade."""
        self._in_trade = True
        self._trade_direction = direction
        self._entry_price = entry_price
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._bracket_placed = True
        self.current_position = direction
        self.clear_orders()  # No more pending orders

    def exit_trade(self):
        """Reset trade state after SL/TP exit."""
        self._in_trade = False
        self._trade_direction = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None
        self._bracket_placed = False
        self.current_position = None

    def is_in_trade(self) -> bool:
        return self._in_trade

    def get_status(self) -> dict:
        """Get current strategy status for dashboard."""
        return {
            "symbol": self.symbol,
            "enabled": self.enabled,
            "quantity": self.quantity,
            "breakout_buffer_pct": self.breakout_buffer_pct,
            "current_position": self.current_position,
            "range_high": self.range_high,
            "range_low": self.range_low,
            "breakout_high": self._breakout_high,
            "breakout_low": self._breakout_low,
            "last_price": self.last_price,
            "candle_count": self.candle_count,
            "in_trade": self._in_trade,
            "trade_direction": self._trade_direction,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "orders_placed": self._orders_placed,
            "long_order_id": self._long_order_id,
            "short_order_id": self._short_order_id,
        }
