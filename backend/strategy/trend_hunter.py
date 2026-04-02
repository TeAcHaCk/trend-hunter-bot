"""
Trend Hunter Strategy — 15-minute candle breakout with confirmation.

Strategy:
  1. Calculate breakout range from N recent candles (high/low)
  2. Wait for a CONFIRMATION candle that closes above/below the range
  3. Enter market order with bracket (SL + TP at 1:1 risk-reward)
  4. Only 1 trade at a time — wait for exit before next entry
"""
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class TrendHunterStrategy:
    """15-minute candle breakout strategy with confirmation + 1:1 RR."""

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

        # Trade state — 1 trade at a time
        self._in_trade: bool = False
        self._trade_direction: Optional[str] = None  # "LONG" or "SHORT"
        self._entry_price: Optional[float] = None
        self._stop_loss: Optional[float] = None
        self._take_profit: Optional[float] = None
        self._bracket_placed: bool = False

    def update_levels_from_candles(self, candles: List[Dict]) -> bool:
        """
        Calculate breakout levels from recent candles.
        Returns True if levels were successfully set.
        """
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

    def check_confirmation_candle(self, candles: List[Dict]) -> Optional[str]:
        """
        Check if the most recent COMPLETED candle confirms a breakout.

        A confirmation candle is one whose CLOSE is:
          - Above breakout_high → LONG signal
          - Below breakout_low  → SHORT signal

        We use candles[1] (second most recent) as the latest COMPLETED candle,
        since candles[0] is typically still forming.

        Returns: "LONG", "SHORT", or None
        """
        if not self._levels_set or not candles or len(candles) < 2:
            return None

        if self._in_trade:
            return None  # Already in a trade, skip

        # Use the most recent COMPLETED candle (index 1 = previous completed)
        confirm_candle = candles[1]
        candle_close = float(confirm_candle["close"])
        candle_high = float(confirm_candle["high"])
        candle_low = float(confirm_candle["low"])

        # LONG confirmation: candle closed above breakout high
        if candle_close >= self._breakout_high:
            logger.info(
                f"[{self.symbol}] LONG CONFIRMATION | Candle closed at "
                f"${candle_close:,.2f} >= breakout ${self._breakout_high:,.2f}"
            )
            return "LONG"

        # SHORT confirmation: candle closed below breakout low
        if candle_close <= self._breakout_low:
            logger.info(
                f"[{self.symbol}] SHORT CONFIRMATION | Candle closed at "
                f"${candle_close:,.2f} <= breakout ${self._breakout_low:,.2f}"
            )
            return "SHORT"

        return None

    def calculate_sl_tp(self, entry_price: float, direction: str) -> Dict[str, float]:
        """
        Calculate Stop Loss and Take Profit for 1:1 risk-reward.

        Uses half the candle range spread as the SL/TP distance.

        For LONG:
          SL = entry - half_spread
          TP = entry + half_spread

        For SHORT:
          SL = entry + half_spread
          TP = entry - half_spread
        """
        spread = self.range_high - self.range_low
        half_spread = spread / 2

        # Minimum distance to prevent too-tight SL/TP
        min_distance = entry_price * 0.001  # 0.1% minimum
        half_spread = max(half_spread, min_distance)

        if direction == "LONG":
            sl = round(entry_price - half_spread, 2)
            tp = round(entry_price + half_spread, 2)
        else:  # SHORT
            sl = round(entry_price + half_spread, 2)
            tp = round(entry_price - half_spread, 2)

        logger.info(
            f"[{self.symbol}] {direction} | Entry: ${entry_price:,.2f} | "
            f"SL: ${sl:,.2f} | TP: ${tp:,.2f} | "
            f"Risk: ${half_spread:,.2f} | RR: 1:1"
        )

        return {"stop_loss": sl, "take_profit": tp, "risk": half_spread}

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
        """Check if currently in an active trade."""
        return self._in_trade

    def get_status(self) -> dict:
        """Get current strategy status for dashboard display."""
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
        }
