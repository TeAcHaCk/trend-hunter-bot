"""
Delta Exchange India WebSocket Client.

Public ticker feed for live price updates + (optional) authenticated `orders`
channel so order fills are pushed in real time instead of polled.
"""
import json
import time
import hmac
import hashlib
import asyncio
import logging
from typing import Dict, Optional, Callable, Set, List, Awaitable

import websockets

from backend.config import settings

logger = logging.getLogger(__name__)


class DeltaWebSocket:
    """WebSocket client for Delta Exchange real-time price + order feed."""

    def __init__(self):
        self._ws = None
        self._running = False
        self._connected = False
        self._price_cache: Dict[str, Dict] = {}
        self._price_subscribers: Set[Callable] = set()
        self._order_subscribers: List[Callable[[Dict], Awaitable[None]]] = []
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._task: Optional[asyncio.Task] = None
        self._symbols: List[str] = []
        self._auth_key: str = ""
        self._auth_secret: str = ""

    @property
    def ws_url(self) -> str:
        return settings.ws_url

    @property
    def price_cache(self) -> Dict[str, Dict]:
        return self._price_cache.copy()

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    def get_price(self, symbol: str) -> Optional[Dict]:
        return self._price_cache.get(symbol)

    # ── Subscribers ──
    def add_subscriber(self, callback: Callable):
        self._price_subscribers.add(callback)

    def remove_subscriber(self, callback: Callable):
        self._price_subscribers.discard(callback)

    def add_order_subscriber(self, callback: Callable[[Dict], Awaitable[None]]):
        if callback not in self._order_subscribers:
            self._order_subscribers.append(callback)

    def remove_order_subscriber(self, callback: Callable[[Dict], Awaitable[None]]):
        if callback in self._order_subscribers:
            self._order_subscribers.remove(callback)

    def set_credentials(self, api_key: str, api_secret: str):
        """Provide credentials so we can subscribe to private channels on (re)connect."""
        self._auth_key = api_key or ""
        self._auth_secret = api_secret or ""

    async def _notify_price(self, data: Dict):
        coros = []
        for cb in list(self._price_subscribers):
            try:
                if asyncio.iscoroutinefunction(cb):
                    coros.append(cb(data))
                else:
                    cb(data)
            except Exception as e:
                logger.error(f"Price subscriber error: {e}")
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    async def _notify_order(self, data: Dict):
        if not self._order_subscribers:
            return
        coros = [cb(data) for cb in list(self._order_subscribers)]
        await asyncio.gather(*coros, return_exceptions=True)

    # ── Subscriptions ──
    async def _subscribe_public(self, symbols: list):
        if not self._ws or not symbols:
            return
        msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "v2/ticker", "symbols": symbols},
                ]
            },
        }
        await self._ws.send(json.dumps(msg))
        logger.info(f"Subscribed to v2/ticker for: {symbols}")

    async def _authenticate_and_subscribe_private(self):
        if not self._ws or not self._auth_key or not self._auth_secret:
            return
        timestamp = str(int(time.time()))
        method = "GET"
        path = "/live"
        signature_data = method + timestamp + path
        signature = hmac.new(
            self._auth_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        auth_msg = {
            "type": "auth",
            "payload": {
                "api-key": self._auth_key,
                "signature": signature,
                "timestamp": timestamp,
            },
        }
        try:
            await self._ws.send(json.dumps(auth_msg))
            sub_msg = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {"name": "orders"},
                        {"name": "positions"},
                    ]
                },
            }
            await self._ws.send(json.dumps(sub_msg))
            logger.info("Subscribed to private orders + positions channels")
        except Exception as e:
            logger.warning(f"Private channel auth failed: {e}")

    # ── Message handling ──
    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return

        msg_type = data.get("type")

        if msg_type == "v2/ticker":
            symbol = data.get("symbol", "")
            if symbol:
                ticker = {
                    "symbol": symbol,
                    "price": float(data.get("close", 0) or data.get("mark_price", 0)),
                    "mark_price": float(data.get("mark_price", 0) or 0),
                    "spot_price": float(data.get("spot_price", 0) or 0),
                    "high": float(data.get("high", 0) or 0),
                    "low": float(data.get("low", 0) or 0),
                    "open": float(data.get("open", 0) or 0),
                    "close": float(data.get("close", 0) or 0),
                    "volume": float(data.get("volume", 0) or 0),
                    "turnover": float(data.get("turnover", 0) or 0),
                    "oi": data.get("oi", "0"),
                    "product_id": data.get("product_id"),
                    "timestamp": data.get("timestamp"),
                    "received_at": time.time(),
                }
                self._price_cache[symbol] = ticker
                await self._notify_price(ticker)
        elif msg_type in ("orders", "positions"):
            await self._notify_order(data)
        elif msg_type == "heartbeat":
            pass
        elif msg_type == "subscriptions":
            logger.info(f"Active subscriptions: {data}")
        elif msg_type == "error":
            logger.warning(f"WS error message: {data}")

    # ── Lifecycle ──
    async def connect(self, symbols: list = None):
        if symbols is not None:
            self._symbols = symbols
        elif not self._symbols:
            self._symbols = ["BTCUSD", "ETHUSD"]

        self._running = True
        self._reconnect_delay = 1

        while self._running:
            try:
                logger.info(f"Connecting to Delta WS: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2 ** 20,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_delay = 1
                    logger.info("WebSocket connected")

                    await self._subscribe_public(self._symbols)
                    if self._auth_key and self._auth_secret:
                        await self._authenticate_and_subscribe_private()

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            finally:
                self._connected = False
                self._ws = None

            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def start(self, symbols: list = None):
        if self._task and not self._task.done():
            logger.warning("WebSocket already running")
            return
        self._task = asyncio.create_task(self.connect(symbols))
        logger.info("WebSocket task started")

    async def stop(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._price_cache.clear()
        logger.info("WebSocket stopped")


# Global WebSocket instance
delta_ws = DeltaWebSocket()
