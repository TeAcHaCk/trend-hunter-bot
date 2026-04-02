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

        # Candle cache — only re-fetch when a new candle forms
        self._cached_candles: Dict[str, list] = {}
        self._last_candle_time: Dict[str, int] = {}  # timestamp of newest candle per symbol

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
        """
        Main strategy loop — runs every 10 seconds.

        Flow:
          1. Fetch candles only when a NEW candle forms (~every 15 min)
          2. Lock breakout levels until next candle refresh
          3. Check confirmation every 10 seconds using cached levels
          4. If confirmed → enter market order + bracket (SL + TP)
        """
        if self.state != "RUNNING":
            return

        self.last_check_time = datetime.utcnow().isoformat()

        for symbol, strategy in self.strategies.items():
            if not strategy.enabled:
                continue

            try:
                # 1. Refresh candles if needed (first run or new candle formed)
                await self._refresh_candles_if_needed(symbol, strategy)

                # 2. Get current price
                price_data = delta_ws.get_price(symbol)
                if price_data:
                    current_price = float(price_data.get("close") or price_data.get("mark_price", 0))
                elif symbol in self._cached_candles and self._cached_candles[symbol]:
                    current_price = float(self._cached_candles[symbol][0]["close"])
                else:
                    current_price = 0

                if not current_price:
                    logger.warning(f"[{symbol}] No price available")
                    continue

                strategy.last_price = current_price

                # 3. If levels not set yet, skip
                if not strategy._levels_set:
                    logger.warning(f"[{symbol}] Levels not set yet")
                    continue

                # 4. If already in a trade — monitor position
                if strategy.is_in_trade():
                    await self._monitor_position(symbol, strategy, current_price)
                    continue

                # 5. Safety checks
                if not self.position_manager._check_daily_loss():
                    continue
                if not self.position_manager._check_cooldown(symbol):
                    continue

                # 6. Check for confirmation candle (uses cached candles)
                candles = self._cached_candles.get(symbol, [])
                if not candles or len(candles) < 3:
                    continue

                signal = strategy.check_confirmation_candle(candles)
                if not signal:
                    dist_high = strategy._breakout_high - current_price if strategy._breakout_high else 0
                    dist_low = current_price - strategy._breakout_low if strategy._breakout_low else 0
                    logger.info(
                        f"[{symbol}] ${current_price:,.2f} | "
                        f"Waiting | UP ${strategy._breakout_high:,.2f} (${dist_high:,.2f}) / "
                        f"DOWN ${strategy._breakout_low:,.2f} (${dist_low:,.2f})"
                    )
                    continue

                # 7. SIGNAL CONFIRMED — Execute trade
                self.total_signals += 1
                logger.info(f"[{symbol}] CONFIRMED {signal} BREAKOUT!")

                await self._execute_entry(symbol, strategy, signal, current_price)

            except Exception as e:
                logger.error(f"[{symbol}] Strategy check error: {e}", exc_info=True)

    async def _refresh_candles_if_needed(self, symbol: str, strategy: TrendHunterStrategy):
        """
        Fetch new candles only when a new candle has formed.
        For 15m candles, this means ~every 15 minutes.
        Keeps breakout levels LOCKED between refreshes.
        """
        import time as _time

        resolution_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "1d": 86400,
        }
        interval = resolution_seconds.get(self.candle_resolution, 900)
        now = int(_time.time())
        last_fetch_time = self._last_candle_time.get(symbol, 0)

        # Only refresh if enough time has passed for a new candle to form
        if last_fetch_time > 0 and (now - last_fetch_time) < interval:
            return  # Levels are locked, no refresh needed

        # Fetch new candles
        candles = self.delta_client.get_candles(
            symbol=symbol,
            resolution=self.candle_resolution,
            num_candles=self.lookback_candles,
        )

        if not candles or len(candles) < 3:
            return

        newest_time = int(candles[0].get("time", 0))

        # Only update levels if we actually got a NEW candle
        if newest_time != last_fetch_time:
            self._cached_candles[symbol] = candles
            self._last_candle_time[symbol] = newest_time
            strategy.update_levels_from_candles(candles)
            logger.info(
                f"[{symbol}] LEVELS REFRESHED | New candle at {newest_time} | "
                f"Locked for next {self.candle_resolution}"
            )
        else:
            # Same candle — just update the cache timestamp to prevent re-fetching
            self._last_candle_time[symbol] = now

    async def _execute_entry(self, symbol: str, strategy: TrendHunterStrategy,
                             signal: str, current_price: float):
        """Execute a market entry order and attach bracket (SL + TP)."""
        product_id = self.position_manager.get_product_id(symbol)
        if not product_id:
            logger.error(f"[{symbol}] Cannot resolve product ID")
            return

        # Calculate SL/TP at 1:1 RR
        levels = strategy.calculate_sl_tp(current_price, signal)
        sl_price = levels["stop_loss"]
        tp_price = levels["take_profit"]

        # Place market entry order
        side = "buy" if signal == "LONG" else "sell"
        order_result = self.delta_client.place_order(
            product_id=product_id,
            product_symbol=symbol,
            side=side,
            size=strategy.quantity,
            order_type="market_order",
        )

        if not order_result.get("success") and not order_result.get("result"):
            logger.error(f"[{symbol}] Entry order FAILED: {order_result}")
            return

        logger.info(f"[{symbol}] ENTERED {signal} @ ~${current_price:,.2f}")

        # Place bracket order (SL + TP) on the position
        bracket_result = self.delta_client.place_bracket_order(
            product_id=product_id,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
        )

        if bracket_result.get("success") or bracket_result.get("result"):
            logger.info(
                f"[{symbol}] Bracket placed | SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}"
            )
        else:
            logger.warning(f"[{symbol}] Bracket order failed: {bracket_result}")

        # Record trade state
        strategy.enter_trade(signal, current_price, sl_price, tp_price)
        self.position_manager._positions[symbol] = {
            "side": signal,
            "size": strategy.quantity,
            "entry_price": current_price,
            "product_id": product_id,
        }
        self.position_manager._last_trade_time[symbol] = datetime.utcnow()
        self.total_trades += 1

        # Log to database
        await self._log_trade({
            "symbol": symbol,
            "signal": signal,
            "quantity": strategy.quantity,
            "entry_price": current_price,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "risk": levels["risk"],
            "timestamp": datetime.utcnow().isoformat(),
            "actions": [f"Market {signal}", f"SL: {sl_price}", f"TP: {tp_price}"],
            "success": True,
        })

    async def _monitor_position(self, symbol: str, strategy: TrendHunterStrategy,
                                current_price: float):
        """Monitor an open position — check if SL/TP has been hit."""
        product_id = self.position_manager.get_product_id(symbol)
        if not product_id:
            return

        # Check actual position on exchange
        pos_data = self.delta_client.get_position(product_id)
        position_result = pos_data.get("result", {})

        # Get position size — if 0, position was closed (SL or TP hit)
        actual_size = int(position_result.get("size", 0)) if position_result else 0

        if actual_size == 0:
            # Position closed — SL or TP was hit
            entry_price = strategy._entry_price or 0
            pnl = 0
            if strategy._trade_direction == "LONG":
                pnl = (current_price - entry_price) * strategy.quantity
            else:
                pnl = (entry_price - current_price) * strategy.quantity

            exit_type = "TP" if pnl > 0 else "SL"
            logger.info(
                f"[{symbol}] POSITION CLOSED by {exit_type} | "
                f"Entry: ${entry_price:,.2f} | Exit: ~${current_price:,.2f} | "
                f"PnL: ${pnl:,.2f}"
            )

            self.position_manager._daily_pnl += pnl
            if symbol in self.position_manager._positions:
                del self.position_manager._positions[symbol]

            strategy.exit_trade()
            return

        # Position still open — log status
        entry_price = strategy._entry_price or current_price
        unrealized_pnl = 0
        if strategy._trade_direction == "LONG":
            unrealized_pnl = (current_price - entry_price) * strategy.quantity
        else:
            unrealized_pnl = (entry_price - current_price) * strategy.quantity

        logger.info(
            f"[{symbol}] IN {strategy._trade_direction} | "
            f"Entry: ${entry_price:,.2f} | Now: ${current_price:,.2f} | "
            f"PnL: ${unrealized_pnl:,.2f} | "
            f"SL: ${strategy._stop_loss:,.2f} | TP: ${strategy._take_profit:,.2f}"
        )

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
