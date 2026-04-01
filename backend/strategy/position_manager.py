"""
Position Manager — Manages order execution, position lifecycle, and safety guards.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict

from backend.exchange.delta_client import DeltaClient

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position lifecycle with safety guards."""

    def __init__(self, delta_client: DeltaClient):
        self.client = delta_client

        # Position tracking per symbol
        self._positions: Dict[str, Dict] = {}  # {symbol: {side, size, entry_price, product_id}}

        # Safety guards
        self._last_trade_time: Dict[str, datetime] = {}
        self._daily_pnl: float = 0.0
        self._daily_pnl_reset_date: Optional[datetime] = None
        self._bot_paused_by_guard: bool = False

        # Configurable limits
        self.cooldown_minutes: int = 5
        self.max_daily_loss: float = 100.0
        self.leverage: int = 10

        # Product ID cache
        self._product_ids: Dict[str, int] = {}

    def set_product_id(self, symbol: str, product_id: int):
        """Cache a product ID for a symbol."""
        self._product_ids[symbol] = product_id

    def get_product_id(self, symbol: str) -> Optional[int]:
        """Get cached product ID, or fetch from API."""
        if symbol not in self._product_ids:
            pid = self.client.get_product_id(symbol)
            if pid:
                self._product_ids[symbol] = pid
        return self._product_ids.get(symbol)

    def _check_cooldown(self, symbol: str) -> bool:
        """Check if we're still in cooldown period after last trade."""
        last_trade = self._last_trade_time.get(symbol)
        if last_trade:
            elapsed = datetime.utcnow() - last_trade
            if elapsed < timedelta(minutes=self.cooldown_minutes):
                remaining = timedelta(minutes=self.cooldown_minutes) - elapsed
                logger.info(f"[{symbol}] Cooldown active: {remaining.seconds}s remaining")
                return False
        return True

    def _check_daily_loss(self) -> bool:
        """Check if daily loss limit has been hit."""
        today = datetime.utcnow().date()
        if self._daily_pnl_reset_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_reset_date = today
            self._bot_paused_by_guard = False

        if self._daily_pnl <= -abs(self.max_daily_loss):
            if not self._bot_paused_by_guard:
                logger.warning(
                    f"🛑 Max daily loss hit! PnL: ${self._daily_pnl:.2f} | "
                    f"Limit: -${self.max_daily_loss:.2f}"
                )
                self._bot_paused_by_guard = True
            return False
        return True

    def get_current_position(self, symbol: str) -> Optional[str]:
        """Get the current position direction for a symbol."""
        pos = self._positions.get(symbol)
        return pos["side"] if pos else None

    async def execute_signal(self, symbol: str, signal: str, quantity: int,
                             current_price: float) -> Optional[Dict]:
        """
        Execute a trading signal with all safety checks.

        Args:
            symbol: Trading pair
            signal: "LONG" or "SHORT"
            quantity: Number of contracts
            current_price: Current market price

        Returns:
            Trade result dict or None if blocked by safety
        """
        product_id = self.get_product_id(symbol)
        if not product_id:
            logger.error(f"[{symbol}] Cannot resolve product ID")
            return None

        # Safety checks
        if not self._check_daily_loss():
            return None

        if not self._check_cooldown(symbol):
            return None

        current_pos = self.get_current_position(symbol)

        # Skip if already in the same position
        if current_pos == signal:
            logger.debug(f"[{symbol}] Already in {signal} position, skipping")
            return None

        trade_result = {
            "symbol": symbol,
            "signal": signal,
            "quantity": quantity,
            "entry_price": current_price,
            "timestamp": datetime.utcnow().isoformat(),
            "actions": [],
        }

        # Close existing position if reversing
        if current_pos:
            logger.info(f"[{symbol}] Closing {current_pos} position to reverse to {signal}")
            close_result = self.client.close_position(
                product_id=product_id,
                product_symbol=symbol,
                current_side=current_pos,
                size=self._positions[symbol]["size"],
            )

            if close_result.get("success"):
                trade_result["actions"].append(f"Closed {current_pos}")
                # Estimate PnL
                entry = self._positions[symbol].get("entry_price", current_price)
                if current_pos == "LONG":
                    pnl = (current_price - entry) * quantity
                else:
                    pnl = (entry - current_price) * quantity
                self._daily_pnl += pnl
                trade_result["close_pnl"] = pnl
                del self._positions[symbol]
            else:
                logger.error(f"[{symbol}] Failed to close position: {close_result}")
                trade_result["error"] = "Failed to close existing position"
                return trade_result

        # Open new position
        side = "buy" if signal == "LONG" else "sell"
        order_result = self.client.place_order(
            product_id=product_id,
            product_symbol=symbol,
            side=side,
            size=quantity,
            order_type="market_order",
        )

        if order_result.get("success"):
            self._positions[symbol] = {
                "side": signal,
                "size": quantity,
                "entry_price": current_price,
                "product_id": product_id,
                "order_id": order_result.get("result", {}).get("id"),
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._last_trade_time[symbol] = datetime.utcnow()
            trade_result["actions"].append(f"Opened {signal}")
            trade_result["order_id"] = order_result.get("result", {}).get("id")
            trade_result["success"] = True
            logger.info(f"[{symbol}] ✅ {signal} position opened at ~{current_price}")
        else:
            trade_result["error"] = order_result.get("error", {}).get("message", "Order failed")
            trade_result["success"] = False
            logger.error(f"[{symbol}] Failed to open {signal}: {order_result}")

        return trade_result

    def sync_positions_from_exchange(self, exchange_positions: list):
        """Sync local position tracking with actual exchange positions."""
        self._positions.clear()
        for pos in exchange_positions:
            symbol = pos.get("product", {}).get("symbol", "")
            size = int(pos.get("size", 0))
            if size != 0:
                side = "LONG" if size > 0 else "SHORT"
                self._positions[symbol] = {
                    "side": side,
                    "size": abs(size),
                    "entry_price": float(pos.get("entry_price", 0)),
                    "product_id": pos.get("product_id"),
                }

    def get_status(self) -> Dict:
        """Get current position manager status."""
        return {
            "positions": self._positions.copy(),
            "daily_pnl": self._daily_pnl,
            "bot_paused_by_guard": self._bot_paused_by_guard,
            "cooldown_minutes": self.cooldown_minutes,
            "max_daily_loss": self.max_daily_loss,
            "leverage": self.leverage,
        }

    def reset_daily_pnl(self):
        """Manually reset daily PnL and unpause bot."""
        self._daily_pnl = 0.0
        self._bot_paused_by_guard = False
        logger.info("Daily PnL reset and bot unpaused")
