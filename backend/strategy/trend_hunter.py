"""
Trend Hunter Strategy — Stop-order breakout with EMA trend filter.

Strategy:
  1. Calculate breakout range from N recent candles (high/low)
  2. Compute EMA-20 from candle closes for trend direction
  3. Place STOP BUY at breakout_high (only if uptrend) and/or
     STOP SELL at breakout_low (only if downtrend)
  4. When one fills → cancel the other → attach bracket SL/TP
  5. Trail the SL behind price as it moves in your favor
  6. If not filled in `order_expiry_seconds` → cancel and recalculate
  7. Only 1 trade at a time per symbol

Key difference from limit-order approach:
  Stop orders sit dormant on the exchange until price *reaches* the
  stop_price, preventing instant fills when price is inside the range.

Improvements:
  * STOP orders instead of LIMIT orders for breakout entries
  * EMA trend filter — only trade WITH the trend direction
  * Range-squeeze filter — skip when range is too narrow (noise)
  * ATR-based SL/TP with min/max bounds
  * Trailing stop-loss to lock in profits
  * Volume filter for breakout confirmation
  * Persistable snapshot for crash-safe state recovery
"""
import time
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class TrendHunterStrategy:
    """Stop-order breakout strategy with EMA trend filter and trailing SL."""

    def __init__(self, symbol: str, quantity: int = 10,
                 breakout_buffer_pct: float = 0.05,
                 sl_atr_mult: float = 1.5,
                 tp_atr_mult: float = 2.5,
                 min_sl_pct: float = 0.15,
                 max_sl_pct: float = 1.5,
                 require_volume_confirmation: bool = True,
                 order_expiry_seconds: int = 600,
                 ema_period: int = 20,
                 min_range_pct: float = 0.08,
                 use_trailing_sl: bool = True,
                 trail_activation_pct: float = 0.15):
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

        # EMA & trend filter
        self.ema_period = ema_period
        self._ema_value: Optional[float] = None
        self._trend: str = "NEUTRAL"   # "BULLISH", "BEARISH", "NEUTRAL"

        # Range filter
        self.min_range_pct = min_range_pct

        # Trailing stop
        self.use_trailing_sl = use_trailing_sl
        self.trail_activation_pct = trail_activation_pct

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

        # Stop order tracking
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
        self._best_price: Optional[float] = None  # for trailing SL

    # ── Levels, EMA & filters ──

    def update_levels_from_candles(self, candles: List[Dict],
                                   lookback: int = 8) -> bool:
        """Calculate breakout range, ATR, EMA, and avg volume from candles.

        Args:
            candles: List of candle dicts (newest first).
            lookback: Number of recent candles for range calculation.
        """
        if not candles or len(candles) < 2:
            return False

        # Use only `lookback` candles for range, but all candles for EMA
        range_candles = candles[:lookback]

        highs = [float(c["high"]) for c in range_candles]
        lows = [float(c["low"]) for c in range_candles]
        volumes = [float(c.get("volume", 0)) for c in range_candles]

        self.range_high = max(highs)
        self.range_low = min(lows)
        self.candle_count = len(range_candles)

        # True range per candle (Wilder simplified)
        ordered = list(reversed(range_candles))
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

        # ── EMA computation ──
        self._compute_ema(candles)

        # Breakout levels with buffer
        self._breakout_high = self.range_high * (1 + self.breakout_buffer_pct / 100)
        self._breakout_low = self.range_low * (1 - self.breakout_buffer_pct / 100)
        self._levels_set = True

        spread = self.range_high - self.range_low
        spread_pct = (spread / self.range_low * 100) if self.range_low else 0

        ema_str = f"${self._ema_value:,.2f}" if self._ema_value else "N/A"
        vol_str = (
            f"Vol(last/avg): {self._last_candle_volume:.0f}/{self._avg_volume:.0f}"
            if self._avg_volume is not None else ""
        )

        logger.info(
            f"[{self.symbol}] Levels | H: ${self.range_high:,.2f} L: ${self.range_low:,.2f} | "
            f"Spread: ${spread:,.2f} ({spread_pct:.3f}%) | ATR: ${self._atr:,.2f} | "
            f"EMA-{self.ema_period}: {ema_str} | Trend: {self._trend} | {vol_str}"
        )
        return True

    def _compute_ema(self, candles: List[Dict]):
        """Compute EMA from candle closes (candles are newest-first)."""
        closes = [float(c["close"]) for c in reversed(candles)]  # oldest-first
        if len(closes) < self.ema_period:
            # Not enough data — use SMA as approximation
            self._ema_value = sum(closes) / len(closes) if closes else None
        else:
            # Seed with SMA of first `ema_period` closes
            sma = sum(closes[:self.ema_period]) / self.ema_period
            multiplier = 2.0 / (self.ema_period + 1)
            ema = sma
            for price in closes[self.ema_period:]:
                ema = (price - ema) * multiplier + ema
            self._ema_value = ema

        # Determine trend
        if self._ema_value is not None and candles:
            last_close = float(candles[0]["close"])
            margin = self._ema_value * 0.001  # 0.1% dead zone
            if last_close > self._ema_value + margin:
                self._trend = "BULLISH"
            elif last_close < self._ema_value - margin:
                self._trend = "BEARISH"
            else:
                self._trend = "NEUTRAL"

    def passes_volume_filter(self) -> bool:
        """Return True if volume confirmation is satisfied (or disabled)."""
        if not self.require_volume_confirmation:
            return True
        if not self._avg_volume or not self._last_candle_volume:
            return True  # not enough data — don't block
        return self._last_candle_volume >= self._avg_volume * 0.8

    def passes_range_filter(self) -> bool:
        """Return True if range spread is above minimum threshold."""
        if not self.range_high or not self.range_low or self.range_low <= 0:
            return False
        spread_pct = (self.range_high - self.range_low) / self.range_low * 100
        if spread_pct < self.min_range_pct:
            logger.info(
                f"[{self.symbol}] Range too tight: {spread_pct:.4f}% "
                f"< {self.min_range_pct}% — skipping"
            )
            return False
        return True

    def get_allowed_sides(self) -> List[str]:
        """Return which breakout sides are allowed based on EMA trend.

        BULLISH → only buy breakouts (trade with the uptrend)
        BEARISH → only sell breakouts (trade with the downtrend)
        NEUTRAL → both sides allowed
        """
        if self._trend == "BULLISH":
            return ["buy"]
        elif self._trend == "BEARISH":
            return ["sell"]
        return ["buy", "sell"]

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
        """ATR-based SL/TP with strict 1:1 Risk:Reward ratio."""
        atr = self._atr or 0.0
        sl_dist = atr * self.sl_atr_mult

        # Apply min/max bounds (% of entry)
        min_dist = entry_price * (self.min_sl_pct / 100.0)
        max_dist = entry_price * (self.max_sl_pct / 100.0)
        sl_dist = max(min_dist, min(sl_dist, max_dist))

        # Strict 1:1 Risk:Reward — TP distance always equals SL distance
        tp_dist = sl_dist

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

    # ── Trailing Stop ──

    def calculate_trailing_sl(self, current_price: float) -> Optional[float]:
        """Calculate a new trailing SL if price has moved favorably.

        Returns the new SL price, or None if no update is needed.
        The SL only moves in the profitable direction (never widens).
        """
        if not self.use_trailing_sl or not self._in_trade:
            return None
        if not self._entry_price or not self._stop_loss or not self._atr:
            return None

        direction = self._trade_direction

        # Initialize best_price on first call
        if self._best_price is None:
            self._best_price = self._entry_price

        # Activation check: only start trailing after price moves past activation threshold
        activation_dist = self._entry_price * (self.trail_activation_pct / 100.0)
        if direction == "LONG":
            if current_price < self._entry_price + activation_dist:
                return None
            # Update best price
            if current_price > self._best_price:
                self._best_price = current_price
            # Trail SL: best_price - ATR * sl_mult
            trail_dist = self._atr * self.sl_atr_mult
            new_sl = round(self._best_price - trail_dist, 2)
            # Only move SL up (never down)
            if new_sl > self._stop_loss:
                logger.info(
                    f"[{self.symbol}] Trailing SL UP: ${self._stop_loss:,.2f} → ${new_sl:,.2f} "
                    f"(best={self._best_price:,.2f}, now={current_price:,.2f})"
                )
                self._stop_loss = new_sl
                return new_sl
        else:  # SHORT
            if current_price > self._entry_price - activation_dist:
                return None
            if current_price < self._best_price:
                self._best_price = current_price
            trail_dist = self._atr * self.sl_atr_mult
            new_sl = round(self._best_price + trail_dist, 2)
            if new_sl < self._stop_loss:
                logger.info(
                    f"[{self.symbol}] Trailing SL DOWN: ${self._stop_loss:,.2f} → ${new_sl:,.2f} "
                    f"(best={self._best_price:,.2f}, now={current_price:,.2f})"
                )
                self._stop_loss = new_sl
                return new_sl

        return None

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
            f"[{self.symbol}] STOP orders armed | "
            f"BUY #{long_order_id} @ ${self._breakout_high:,.2f} | "
            f"SELL #{short_order_id} @ ${self._breakout_low:,.2f} | "
            f"Trend: {self._trend}"
            if self._breakout_high and self._breakout_low else
            f"[{self.symbol}] Orders armed | "
            f"BUY #{long_order_id} | SELL #{short_order_id}"
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
        self._best_price = entry_price  # initialize trailing
        self.current_position = direction
        self.clear_orders()

    def exit_trade(self):
        self._in_trade = False
        self._trade_direction = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None
        self._bracket_placed = False
        self._best_price = None
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
            "best_price": self._best_price,
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
        self._best_price = snap.get("best_price")
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
            "ema": self._ema_value,
            "trend": self._trend,
            "breakout_high": self._breakout_high,
            "breakout_low": self._breakout_low,
            "last_price": self.last_price,
            "candle_count": self.candle_count,
            "in_trade": self._in_trade,
            "trade_direction": self._trade_direction,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "best_price": self._best_price,
            "orders_placed": self._orders_placed,
            "orders_placed_at": self._orders_placed_time or None,
            "order_expiry_seconds": self._order_expiry_seconds,
            "long_order_id": self._long_order_id,
            "short_order_id": self._short_order_id,
        }
