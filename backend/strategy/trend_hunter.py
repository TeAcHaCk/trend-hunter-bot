"""
Trend Hunter Strategy — 15-Minute Candle Breakout (Intraday).
NO EMAs, NO RSI, NO lagging indicators.

Rules:
- Fetch last N candles (15-min default) to build a consolidation range
- If current price breaks ABOVE the range high (+buffer) → LONG signal
- If current price breaks BELOW the range low (-buffer) → SHORT signal
- Buffer prevents false breakouts on small wicks
"""
import logging
from typing import Optional, List, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class TrendHunterStrategy:
    """Intraday breakout strategy using recent candle highs/lows."""

    def __init__(self, symbol: str, quantity: int, breakout_buffer_pct: float = 0.1):
        """
        Args:
            symbol: Trading pair (e.g., "BTCUSD")
            quantity: Number of contracts to trade
            breakout_buffer_pct: % buffer above high / below low (e.g., 0.1 = 0.1%)
        """
        self.symbol = symbol
        self.quantity = quantity
        self.breakout_buffer_pct = breakout_buffer_pct
        self.current_position: Optional[str] = None  # "LONG", "SHORT", or None
        self.enabled: bool = True

        # Price tracking
        self.last_price: Optional[float] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.candle_count: int = 0

        # Computed breakout levels
        self._breakout_high: Optional[float] = None
        self._breakout_low: Optional[float] = None
        self._levels_set: bool = False

    def update_levels(self, high: float, low: float):
        """Legacy method — update from raw high/low values."""
        self.range_high = high
        self.range_low = low
        self._breakout_high = high * (1 + self.breakout_buffer_pct / 100)
        self._breakout_low = low * (1 - self.breakout_buffer_pct / 100)
        self._levels_set = True

    def update_levels_from_candles(self, candles: List[Dict]) -> bool:
        """
        Calculate breakout levels from recent candles.

        Takes the highest high and lowest low across the candle window
        to define the consolidation range.

        Args:
            candles: List of candle dicts with 'high', 'low', 'close' keys

        Returns:
            True if levels were set successfully
        """
        if not candles or len(candles) < 2:
            logger.warning(f"[{self.symbol}] Not enough candles ({len(candles) if candles else 0}) to set levels")
            return False

        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        self.range_high = max(highs)
        self.range_low = min(lows)
        self.candle_count = len(candles)

        # Apply buffer
        self._breakout_high = self.range_high * (1 + self.breakout_buffer_pct / 100)
        self._breakout_low = self.range_low * (1 - self.breakout_buffer_pct / 100)
        self._levels_set = True

        spread = self.range_high - self.range_low
        spread_pct = (spread / self.range_low * 100) if self.range_low > 0 else 0

        logger.info(
            f"[{self.symbol}] Candle range ({self.candle_count} candles) | "
            f"H: ${self.range_high:,.2f} L: ${self.range_low:,.2f} | "
            f"Spread: ${spread:,.2f} ({spread_pct:.2f}%) | "
            f"Breakout: ↑${self._breakout_high:,.2f} / ↓${self._breakout_low:,.2f}"
        )
        return True

    def check_signal(self, current_price: float) -> Optional[str]:
        """
        Check if current price triggers a breakout signal.

        Returns:
            "LONG" if price breaks above range high + buffer
            "SHORT" if price breaks below range low - buffer
            None if no signal
        """
        self.last_price = current_price

        if not self.enabled:
            return None

        if not self._levels_set or self._breakout_high is None or self._breakout_low is None:
            return None

        if current_price >= self._breakout_high:
            logger.info(
                f"[{self.symbol}] LONG signal! Price ${current_price:,.2f} >= "
                f"breakout high ${self._breakout_high:,.2f}"
            )
            return "LONG"
        elif current_price <= self._breakout_low:
            logger.info(
                f"[{self.symbol}] SHORT signal! Price ${current_price:,.2f} <= "
                f"breakout low ${self._breakout_low:,.2f}"
            )
            return "SHORT"

        return None

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
        }
