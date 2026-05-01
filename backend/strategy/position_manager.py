"""
Position Manager — Manages position lifecycle and safety guards.
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
        self._positions: Dict[str, Dict] = {}

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
        self._product_ids[symbol] = product_id

    async def get_product_id(self, symbol: str) -> Optional[int]:
        if symbol not in self._product_ids:
            pid = await self.client.get_product_id(symbol)
            if pid:
                self._product_ids[symbol] = pid
        return self._product_ids.get(symbol)

    def check_cooldown(self, symbol: str) -> bool:
        last_trade = self._last_trade_time.get(symbol)
        if last_trade:
            elapsed = datetime.utcnow() - last_trade
            if elapsed < timedelta(minutes=self.cooldown_minutes):
                remaining = timedelta(minutes=self.cooldown_minutes) - elapsed
                logger.info(f"[{symbol}] Cooldown active: {remaining.seconds}s remaining")
                return False
        return True

    def check_daily_loss(self) -> bool:
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
        pos = self._positions.get(symbol)
        return pos["side"] if pos else None

    def record_pnl(self, pnl: float):
        """Add a realised PnL amount to the daily tracker."""
        self._daily_pnl += pnl

    def mark_trade_time(self, symbol: str):
        self._last_trade_time[symbol] = datetime.utcnow()

    def sync_positions_from_exchange(self, exchange_positions: list):
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
        return {
            "positions": self._positions.copy(),
            "daily_pnl": self._daily_pnl,
            "bot_paused_by_guard": self._bot_paused_by_guard,
            "cooldown_minutes": self.cooldown_minutes,
            "max_daily_loss": self.max_daily_loss,
            "leverage": self.leverage,
        }

    def reset_daily_pnl(self):
        self._daily_pnl = 0.0
        self._bot_paused_by_guard = False
        logger.info("Daily PnL reset and bot unpaused")
