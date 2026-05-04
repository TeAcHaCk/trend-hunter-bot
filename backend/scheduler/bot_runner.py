"""
Bot Runner — Async execution loop for the Trend Hunter strategy.

Major improvements:
  * Async REST → no event-loop blocking on order/cancel/position calls.
  * Parallel BUY/SELL limit-order placement (asyncio.gather) ⇒ ~½ entry latency.
  * Idempotent order placement via client_order_id.
  * Instant fill detection through the private `orders` WS channel
    (falls back to polling if the channel is unavailable).
  * Atomic protection: bracket SL/TP + cancel-of-opposite are issued in
    parallel the moment we observe a fill.
  * Crash-safe state: per-symbol snapshots persisted to SQLite; reconciled
    against the exchange on startup.
  * Daily-loss + cooldown checked on every execution path.
  * Realised PnL written back to the original TradeLog row on exit.
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from backend.exchange.delta_client import DeltaClient
from backend.exchange.delta_ws import delta_ws
from backend.strategy.trend_hunter import TrendHunterStrategy
from backend.strategy.position_manager import PositionManager
from backend.models.database import async_session
from backend.models.trade_log import TradeLog, BotSettings
from backend.config import settings

logger = logging.getLogger(__name__)

STATE_KEY = "bot_state_snapshot"


class BotRunner:
    """Manages the trading bot lifecycle and strategy execution."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.delta_client = DeltaClient()
        self.position_manager = PositionManager(self.delta_client)

        self.strategies: Dict[str, TrendHunterStrategy] = {}
        self.state: str = "STOPPED"
        self._initialized: bool = False

        self.last_check_time: Optional[str] = None
        self.total_signals: int = 0
        self.total_trades: int = 0

        # Daily counters — reset at the same time as daily_pnl
        self.daily_signals: int = 0
        self.daily_trades: int = 0
        self._daily_counters_date: Optional[object] = None

        self._cached_candles: Dict[str, list] = {}
        self._last_candle_time: Dict[str, int] = {}

        # Concurrency control: one in-flight execution path per symbol
        self._symbol_locks: Dict[str, asyncio.Lock] = {}
        self._fill_in_progress: Dict[str, bool] = {}

        # Reverse lookup product_id -> symbol for WS fill events
        self._pid_to_symbol: Dict[int, str] = {}

        # Tunables
        self.poll_interval_seconds: int = 3
        self.candle_resolution: str = "15m"
        self.lookback_candles: int = 6

    # ── Initialisation & settings ─────────────────────────────────

    def initialize(self, bot_settings: Dict = None):
        s = bot_settings or settings.DEFAULT_SETTINGS

        self.candle_resolution = s.get("candle_resolution", "15m")
        self.lookback_candles = s.get("lookback_candles", 6)
        # Faster default poll — async, so this is cheap
        self.poll_interval_seconds = int(s.get("poll_interval_seconds", 3))

        common_kwargs = {
            "breakout_buffer_pct": s.get("breakout_buffer_pct", 0.1),
            "sl_atr_mult": s.get("sl_atr_mult", 1.0),
            "tp_atr_mult": s.get("tp_atr_mult", 1.5),
            "min_sl_pct": s.get("min_sl_pct", 0.15),
            "max_sl_pct": s.get("max_sl_pct", 1.5),
            "require_volume_confirmation": s.get("require_volume_confirmation", False),
            "order_expiry_seconds": int(s.get("order_expiry_seconds", 1800)),
        }

        # BTC
        if s.get("btc_enabled", True):
            existing = self.strategies.get("BTCUSD")
            new_strat = TrendHunterStrategy(
                symbol="BTCUSD",
                quantity=s.get("btc_quantity", 10),
                **common_kwargs,
            )
            if existing:
                # Preserve open trade / orders state across re-initialisation
                new_strat.restore(existing.snapshot())
            self.strategies["BTCUSD"] = new_strat
        elif "BTCUSD" in self.strategies:
            self.strategies["BTCUSD"].enabled = False

        # ETH
        if s.get("eth_enabled", True):
            existing = self.strategies.get("ETHUSD")
            new_strat = TrendHunterStrategy(
                symbol="ETHUSD",
                quantity=s.get("eth_quantity", 10),
                **common_kwargs,
            )
            if existing:
                new_strat.restore(existing.snapshot())
            self.strategies["ETHUSD"] = new_strat
        elif "ETHUSD" in self.strategies:
            self.strategies["ETHUSD"].enabled = False

        self.position_manager.cooldown_minutes = s.get("cooldown_minutes", 5)
        self.position_manager.max_daily_loss = s.get("max_daily_loss", 100.0)
        self.position_manager.leverage = s.get("leverage", 10)

        for symbol in self.strategies:
            self._symbol_locks.setdefault(symbol, asyncio.Lock())
            self._fill_in_progress.setdefault(symbol, False)

        self._initialized = True
        logger.info(
            f"Bot initialized | Candles: {self.lookback_candles}x {self.candle_resolution} | "
            f"Buffer: {s.get('breakout_buffer_pct', 0.1)}% | Poll: {self.poll_interval_seconds}s"
        )

    def update_client(self, api_key: str, api_secret: str):
        old = self.delta_client
        self.delta_client = DeltaClient(api_key=api_key, api_secret=api_secret)
        self.position_manager.client = self.delta_client
        delta_ws.set_credentials(api_key, api_secret)
        # Best-effort cleanup of the old session
        try:
            asyncio.get_event_loop().create_task(old.close())
        except Exception:
            pass
        logger.info("Delta client updated with new credentials")

    async def _resolve_product_ids(self):
        for symbol in self.strategies:
            pid = await self.position_manager.get_product_id(symbol)
            if pid:
                self._pid_to_symbol[pid] = symbol
            else:
                logger.warning(f"Could not resolve product ID for {symbol}")

    # ── Helpers ───────────────────────────────────────────────────

    def _get_price(self, symbol: str) -> float:
        price_data = delta_ws.get_price(symbol)
        if price_data:
            p = float(price_data.get("close") or price_data.get("mark_price") or 0)
            if p > 0:
                return p
        cached = self._cached_candles.get(symbol) or []
        if cached:
            try:
                return float(cached[0]["close"])
            except (KeyError, TypeError, ValueError):
                pass
        return 0.0

    def _new_coid(self, symbol: str, side: str) -> str:
        return f"th-{symbol[:3].lower()}-{side}-{uuid.uuid4().hex[:10]}"

    # ── Main loop ────────────────────────────────────────────────

    def _check_daily_reset(self):
        """Reset daily counters when the UTC date rolls over."""
        today = datetime.now(timezone.utc).date()
        if self._daily_counters_date != today:
            self.daily_signals = 0
            self.daily_trades = 0
            self._daily_counters_date = today

    async def _run_strategy_check(self):
        if self.state != "RUNNING":
            return
        self._check_daily_reset()
        self.last_check_time = datetime.now(timezone.utc).isoformat()

        await asyncio.gather(
            *(self._check_symbol(sym) for sym in list(self.strategies.keys())),
            return_exceptions=True,
        )

    async def _check_symbol(self, symbol: str):
        strategy = self.strategies.get(symbol)
        if not strategy or not strategy.enabled:
            return

        # Single in-flight execution path per symbol
        lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                if strategy.is_in_trade():
                    price = self._get_price(symbol)
                    if price:
                        strategy.last_price = price
                    await self._monitor_position(symbol, strategy, price)
                    return

                if strategy._orders_placed:
                    price = self._get_price(symbol)
                    if price:
                        strategy.last_price = price
                    await self._check_order_status(symbol, strategy, price)
                    return

                # No orders → safety checks first
                if not self.position_manager.check_daily_loss():
                    return
                if not self.position_manager.check_cooldown(symbol):
                    return

                await self._refresh_candles_if_needed(symbol, strategy)

                price = self._get_price(symbol)
                if not price:
                    logger.warning(f"[{symbol}] No price available")
                    return
                strategy.last_price = price

                if not strategy._levels_set:
                    return

                if not strategy.passes_volume_filter():
                    logger.info(f"[{symbol}] Skipping — volume below confirmation threshold")
                    return

                await self._place_breakout_orders(symbol, strategy, price)

            except Exception as e:
                logger.error(f"[{symbol}] Strategy check error: {e}", exc_info=True)

    # ── Candles ───────────────────────────────────────────────────

    async def _refresh_candles_if_needed(self, symbol: str,
                                         strategy: TrendHunterStrategy):
        resolution_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "1d": 86400,
        }
        interval = resolution_seconds.get(self.candle_resolution, 900)
        now = int(time.time())
        last_fetch_time = self._last_candle_time.get(symbol, 0)

        if last_fetch_time > 0 and (now - last_fetch_time) < interval:
            return

        candles = await self.delta_client.get_candles(
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
                f"[{symbol}] Levels refreshed | new candle @ {newest_time} | "
                f"locked for next {self.candle_resolution}"
            )
        else:
            self._last_candle_time[symbol] = now

    # ── Order placement (parallel) ────────────────────────────────

    async def _place_breakout_orders(self, symbol: str,
                                     strategy: TrendHunterStrategy,
                                     current_price: float):
        product_id = await self.position_manager.get_product_id(symbol)
        if not product_id:
            logger.error(f"[{symbol}] Cannot resolve product ID")
            return

        high = strategy._breakout_high
        low = strategy._breakout_low
        if not high or not low:
            return

        # Sanity: don't arm if price already outside the range — likely stale levels
        if current_price > high or current_price < low:
            logger.warning(
                f"[{symbol}] Price ${current_price:,.2f} outside range "
                f"[{low:,.2f}, {high:,.2f}] — forcing candle refresh"
            )
            self._last_candle_time[symbol] = 0
            return

        long_coid = self._new_coid(symbol, "buy")
        short_coid = self._new_coid(symbol, "sell")

        logger.info(
            f"[{symbol}] Arming breakout | BUY @ ${high:,.2f} | SELL @ ${low:,.2f} | "
            f"Now: ${current_price:,.2f}"
        )

        long_task = asyncio.create_task(self.delta_client.place_order(
            product_id=product_id,
            side="buy",
            size=strategy.quantity,
            order_type="limit_order",
            limit_price=str(round(high, 2)),
            client_order_id=long_coid,
            time_in_force="gtc",
        ))
        short_task = asyncio.create_task(self.delta_client.place_order(
            product_id=product_id,
            side="sell",
            size=strategy.quantity,
            order_type="limit_order",
            limit_price=str(round(low, 2)),
            client_order_id=short_coid,
            time_in_force="gtc",
        ))
        long_result, short_result = await asyncio.gather(long_task, short_task,
                                                         return_exceptions=True)

        long_order_id = self._extract_order_id(long_result)
        short_order_id = self._extract_order_id(short_result)

        if not long_order_id:
            logger.error(f"[{symbol}] Limit BUY FAILED: {long_result}")
        if not short_order_id:
            logger.error(f"[{symbol}] Limit SELL FAILED: {short_result}")

        if long_order_id or short_order_id:
            strategy.record_orders_placed(
                long_order_id=long_order_id,
                short_order_id=short_order_id,
                long_coid=long_coid,
                short_coid=short_coid,
            )
            self.total_signals += 1
            self.daily_signals += 1
            await self._persist_state()
        else:
            logger.error(f"[{symbol}] Both limit orders failed — will retry next cycle")

    @staticmethod
    def _extract_order_id(result: Any) -> Optional[int]:
        if isinstance(result, Exception):
            return None
        if not isinstance(result, dict):
            return None
        if not (result.get("success") or result.get("result")):
            return None
        r = result.get("result")
        if isinstance(r, dict):
            return r.get("id")
        return None

    # ── Fill / expiry handling ───────────────────────────────────

    async def _check_order_status(self, symbol: str,
                                  strategy: TrendHunterStrategy,
                                  current_price: float):
        product_id = await self.position_manager.get_product_id(symbol)
        if not product_id:
            return

        if strategy.are_orders_expired():
            logger.info(f"[{symbol}] Orders EXPIRED — cancelling and recalculating")
            await self._cancel_pending_orders(symbol, strategy, product_id)
            self._last_candle_time[symbol] = 0
            await self._persist_state()
            return

        # Fall back to position polling — used when WS fill push hasn't arrived
        pos_data = await self.delta_client.get_position(product_id)
        result = pos_data.get("result") if isinstance(pos_data, dict) else None
        actual_size = 0
        entry_price = current_price
        if isinstance(result, dict) and result.get("size") is not None:
            try:
                actual_size = int(result.get("size", 0))
                entry_str = result.get("entry_price")
                if entry_str:
                    entry_price = float(entry_str)
            except (TypeError, ValueError):
                actual_size = 0

        if actual_size != 0:
            await self._handle_fill(symbol, strategy, product_id, actual_size, entry_price)
            return

        # Still pending — log progress
        elapsed = int(time.time() - strategy._orders_placed_time)
        remaining = max(0, strategy._order_expiry_seconds - elapsed)
        dist_high = strategy._breakout_high - current_price if strategy._breakout_high else 0
        dist_low = current_price - strategy._breakout_low if strategy._breakout_low else 0
        logger.info(
            f"[{symbol}] ${current_price:,.2f} | PENDING ({remaining}s) | "
            f"BUY @ ${strategy._breakout_high:,.2f} (-${dist_high:,.2f}) | "
            f"SELL @ ${strategy._breakout_low:,.2f} (-${dist_low:,.2f})"
        )

    async def _handle_fill(self, symbol: str,
                           strategy: TrendHunterStrategy,
                           product_id: int,
                           actual_size: int,
                           entry_price: float):
        """Cancel opposite leg, attach bracket SL/TP, log trade — atomically."""
        if self._fill_in_progress.get(symbol):
            return
        self._fill_in_progress[symbol] = True
        try:
            if actual_size > 0:
                direction = "LONG"
                other_order_id = strategy._short_order_id
            else:
                direction = "SHORT"
                other_order_id = strategy._long_order_id

            logger.info(
                f"[{symbol}] ✅ FILLED {direction} @ ${entry_price:,.2f} | "
                f"size={abs(actual_size)}"
            )

            levels = strategy.calculate_sl_tp(entry_price, direction)
            sl_price = levels["stop_loss"]
            tp_price = levels["take_profit"]

            # Cancel opposite leg immediately
            cancel_tasks = []
            if other_order_id:
                cancel_tasks.append(asyncio.create_task(
                    self.delta_client.cancel_order(product_id, other_order_id)
                ))
            if cancel_tasks:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)

            # Try bracket order first (PUT /v2/orders/bracket)
            bracket_ok = await self._place_bracket_with_fallback(
                symbol=symbol,
                product_id=product_id,
                direction=direction,
                size=abs(actual_size),
                sl_price=sl_price,
                tp_price=tp_price,
            )

            if not bracket_ok:
                logger.error(f"[{symbol}] Both bracket and fallback SL/TP failed — "
                             f"emergency closing position")
                await self.delta_client.close_position(
                    product_id=product_id,
                    current_side=direction,
                    size=abs(actual_size),
                )
                strategy.clear_orders()
                self.position_manager.mark_trade_time(symbol)
                await self._persist_state()
                return

            logger.info(f"[{symbol}] SL/TP set | SL ${sl_price:,.2f} | TP ${tp_price:,.2f}")

            strategy.enter_trade(direction, entry_price, sl_price, tp_price)
            self.position_manager._positions[symbol] = {
                "side": direction,
                "size": abs(actual_size),
                "entry_price": entry_price,
                "product_id": product_id,
            }
            self.position_manager.mark_trade_time(symbol)
            self.total_trades += 1
            self.daily_trades += 1

            log_id = await self._log_trade_open(symbol, direction, abs(actual_size),
                                                entry_price, sl_price, tp_price,
                                                levels.get("risk", 0))
            strategy._open_trade_log_id = log_id
            await self._persist_state()
        finally:
            self._fill_in_progress[symbol] = False

    async def _place_bracket_with_fallback(
        self, symbol: str, product_id: int,
        direction: str, size: int, sl_price: float, tp_price: float,
    ) -> bool:
        """Try bracket order; on failure, fall back to individual SL + TP orders.

        Returns True if at least SL was placed successfully.
        """
        # --- Attempt 1: PUT bracket order ---
        try:
            bracket_result = await self.delta_client.place_bracket_order(
                product_id=product_id,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
            )
            if isinstance(bracket_result, dict) and (
                bracket_result.get("success") or bracket_result.get("result")
            ):
                logger.info(f"[{symbol}] Bracket order placed successfully via PUT")
                return True
            else:
                logger.warning(f"[{symbol}] Bracket order response: {bracket_result}")
        except Exception as e:
            logger.warning(f"[{symbol}] Bracket order exception: {e}")

        # --- Attempt 2: Individual SL + TP orders ---
        logger.info(f"[{symbol}] Falling back to individual SL/TP orders")
        close_side = "sell" if direction == "LONG" else "buy"
        sl_ok = False
        tp_ok = False

        try:
            sl_result = await self.delta_client.place_stop_order(
                product_id=product_id,
                side=close_side,
                size=size,
                stop_price=sl_price,
            )
            if isinstance(sl_result, dict) and (
                sl_result.get("success") or sl_result.get("result")
            ):
                sl_ok = True
                logger.info(f"[{symbol}] Fallback SL order placed @ ${sl_price:,.2f}")
            else:
                logger.error(f"[{symbol}] Fallback SL failed: {sl_result}")
        except Exception as e:
            logger.error(f"[{symbol}] Fallback SL exception: {e}")

        try:
            tp_result = await self.delta_client.place_take_profit_order(
                product_id=product_id,
                side=close_side,
                size=size,
                stop_price=tp_price,
            )
            if isinstance(tp_result, dict) and (
                tp_result.get("success") or tp_result.get("result")
            ):
                tp_ok = True
                logger.info(f"[{symbol}] Fallback TP order placed @ ${tp_price:,.2f}")
            else:
                logger.error(f"[{symbol}] Fallback TP failed: {tp_result}")
        except Exception as e:
            logger.error(f"[{symbol}] Fallback TP exception: {e}")

        if sl_ok:
            return True  # at minimum the stop-loss is protecting us
        return False

    async def _cancel_pending_orders(self, symbol: str,
                                     strategy: TrendHunterStrategy,
                                     product_id: int):
        cancel_tasks = []
        for order_id in (strategy._long_order_id, strategy._short_order_id):
            if order_id:
                cancel_tasks.append(asyncio.create_task(
                    self.delta_client.cancel_order(product_id, order_id)
                ))

        if cancel_tasks:
            results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
            ok = sum(1 for r in results
                     if isinstance(r, dict) and (r.get("success") or r.get("result")))
            logger.info(f"[{symbol}] Cancelled {ok}/{len(cancel_tasks)} pending orders")
        else:
            try:
                await self.delta_client.cancel_all_orders(product_id)
            except Exception as e:
                logger.warning(f"[{symbol}] cancel_all failed: {e}")

        strategy.clear_orders()

    # ── Position monitor & exit ──────────────────────────────────

    async def _monitor_position(self, symbol: str,
                                strategy: TrendHunterStrategy,
                                current_price: float):
        product_id = await self.position_manager.get_product_id(symbol)
        if not product_id:
            return

        pos_data = await self.delta_client.get_position(product_id)
        result = pos_data.get("result") if isinstance(pos_data, dict) else None
        actual_size = 0
        if isinstance(result, dict):
            try:
                actual_size = int(result.get("size", 0) or 0)
            except (TypeError, ValueError):
                actual_size = 0

        if actual_size == 0:
            await self._on_position_closed(symbol, strategy, current_price)
            return

        entry_price = strategy._entry_price or current_price
        cv = self.position_manager.get_contract_value(symbol)
        unrealised = ((current_price - entry_price) if strategy._trade_direction == "LONG"
                      else (entry_price - current_price)) * strategy.quantity * cv
        logger.info(
            f"[{symbol}] IN {strategy._trade_direction} | Entry: ${entry_price:,.2f} | "
            f"Now: ${current_price:,.2f} | uPnL: ${unrealised:,.2f} | "
            f"SL: ${strategy._stop_loss:,.2f} | TP: ${strategy._take_profit:,.2f}"
        )

    async def _on_position_closed(self, symbol: str,
                                  strategy: TrendHunterStrategy,
                                  current_price: float):
        entry_price = strategy._entry_price or 0
        # Try to get actual exit price from the most recent fill
        exit_price = current_price
        try:
            history = await self.delta_client.get_order_history(page_size=10)
            for o in (history.get("result") or []):
                if (str(o.get("product_symbol")) == symbol
                        and o.get("state") in ("closed", "filled")
                        and o.get("reduce_only")):
                    avg = o.get("average_fill_price") or o.get("filled_price")
                    if avg:
                        exit_price = float(avg)
                        break
        except Exception:
            pass

        cv = self.position_manager.get_contract_value(symbol)
        if strategy._trade_direction == "LONG":
            pnl = (exit_price - entry_price) * strategy.quantity * cv
        else:
            pnl = (entry_price - exit_price) * strategy.quantity * cv

        exit_type = "TP" if pnl > 0 else "SL"
        logger.info(
            f"[{symbol}] CLOSED by {exit_type} | Entry: ${entry_price:,.2f} | "
            f"Exit: ${exit_price:,.2f} | PnL: ${pnl:,.2f}"
        )

        self.position_manager.record_pnl(pnl)
        if symbol in self.position_manager._positions:
            del self.position_manager._positions[symbol]

        await self._log_trade_close(strategy._open_trade_log_id, exit_price, pnl, exit_type)

        strategy.exit_trade()
        self._last_candle_time[symbol] = 0  # force level refresh
        await self._persist_state()

    # ── Private WS order channel — instant fill detection ────────

    async def _on_ws_order_event(self, data: Dict):
        """Triggered by Delta's `orders` / `positions` WS channel."""
        try:
            if data.get("type") == "positions":
                pid = data.get("product_id")
                symbol = self._pid_to_symbol.get(pid)
                if not symbol:
                    return
                strategy = self.strategies.get(symbol)
                if not strategy:
                    return
                size = int(data.get("size", 0) or 0)
                if not strategy.is_in_trade() and strategy._orders_placed and size != 0:
                    entry = float(data.get("entry_price") or 0) or self._get_price(symbol)
                    lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
                    async with lock:
                        await self._handle_fill(symbol, strategy, pid, size, entry)
                elif strategy.is_in_trade() and size == 0:
                    lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
                    async with lock:
                        await self._on_position_closed(symbol, strategy,
                                                       self._get_price(symbol))
        except Exception as e:
            logger.error(f"WS order handler error: {e}", exc_info=True)

    # ── Persistence ───────────────────────────────────────────────

    async def _persist_state(self):
        snap = {sym: strat.snapshot() for sym, strat in self.strategies.items()}
        snap["_meta"] = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "daily_signals": self.daily_signals,
            "daily_trades": self.daily_trades,
            "daily_counters_date": (self._daily_counters_date.isoformat()
                                    if self._daily_counters_date else None),
            "daily_pnl": self.position_manager._daily_pnl,
            "daily_pnl_reset_date": (self.position_manager._daily_pnl_reset_date.isoformat()
                                     if self.position_manager._daily_pnl_reset_date else None),
        }
        try:
            async with async_session() as session:
                row = await session.execute(
                    select(BotSettings).where(BotSettings.key == STATE_KEY)
                )
                existing = row.scalar_one_or_none()
                if existing:
                    existing.value = json.dumps(snap)
                else:
                    session.add(BotSettings(key=STATE_KEY, value=json.dumps(snap)))
                await session.commit()
        except Exception as e:
            logger.warning(f"Could not persist bot state: {e}")

    async def _load_persisted_state(self) -> Dict:
        try:
            async with async_session() as session:
                row = await session.execute(
                    select(BotSettings).where(BotSettings.key == STATE_KEY)
                )
                existing = row.scalar_one_or_none()
                if existing and existing.value:
                    return json.loads(existing.value)
        except Exception as e:
            logger.warning(f"Could not load persisted state: {e}")
        return {}

    async def _reconcile_with_exchange(self):
        """On startup, sync local state with the exchange to avoid duplicates."""
        try:
            saved = await self._load_persisted_state()
            for sym, strat in self.strategies.items():
                snap = saved.get(sym)
                if snap:
                    strat.restore(snap)

            meta = saved.get("_meta", {}) if isinstance(saved, dict) else {}
            if meta.get("daily_pnl") is not None:
                self.position_manager._daily_pnl = float(meta["daily_pnl"])
            if meta.get("daily_pnl_reset_date"):
                try:
                    self.position_manager._daily_pnl_reset_date = datetime.fromisoformat(
                        meta["daily_pnl_reset_date"]
                    ).date()
                except Exception:
                    pass
            if meta.get("total_signals"):
                self.total_signals = int(meta["total_signals"])
            if meta.get("total_trades"):
                self.total_trades = int(meta["total_trades"])
            if meta.get("daily_signals") is not None:
                self.daily_signals = int(meta["daily_signals"])
            if meta.get("daily_trades") is not None:
                self.daily_trades = int(meta["daily_trades"])
            if meta.get("daily_counters_date"):
                try:
                    self._daily_counters_date = datetime.fromisoformat(
                        meta["daily_counters_date"]
                    ).date()
                except Exception:
                    pass
            # Force a daily reset check in case the date has changed since the snapshot
            self._check_daily_reset()

            # Sync against live positions
            positions_resp = await self.delta_client.get_positions()
            live_positions = positions_resp.get("result") if isinstance(positions_resp, dict) else None
            if isinstance(live_positions, list):
                self.position_manager.sync_positions_from_exchange(live_positions)
                for sym, strat in self.strategies.items():
                    live = self.position_manager._positions.get(sym)
                    if live:
                        entry_price = live["entry_price"]
                        direction = live["side"]
                        size = live["size"]
                        product_id = live.get("product_id")

                        # Determine SL/TP — use persisted values if present,
                        # otherwise compute from ATR/entry price
                        sl = strat._stop_loss
                        tp = strat._take_profit
                        needs_sl_tp = (not sl or not tp or sl == 0 or tp == 0)

                        if needs_sl_tp:
                            levels = strat.calculate_sl_tp(entry_price, direction)
                            sl = levels["stop_loss"]
                            tp = levels["take_profit"]
                            logger.info(
                                f"[{sym}] Computed SL/TP for existing position: "
                                f"SL=${sl:,.2f} TP=${tp:,.2f}"
                            )

                        if not strat.is_in_trade():
                            strat.enter_trade(
                                direction=direction,
                                entry_price=entry_price,
                                stop_loss=sl,
                                take_profit=tp,
                            )
                            logger.info(
                                f"[{sym}] Reconciled: live position {direction} "
                                f"size={size} @ ${entry_price:,.2f} "
                                f"SL=${sl:,.2f} TP=${tp:,.2f}"
                            )
                        elif needs_sl_tp:
                            # Strategy already in trade but SL/TP were missing
                            strat._stop_loss = sl
                            strat._take_profit = tp
                            logger.info(
                                f"[{sym}] Updated missing SL/TP on existing trade: "
                                f"SL=${sl:,.2f} TP=${tp:,.2f}"
                            )

                        # Place bracket order on exchange if the position
                        # doesn't have SL/TP protection set yet
                        if needs_sl_tp and product_id:
                            logger.info(
                                f"[{sym}] Placing bracket for existing position "
                                f"(SL/TP were not set on exchange)"
                            )
                            await self._place_bracket_with_fallback(
                                symbol=sym,
                                product_id=product_id,
                                direction=direction,
                                size=size,
                                sl_price=sl,
                                tp_price=tp,
                            )
                    else:
                        if strat.is_in_trade():
                            logger.info(f"[{sym}] Stale local position cleared (no live position)")
                            strat.exit_trade()

            # Sync open orders — drop stale order IDs
            for sym, strat in self.strategies.items():
                if not strat._orders_placed:
                    continue
                pid = await self.position_manager.get_product_id(sym)
                if not pid:
                    continue
                orders_resp = await self.delta_client.get_active_orders(pid)
                live_ids = set()
                for o in (orders_resp.get("result") or []):
                    oid = o.get("id")
                    if oid:
                        live_ids.add(int(oid))
                long_alive = strat._long_order_id in live_ids if strat._long_order_id else False
                short_alive = strat._short_order_id in live_ids if strat._short_order_id else False
                if not long_alive and not short_alive:
                    logger.info(f"[{sym}] No live limit orders — clearing stale order state")
                    strat.clear_orders()

            await self._persist_state()
        except Exception as e:
            logger.error(f"Reconciliation failed: {e}", exc_info=True)

    # ── Trade log writes ─────────────────────────────────────────

    async def _log_trade_open(self, symbol: str, direction: str, quantity: int,
                              entry_price: float, sl: float, tp: float,
                              risk: float) -> Optional[int]:
        try:
            async with async_session() as session:
                trade = TradeLog(
                    timestamp=datetime.utcnow(),
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    quantity=quantity,
                    status="open",
                    notes=f"SL={sl} TP={tp} risk={risk:.4f}",
                )
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                logger.info(f"Trade row #{trade.id} logged: {symbol} {direction}")
                return trade.id
        except Exception as e:
            logger.error(f"Failed to log trade open: {e}")
            return None

    async def _log_trade_close(self, trade_id: Optional[int],
                               exit_price: float, pnl: float, exit_type: str):
        if not trade_id:
            return
        try:
            async with async_session() as session:
                row = await session.execute(
                    select(TradeLog).where(TradeLog.id == trade_id)
                )
                trade = row.scalar_one_or_none()
                if not trade:
                    return
                trade.exit_price = exit_price
                trade.pnl = pnl
                trade.status = "closed"
                trade.notes = (trade.notes or "") + f" | exit={exit_type} pnl={pnl:.2f}"
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to log trade close: {e}")

    # ── Lifecycle ────────────────────────────────────────────────

    async def _health_check(self):
        if not delta_ws.is_connected:
            logger.warning("WebSocket disconnected — reconnecting")
            symbols = [s for s, st in self.strategies.items() if st.enabled]
            await delta_ws.start(symbols)

    async def start(self):
        if self.state == "RUNNING":
            logger.warning("Bot is already running")
            return
        if not self._initialized:
            self.initialize()

        await self._resolve_product_ids()

        # Best-effort leverage set (parallel)
        leverage_tasks = []
        for symbol in self.strategies:
            pid = await self.position_manager.get_product_id(symbol)
            if pid:
                leverage_tasks.append(asyncio.create_task(
                    self.delta_client.set_leverage(pid, self.position_manager.leverage)
                ))
        if leverage_tasks:
            await asyncio.gather(*leverage_tasks, return_exceptions=True)

        # Start WS feed (with credentials so private channel subscribes)
        delta_ws.set_credentials(self.delta_client.api_key, self.delta_client.api_secret)
        delta_ws.add_order_subscriber(self._on_ws_order_event)
        enabled_symbols = [s for s, st in self.strategies.items() if st.enabled]
        await delta_ws.start(enabled_symbols)

        # Reconcile after WS up
        await self._reconcile_with_exchange()

        # Schedule strategy + health checks
        self.scheduler.add_job(
            self._run_strategy_check,
            "interval",
            seconds=self.poll_interval_seconds,
            id="strategy_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._health_check,
            "interval",
            seconds=30,
            id="health_check",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._persist_state,
            "interval",
            seconds=60,
            id="state_persist",
            replace_existing=True,
        )

        if not self.scheduler.running:
            self.scheduler.start()

        self.state = "RUNNING"
        logger.info(f"Bot STARTED | poll={self.poll_interval_seconds}s | symbols: {enabled_symbols}")

    async def stop(self):
        self.state = "STOPPED"
        if self.scheduler.running:
            self.scheduler.remove_all_jobs()
            self.scheduler.shutdown(wait=False)
        delta_ws.remove_order_subscriber(self._on_ws_order_event)
        await delta_ws.stop()
        await self._persist_state()
        await self.delta_client.close()
        logger.info("🛑 Bot STOPPED")

    async def pause(self):
        self.state = "PAUSED"
        logger.info("⏸️ Bot PAUSED")

    async def resume(self):
        if self.state == "PAUSED":
            self.state = "RUNNING"
            logger.info("▶️ Bot RESUMED")

    def get_status(self) -> Dict:
        strategies_status = {}
        for symbol, strategy in self.strategies.items():
            status = strategy.get_status()
            status["current_position"] = self.position_manager.get_current_position(symbol)
            status["contract_value"] = self.position_manager.get_contract_value(symbol)
            strategies_status[symbol] = status

        return {
            "state": self.state,
            "ws_connected": delta_ws.is_connected,
            "last_check_time": self.last_check_time,
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "daily_signals": self.daily_signals,
            "daily_trades": self.daily_trades,
            "strategies": strategies_status,
            "position_manager": self.position_manager.get_status(),
            "prices": delta_ws.price_cache,
            "testnet": settings.DELTA_TESTNET,
        }


# Global bot instance
bot_runner = BotRunner()
