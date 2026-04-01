"""
Delta Exchange India WebSocket Client.
Subscribes to real-time ticker feed for live price updates.
"""
import json
import asyncio
import logging
from typing import Dict, Optional, Callable, Set

import websockets

from backend.config import settings

logger = logging.getLogger(__name__)


class DeltaWebSocket:
    """WebSocket client for Delta Exchange real-time price feed."""

    def __init__(self):
        self._ws = None
        self._running = False
        self._price_cache: Dict[str, Dict] = {}
        self._subscribers: Set[Callable] = set()
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._task: Optional[asyncio.Task] = None

    @property
    def ws_url(self) -> str:
        return settings.ws_url

    @property
    def price_cache(self) -> Dict[str, Dict]:
        return self._price_cache.copy()

    def get_price(self, symbol: str) -> Optional[Dict]:
        """Get cached price data for a symbol."""
        return self._price_cache.get(symbol)

    def add_subscriber(self, callback: Callable):
        """Add a callback to receive price updates."""
        self._subscribers.add(callback)

    def remove_subscriber(self, callback: Callable):
        """Remove a price update callback."""
        self._subscribers.discard(callback)

    async def _notify_subscribers(self, data: Dict):
        """Notify all subscribers of a price update."""
        for callback in self._subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"Subscriber callback error: {e}")

    async def _subscribe(self, symbols: list):
        """Subscribe to ticker channels for given symbols."""
        if not self._ws:
            return

        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker",
                        "symbols": symbols
                    }
                ]
            }
        }

        await self._ws.send(json.dumps(subscribe_msg))
        logger.info(f"Subscribed to v2/ticker for: {symbols}")

    async def _handle_message(self, message: str):
        """Process an incoming WebSocket message."""
        try:
            data = json.loads(message)

            # Handle ticker updates
            if data.get("type") == "v2/ticker":
                symbol = data.get("symbol", "")
                if symbol:
                    ticker_data = {
                        "symbol": symbol,
                        "price": float(data.get("close", 0) or data.get("mark_price", 0)),
                        "mark_price": float(data.get("mark_price", 0)),
                        "spot_price": float(data.get("spot_price", 0)),
                        "high": float(data.get("high", 0)),
                        "low": float(data.get("low", 0)),
                        "open": float(data.get("open", 0)),
                        "close": float(data.get("close", 0)),
                        "volume": float(data.get("volume", 0)),
                        "turnover": float(data.get("turnover", 0)),
                        "oi": data.get("oi", "0"),
                        "product_id": data.get("product_id"),
                        "timestamp": data.get("timestamp"),
                    }
                    self._price_cache[symbol] = ticker_data
                    await self._notify_subscribers(ticker_data)

            elif data.get("type") == "heartbeat":
                pass  # Expected, keep alive

            elif data.get("type") == "subscriptions":
                logger.info(f"Active subscriptions: {data}")

        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Failed to parse WS message: {e}")

    async def connect(self, symbols: list = None):
        """Connect to Delta Exchange WebSocket and subscribe to ticker feed."""
        if symbols is None:
            symbols = ["BTCUSD", "ETHUSD"]

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
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1
                    logger.info("WebSocket connected successfully")

                    # Subscribe to ticker channels
                    await self._subscribe(symbols)

                    # Listen for messages
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            finally:
                self._ws = None

            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay
                )

    async def start(self, symbols: list = None):
        """Start the WebSocket connection in a background task."""
        if self._task and not self._task.done():
            logger.warning("WebSocket already running")
            return

        self._task = asyncio.create_task(self.connect(symbols))
        logger.info("WebSocket task started")

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._price_cache.clear()
        logger.info("WebSocket stopped")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.open


# Global WebSocket instance
delta_ws = DeltaWebSocket()
