"""
Trend Hunter Strategy — Limit orders at breakout levels.

Strategy:
  1. Calculate breakout range from N recent candles (high/low)
  2. Place limit BUY at breakout_high and limit SELL at breakout_low
  3. When one fills → cancel the other → attach bracket SL/TP
  4. If not filled in `order_expiry_seconds` → cancel and recalculate
  5. Only 1 trade at a time per symbol

Improvements over the previous version:
  * ATR-based stop-loss/take-profit with sane min/max bounds, instead of
    half-spread (which collapses to ~zero in low-volatility windows).
  * Optional volume filter — last candle must show above-average volume
    before we arm new breakout orders.
  * Trade-log row id tracking so the same DB row gets updated on close.
  * Persistable snapshot for crash-safe state recovery.
"""
import time
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class TrendHunterStrategy:
    """Limit-order breakout strategy with ATR-tuned bracket exits."""

    def __init__(self, symbol: str, quantity: int = 10,
                 breakout_buffer_pct: float = 0.1,
                 sl_atr_mult: float = 1.0,
                 tp_atr_mult: float = 1.5,
                 min_sl_pct: float = 0.15,
                 max_sl_pct: float = 1.5,
                 require_volume_confirmation: bool = False,
                 order_expiry_seconds: int = 1800):
        self.symbol = symbol
        self.quantity = quantity
        self.breakout_buffer_pct = breakout_buffer_pct
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.min_sl_pct = min_sl_pct
        self.max_sl_pct = max_sl_pct
        self.require_volume_confirmation = require_volume_confirmation
        self.enabled: bool = True
        self.current_position: Optional[str] = None

        # Price tracking
        self.last_price: Optional[float] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.candle_count: int = 0
        self._atr: Optional[float] = None
        self._avg_volume: Optional[float] = None
        self._last_candle_volume: Optional[float] = None

        # Computed breakout levels
        self._breakout_high: Optional[float] = None
        self._breakout_low: Optional[float] = None
        self._levels_set: bool = False

        # Limit order tracking
        self._long_order_id: Optional[int] = None
        self._short_order_id: Optional[int] = None
        self._long_client_order_id: Optional[str] = None
        self._short_client_order_id: Optional[str] = None
        self._orders_placed: bool = False
        self._orders_placed_time: float = 0
        self._order_expiry_seconds: int = order_expiry_seconds

        # Trade state — 1 trade at a time
        self._in_trade: bool = False
        self._trade_direction: Optional[str] = None
        self._entry_price: Optional[float] = None
        self._stop_loss: Optional[float] = None
        self._take_profit: Optional[float] = None
        self._bracket_placed: bool = False
        self._open_trade_log_id: Optional[int] = None

    # ── Levels & filters ──
    def update_levels_from_candles(self, candles: List[Dict]) -> bool:
        """Calculate breakout range, ATR, and avg volume from recent candles."""
        if not candles or len(candles) < 2:
            return False

        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]

        self.range_high = max(highs)
        self.range_low = min(lows)
        self.candle_count = len(candles)

        # True range per candle (Wilder simplified): max(H-L, |H-prevC|, |L-prevC|)
        # Candles are newest-first; iterate oldest→newest for prev-close.
        ordered = list(reversed(candles))
        trs: List[float] = []
        prev_close: Optional[float] = None
        for c in ordered:
            h = float(c["high"]); l = float(c["low"]); cl = float(c["close"])
            if prev_close is None:
                trs.append(h - l)
            else:
                trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
            prev_close = cl
        self._atr = sum(trs) / len(trs) if trs else 0.0

        if volumes:
            self._avg_volume = sum(volumes) / len(volumes)
            self._last_candle_volume = volumes[0]  # newest

        # Breakout levels with buffer
        self._breakout_high = self.range_high * (1 + self.breakout_buffer_pct / 100)
        self._breakout_low = self.range_low * (1 - self.breakout_buffer_pct / 100)
        self._levels_set = True

        spread = self.range_high - self.range_low
        spread_pct = (spread / self.range_low * 100) if self.range_low else 0

        logger.info(
            f"[{self.symbol}] Levels | H: ${self.range_high:,.2f} L: ${self.range_low:,.2f} | "
            f"Spread: ${spread:,.2f} ({spread_pct:.2f}%) | ATR: ${self._atr:,.2f} | "
            f"Vol(last/avg): {self._last_candle_volume:.0f}/{self._avg_volume:.0f}"
            if self._avg_volume is not None else
            f"[{self.symbol}] Levels | H: ${self.range_high:,.2f} L: ${self.range_low:,.2f} | "
            f"Spread: ${spread:,.2f} ({spread_pct:.2f}%) | ATR: ${self._atr:,.2f}"
        )
        return True

    def passes_volume_filter(self) -> bool:
        """Return True if volume confirmation is satisfied (or disabled)."""
        if not self.require_volume_confirmation:
            return True
        if not self._avg_volume or not self._last_candle_volume:
            return True  # not enough data — don't block
        return self._last_candle_volume >= self._avg_volume * 0.8

    def needs_new_orders(self) -> bool:
        if self._in_trade or not self._levels_set or self._orders_placed:
            return False
        return True

    def are_orders_expired(self) -> bool:
        if not self._orders_placed:
            return False
        return (time.time() - self._orders_placed_time) >= self._order_expiry_seconds

    # ── SL/TP ──
    def calculate_sl_tp(self, entry_price: float, direction: str) -> Dict[str, float]:
        """ATR-based SL/TP with min/max bounds in % of entry price."""
        atr = self._atr or 0.0
        sl_dist = atr * self.sl_atr_mult
        tp_dist = atr * self.tp_atr_mult

        # Apply min/max bounds (% of entry) so ATR≈0 doesn't cause instant stop-outs
        # and runaway ATR doesn't put the stop absurdly far away.
        min_dist = entry_price * (self.min_sl_pct / 100.0)
        max_dist = entry_price * (self.max_sl_pct / 100.0)
        sl_dist = max(min_dist, min(sl_dist, max_dist))
        # Keep TP at least the same distance as SL (≥1:1 RR)
        tp_dist = max(tp_dist, sl_dist)

        if direction == "LONG":
            sl = round(entry_price - sl_dist, 2)
            tp = round(entry_price + tp_dist, 2)
        else:
            sl = round(entry_price + sl_dist, 2)
            tp = round(entry_price - tp_dist, 2)

        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        logger.info(
            f"[{self.symbol}] {direction} | Entry: ${entry_price:,.2f} | "
            f"SL: ${sl:,.2f} (${sl_dist:,.2f}) | TP: ${tp:,.2f} (${tp_dist:,.2f}) | RR: 1:{rr:.2f}"
        )
        return {"stop_loss": sl, "take_profit": tp, "risk": sl_dist}

    # ── Order tracking ──
    def record_orders_placed(self,
                             long_order_id: Optional[int] = None,
                             short_order_id: Optional[int] = None,
                             long_coid: Optional[str] = None,
                             short_coid: Optional[str] = None):
        self._long_order_id = long_order_id
        self._short_order_id = short_order_id
        self._long_client_order_id = long_coid
        self._short_client_order_id = short_coid
        self._orders_placed = True
        self._orders_placed_time = time.time()
        logger.info(
            f"[{self.symbol}] Orders armed | "
            f"BUY #{long_order_id} @ ${self._breakout_high:,.2f} | "
            f"SELL #{short_order_id} @ ${self._breakout_low:,.2f}"
        )

    def clear_orders(self):
        self._long_order_id = None
        self._short_order_id = None
        self._long_client_order_id = None
        self._short_client_order_id = None
        self._orders_placed = False
        self._orders_placed_time = 0

    def enter_trade(self, direction: str, entry_price: float,
                    stop_loss: float, take_profit: float):
        self._in_trade = True
        self._trade_direction = direction
        self._entry_price = entry_price
        self._stop_loss = stop_loss
        self._take_profit = take_profit
        self._bracket_placed = True
        self.current_position = direction
        self.clear_orders()

    def exit_trade(self):
        self._in_trade = False
        self._trade_direction = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None
        self._bracket_placed = False
        self.current_position = None
        self._open_trade_log_id = None

    def is_in_trade(self) -> bool:
        return self._in_trade

    # ── Persistence ──
    def snapshot(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "in_trade": self._in_trade,
            "trade_direction": self._trade_direction,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "open_trade_log_id": self._open_trade_log_id,
            "long_order_id": self._long_order_id,
            "short_order_id": self._short_order_id,
            "long_coid": self._long_client_order_id,
            "short_coid": self._short_client_order_id,
            "orders_placed": self._orders_placed,
            "orders_placed_time": self._orders_placed_time,
        }

    def restore(self, snap: Dict[str, Any]):
        self._in_trade = snap.get("in_trade", False)
        self._trade_direction = snap.get("trade_direction")
        self._entry_price = snap.get("entry_price")
        self._stop_loss = snap.get("stop_loss")
        self._take_profit = snap.get("take_profit")
        self._open_trade_log_id = snap.get("open_trade_log_id")
        self._long_order_id = snap.get("long_order_id")
        self._short_order_id = snap.get("short_order_id")
        self._long_client_order_id = snap.get("long_coid")
        self._short_client_order_id = snap.get("short_coid")
        self._orders_placed = snap.get("orders_placed", False)
        self._orders_placed_time = snap.get("orders_placed_time", 0)
        self.current_position = self._trade_direction
        if self._in_trade:
            self._bracket_placed = True

    def get_status(self) -> dict:
        return {
            "symbol": self.symbol,
            "enabled": self.enabled,
            "quantity": self.quantity,
            "breakout_buffer_pct": self.breakout_buffer_pct,
            "current_position": self.current_position,
            "range_high": self.range_high,
            "range_low": self.range_low,
            "atr": self._atr,
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
