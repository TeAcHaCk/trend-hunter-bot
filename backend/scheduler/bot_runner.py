"""
Bot Runner — APScheduler-based execution loop for the Trend Hunter strategy.
"""
import logging
import asyncio
import time
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
          1. Refresh candle data → compute breakout levels
          2. If no orders placed → place limit BUY at high + SELL at low
          3. If orders placed → check if filled or expired (30 min)
          4. If filled → cancel other order → place bracket SL/TP
          5. If expired → cancel orders → recalculate levels → repeat
        """
        if self.state != "RUNNING":
            return

        self.last_check_time = datetime.utcnow().isoformat()

        for symbol, strategy in self.strategies.items():
            if not strategy.enabled:
                continue

            try:
                # ── IN TRADE → monitor position (needs price first) ──
                if strategy.is_in_trade():
                    price_data = delta_ws.get_price(symbol)
                    if price_data:
                        current_price = float(price_data.get("close") or price_data.get("mark_price", 0))
                    elif symbol in self._cached_candles and self._cached_candles[symbol]:
                        current_price = float(self._cached_candles[symbol][0]["close"])
                    else:
                        current_price = 0
                    if current_price:
                        strategy.last_price = current_price
                        await self._monitor_position(symbol, strategy, current_price)
                    continue

                # ── ORDERS PLACED → check fill / expiry ──
                if strategy._orders_placed:
                    price_data = delta_ws.get_price(symbol)
                    if price_data:
                        current_price = float(price_data.get("close") or price_data.get("mark_price", 0))
                    elif symbol in self._cached_candles and self._cached_candles[symbol]:
                        current_price = float(self._cached_candles[symbol][0]["close"])
                    else:
                        current_price = 0
                    if current_price:
                        strategy.last_price = current_price
                    await self._check_order_status(symbol, strategy, current_price or 0)
                    continue

                # ── NO ORDERS → refresh levels + place new orders ──
                # Safety checks
                if not self.position_manager._check_daily_loss():
                    continue

                # ALWAYS refresh candle levels first (this also provides price)
                await self._refresh_candles_if_needed(symbol, strategy)

                # Get current price (from WebSocket or cached candles)
                price_data = delta_ws.get_price(symbol)
                if price_data:
                    current_price = float(price_data.get("close") or price_data.get("mark_price", 0))
                elif symbol in self._cached_candles and self._cached_candles[symbol]:
                    current_price = float(self._cached_candles[symbol][0]["close"])
                else:
                    current_price = 0

                if not current_price:
                    logger.warning(f"[{symbol}] No price available after candle refresh")
                    continue

                strategy.last_price = current_price

                if not strategy._levels_set:
                    logger.warning(f"[{symbol}] Levels not set yet")
                    continue

                # Place limit orders at breakout levels
                await self._place_breakout_orders(symbol, strategy, current_price)

            except Exception as e:
                logger.error(f"[{symbol}] Strategy check error: {e}", exc_info=True)

    async def _refresh_candles_if_needed(self, symbol: str, strategy: TrendHunterStrategy):
        """Fetch new candles when a new candle has formed."""
        import time as _time

        resolution_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "1d": 86400,
        }
        interval = resolution_seconds.get(self.candle_resolution, 900)
        now = int(_time.time())
        last_fetch_time = self._last_candle_time.get(symbol, 0)

        # Only refresh if enough time has passed for a new candle
        if last_fetch_time > 0 and (now - last_fetch_time) < interval:
            return

        candles = self.delta_client.get_candles(
            symbol=symbol,
            resolution=self.candle_resolution,
            num_candles=self.lookback_candles,
        )

        if not candles or len(candles) < 2:
            return

        newest_time = int(candles[0].get("time", 0))

        if newest_time != last_fetch_time:
            self._cached_candles[symbol] = candles
            self._last_candle_time[symbol] = newest_time
            strategy.update_levels_from_candles(candles)
            logger.info(
                f"[{symbol}] LEVELS REFRESHED | New candle at {newest_time} | "
                f"Locked for next {self.candle_resolution}"
            )
        else:
            self._last_candle_time[symbol] = now

    async def _place_breakout_orders(self, symbol: str,
                                     strategy: TrendHunterStrategy,
                                     current_price: float):
        """Place limit BUY at breakout_high and limit SELL at breakout_low."""
        product_id = self.position_manager.get_product_id(symbol)
        if not product_id:
            logger.error(f"[{symbol}] Cannot resolve product ID")
            return

        high = strategy._breakout_high
        low = strategy._breakout_low

        if not high or not low:
            return

        logger.info(
            f"[{symbol}] PLACING LIMIT ORDERS | "
            f"BUY @ ${high:,.2f} | SELL @ ${low:,.2f} | "
            f"Current: ${current_price:,.2f}"
        )

        # Place limit BUY at breakout high
        long_result = self.delta_client.place_order(
            product_id=product_id,
            product_symbol=symbol,
            side="buy",
            size=strategy.quantity,
            order_type="limit_order",
            limit_price=str(round(high, 2)),
        )

        long_order_id = None
        if long_result.get("result"):
            long_order_id = long_result["result"].get("id")
            logger.info(f"[{symbol}] Limit BUY placed #{long_order_id} @ ${high:,.2f}")
        else:
            logger.error(f"[{symbol}] Limit BUY FAILED: {long_result}")

        # Place limit SELL at breakout low
        short_result = self.delta_client.place_order(
            product_id=product_id,
            product_symbol=symbol,
            side="sell",
            size=strategy.quantity,
            order_type="limit_order",
            limit_price=str(round(low, 2)),
        )

        short_order_id = None
        if short_result.get("result"):
            short_order_id = short_result["result"].get("id")
            logger.info(f"[{symbol}] Limit SELL placed #{short_order_id} @ ${low:,.2f}")
        else:
            logger.error(f"[{symbol}] Limit SELL FAILED: {short_result}")

        # Track the orders
        if long_order_id or short_order_id:
            strategy.record_orders_placed(long_order_id, short_order_id)
            self.total_signals += 1
        else:
            logger.error(f"[{symbol}] Both limit orders failed — will retry next cycle")

    async def _check_order_status(self, symbol: str,
                                  strategy: TrendHunterStrategy,
                                  current_price: float):
        """Check if limit orders have been filled or need cancellation."""
        product_id = self.position_manager.get_product_id(symbol)
        if not product_id:
            return

        # Check for expiry first (30 min)
        if strategy.are_orders_expired():
            logger.info(
                f"[{symbol}] Orders EXPIRED after 30min — cancelling and recalculating"
            )
            await self._cancel_pending_orders(symbol, strategy, product_id)
            # Force level refresh on next cycle
            self._last_candle_time[symbol] = 0
            return

        # Check actual position on exchange to detect fill
        pos_data = self.delta_client.get_position(product_id)
        position_result = pos_data.get("result", {})
        actual_size = int(position_result.get("size", 0)) if position_result else 0

        if actual_size != 0:
            # A limit order was filled! Determine direction
            entry_price_str = position_result.get("entry_price", "0")
            entry_price = float(entry_price_str) if entry_price_str else current_price

            if actual_size > 0:
                direction = "LONG"
                filled_order_id = strategy._long_order_id
                other_order_id = strategy._short_order_id
            else:
                direction = "SHORT"
                filled_order_id = strategy._short_order_id
                other_order_id = strategy._long_order_id

            logger.info(
                f"[{symbol}] ✅ LIMIT ORDER FILLED! {direction} @ ${entry_price:,.2f} | "
                f"Order #{filled_order_id}"
            )

            # Cancel the other pending limit order
            if other_order_id:
                try:
                    self.delta_client.cancel_order(other_order_id, product_id)
                    logger.info(f"[{symbol}] Cancelled opposite order #{other_order_id}")
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel opposite: {e}")

            # Calculate SL/TP at 1:1 RR
            levels = strategy.calculate_sl_tp(entry_price, direction)
            sl_price = levels["stop_loss"]
            tp_price = levels["take_profit"]

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
            strategy.enter_trade(direction, entry_price, sl_price, tp_price)
            self.position_manager._positions[symbol] = {
                "side": direction,
                "size": abs(actual_size),
                "entry_price": entry_price,
                "product_id": product_id,
            }
            self.position_manager._last_trade_time[symbol] = datetime.utcnow()
            self.total_trades += 1

            # Log to database
            await self._log_trade({
                "symbol": symbol,
                "signal": direction,
                "quantity": abs(actual_size),
                "entry_price": entry_price,
                "stop_loss": sl_price,
                "take_profit": tp_price,
                "risk": levels["risk"],
                "timestamp": datetime.utcnow().isoformat(),
                "actions": [f"Limit {direction}", f"SL: {sl_price}", f"TP: {tp_price}"],
                "success": True,
            })
            return

        # No fill yet — log waiting status
        elapsed = int(time.time() - strategy._orders_placed_time)
        remaining = max(0, strategy._order_expiry_seconds - elapsed)
        dist_high = strategy._breakout_high - current_price if strategy._breakout_high else 0
        dist_low = current_price - strategy._breakout_low if strategy._breakout_low else 0
        logger.info(
            f"[{symbol}] ${current_price:,.2f} | "
            f"ORDERS PENDING ({remaining}s left) | "
            f"BUY @ ${strategy._breakout_high:,.2f} (${dist_high:,.2f}) | "
            f"SELL @ ${strategy._breakout_low:,.2f} (${dist_low:,.2f})"
        )

    async def _cancel_pending_orders(self, symbol: str,
                                     strategy: TrendHunterStrategy,
                                     product_id: int):
        """Cancel all pending limit orders for a symbol."""
        cancelled = 0
        for order_id in [strategy._long_order_id, strategy._short_order_id]:
            if order_id:
                try:
                    self.delta_client.cancel_order(order_id, product_id)
                    cancelled += 1
                    logger.info(f"[{symbol}] Cancelled order #{order_id}")
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel #{order_id}: {e}")

        if cancelled == 0:
            # Fallback: cancel all orders for this product
            try:
                self.delta_client.cancel_all_orders(product_id)
                logger.info(f"[{symbol}] Cancelled all orders for product {product_id}")
            except Exception as e:
                logger.warning(f"[{symbol}] cancel_all failed: {e}")

        strategy.clear_orders()

    async def _monitor_position(self, symbol: str, strategy: TrendHunterStrategy,
                                current_price: float):
        """Monitor an open position — check if SL/TP has been hit."""
        product_id = self.position_manager.get_product_id(symbol)
        if not product_id:
            return

        pos_data = self.delta_client.get_position(product_id)
        position_result = pos_data.get("result", {})
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

            # Force level refresh for next trade
            self._last_candle_time[symbol] = 0
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
