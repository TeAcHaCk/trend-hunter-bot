"""
Bot Runner — APScheduler-based execution loop for the Trend Hunter strategy.
"""
import logging
import asyncio
from datetime import datetime
from typing import Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.exchange.delta_client import DeltaClient
from backend.exchange.delta_ws import delta_ws
from backend.strategy.trend_hunter import TrendHunterStrategy
from backend.strategy.position_manager import PositionManager
from backend.models.database import async_session
from backend.models.trade_log import TradeLog
from backend.config import settings

logger = logging.getLogger(__name__)


class BotRunner:
    """Manages the trading bot lifecycle and strategy execution."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.delta_client = DeltaClient()
        self.position_manager = PositionManager(self.delta_client)

        # Strategies per symbol
        self.strategies: Dict[str, TrendHunterStrategy] = {}

        # Bot state
        self.state: str = "STOPPED"  # RUNNING, PAUSED, STOPPED
        self._initialized: bool = False

        # Stats
        self.last_check_time: Optional[str] = None
        self.total_signals: int = 0
        self.total_trades: int = 0

    def initialize(self, bot_settings: Dict = None):
        """Initialize strategies with settings."""
        s = bot_settings or settings.DEFAULT_SETTINGS

        # Candle settings
        self.candle_resolution = s.get("candle_resolution", "15m")
        self.lookback_candles = s.get("lookback_candles", 6)

        # Create/update BTC strategy
        if s.get("btc_enabled", True):
            self.strategies["BTCUSD"] = TrendHunterStrategy(
                symbol="BTCUSD",
                quantity=s.get("btc_quantity", 10),
                breakout_buffer_pct=s.get("breakout_buffer_pct", 0.1),
            )
        elif "BTCUSD" in self.strategies:
            self.strategies["BTCUSD"].enabled = False

        # Create/update ETH strategy
        if s.get("eth_enabled", True):
            self.strategies["ETHUSD"] = TrendHunterStrategy(
                symbol="ETHUSD",
                quantity=s.get("eth_quantity", 10),
                breakout_buffer_pct=s.get("breakout_buffer_pct", 0.1),
            )
        elif "ETHUSD" in self.strategies:
            self.strategies["ETHUSD"].enabled = False

        # Position manager settings
        self.position_manager.cooldown_minutes = s.get("cooldown_minutes", 5)
        self.position_manager.max_daily_loss = s.get("max_daily_loss", 100.0)
        self.position_manager.leverage = s.get("leverage", 10)

        self._initialized = True
        logger.info(
            f"Bot initialized | Candles: {self.lookback_candles}x {self.candle_resolution} | "
            f"Buffer: {s.get('breakout_buffer_pct', 0.1)}%"
        )

    def update_client(self, api_key: str, api_secret: str):
        """Update the Delta client with new API credentials."""
        self.delta_client = DeltaClient(api_key=api_key, api_secret=api_secret)
        self.position_manager.client = self.delta_client
        logger.info("Delta client updated with new credentials")

    async def _resolve_product_ids(self):
        """Resolve and cache product IDs for all symbols."""
        for symbol in self.strategies:
            pid = self.position_manager.get_product_id(symbol)
            if not pid:
                logger.warning(f"Could not resolve product ID for {symbol}")

    async def _run_strategy_check(self):
        """Main strategy execution loop - runs on scheduler interval."""
        if self.state != "RUNNING":
            return

        self.last_check_time = datetime.utcnow().isoformat()

        for symbol, strategy in self.strategies.items():
            if not strategy.enabled:
                continue

            try:
                # Fetch recent candles for breakout range
                candles = self.delta_client.get_candles(
                    symbol=symbol,
                    resolution=self.candle_resolution,
                    num_candles=self.lookback_candles,
                )

                if not candles or len(candles) < 2:
                    logger.warning(f"[{symbol}] Not enough candle data (got {len(candles) if candles else 0})")
                    continue

                # Get current price from latest candle or WebSocket
                price_data = delta_ws.get_price(symbol)
                if price_data:
                    current_price = float(price_data.get("close") or price_data.get("mark_price", 0))
                else:
                    current_price = float(candles[0]["close"])

                if not current_price:
                    logger.warning(f"[{symbol}] No current price available")
                    continue

                # Update strategy with candle-based breakout levels
                strategy.update_levels_from_candles(candles)

                # Log price vs breakout levels
                pos_str = self.position_manager.get_current_position(symbol) or 'None'
                dist_high = strategy._breakout_high - current_price if strategy._breakout_high else 0
                dist_low = current_price - strategy._breakout_low if strategy._breakout_low else 0
                logger.info(
                    f"[{symbol}] Price: ${current_price:,.2f} | "
                    f"Breakout: UP ${strategy._breakout_high:,.2f} (${dist_high:,.2f} away) / "
                    f"DOWN ${strategy._breakout_low:,.2f} (${dist_low:,.2f} away) | Pos: {pos_str}"
                )

                # Check for signal
                signal = strategy.check_signal(current_price)
                current_pos = self.position_manager.get_current_position(symbol)

                if signal and signal != current_pos:
                    self.total_signals += 1
                    logger.info(f"[{symbol}] SIGNAL: {signal} (current pos: {current_pos})")

                    # Execute trade
                    result = await self.position_manager.execute_signal(
                        symbol=symbol,
                        signal=signal,
                        quantity=strategy.quantity,
                        current_price=current_price,
                    )

                    if result and result.get("success"):
                        strategy.current_position = signal
                        self.total_trades += 1
                        await self._log_trade(result)
                    elif result:
                        logger.warning(f"[{symbol}] Trade execution failed: {result.get('error')}")

            except Exception as e:
                logger.error(f"[{symbol}] Strategy check error: {e}", exc_info=True)

    async def _log_trade(self, trade_result: Dict):
        """Log a trade to the database."""
        try:
            async with async_session() as session:
                trade = TradeLog(
                    timestamp=datetime.utcnow(),
                    symbol=trade_result.get("symbol", ""),
                    direction=trade_result.get("signal", ""),
                    entry_price=trade_result.get("entry_price", 0),
                    quantity=trade_result.get("quantity", 0),
                    status="open",
                    order_id=str(trade_result.get("order_id", "")),
                    notes=str(trade_result.get("actions", [])),
                )

                # If this was a reversal, also log the close
                if "close_pnl" in trade_result:
                    trade.notes += f" | Close PnL: {trade_result['close_pnl']:.2f}"

                session.add(trade)
                await session.commit()
                logger.info(f"Trade logged: {trade.symbol} {trade.direction}")
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    async def _health_check(self):
        """Periodic health check for WebSocket connection."""
        if not delta_ws.is_connected:
            logger.warning("WebSocket disconnected — attempting reconnect")
            symbols = [s for s in self.strategies if self.strategies[s].enabled]
            await delta_ws.start(symbols)

    async def start(self):
        """Start the trading bot."""
        if self.state == "RUNNING":
            logger.warning("Bot is already running")
            return

        if not self._initialized:
            self.initialize()

        # Resolve product IDs
        await self._resolve_product_ids()

        # Set leverage for all products
        for symbol in self.strategies:
            pid = self.position_manager.get_product_id(symbol)
            if pid:
                try:
                    self.delta_client.set_leverage(pid, self.position_manager.leverage)
                except Exception as e:
                    logger.warning(f"Could not set leverage for {symbol}: {e}")

        # Start WebSocket feed
        enabled_symbols = [s for s in self.strategies if self.strategies[s].enabled]
        await delta_ws.start(enabled_symbols)

        # Schedule strategy checks
        interval = settings.DEFAULT_SETTINGS.get("poll_interval_seconds", 30)
        self.scheduler.add_job(
            self._run_strategy_check,
            "interval",
            seconds=interval,
            id="strategy_check",
            replace_existing=True,
        )

        # Schedule health checks every 60s
        self.scheduler.add_job(
            self._health_check,
            "interval",
            seconds=60,
            id="health_check",
            replace_existing=True,
        )

        if not self.scheduler.running:
            self.scheduler.start()

        self.state = "RUNNING"
        logger.info(f"Bot STARTED | Checking every {interval}s | Symbols: {enabled_symbols}")

    async def stop(self):
        """Stop the trading bot."""
        self.state = "STOPPED"

        if self.scheduler.running:
            self.scheduler.remove_all_jobs()
            self.scheduler.shutdown(wait=False)

        await delta_ws.stop()
        logger.info("🛑 Bot STOPPED")

    async def pause(self):
        """Pause the trading bot (keeps WebSocket alive)."""
        self.state = "PAUSED"
        logger.info("⏸️ Bot PAUSED")

    async def resume(self):
        """Resume a paused bot."""
        if self.state == "PAUSED":
            self.state = "RUNNING"
            logger.info("▶️ Bot RESUMED")

    def get_status(self) -> Dict:
        """Get comprehensive bot status."""
        strategies_status = {}
        for symbol, strategy in self.strategies.items():
            status = strategy.get_status()
            status["current_position"] = self.position_manager.get_current_position(symbol)
            strategies_status[symbol] = status

        return {
            "state": self.state,
            "ws_connected": delta_ws.is_connected,
            "last_check_time": self.last_check_time,
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "strategies": strategies_status,
            "position_manager": self.position_manager.get_status(),
            "prices": delta_ws.price_cache,
            "testnet": settings.DELTA_TESTNET,
        }


# Global bot instance
bot_runner = BotRunner()
