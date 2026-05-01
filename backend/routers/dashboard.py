"""
Dashboard API routes — bot control, live status, positions.
"""
import asyncio
import json
import logging
from typing import List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.scheduler.bot_runner import bot_runner
from backend.exchange.delta_ws import delta_ws

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])

# Track active WebSocket clients
_ws_clients: List[WebSocket] = []


@router.get("/status")
async def get_bot_status():
    """Get comprehensive bot status including prices, positions, strategies."""
    return {"success": True, "result": bot_runner.get_status()}


@router.post("/bot/start")
async def start_bot():
    """Start the trading bot."""
    try:
        await bot_runner.start()
        return {"success": True, "message": "Bot started", "state": bot_runner.state}
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        return {"success": False, "error": str(e)}


@router.post("/bot/stop")
async def stop_bot():
    """Stop the trading bot."""
    try:
        await bot_runner.stop()
        return {"success": True, "message": "Bot stopped", "state": bot_runner.state}
    except Exception as e:
        logger.error(f"Failed to stop bot: {e}")
        return {"success": False, "error": str(e)}


@router.post("/bot/pause")
async def pause_bot():
    """Pause/resume the trading bot."""
    try:
        if bot_runner.state == "PAUSED":
            await bot_runner.resume()
            return {"success": True, "message": "Bot resumed", "state": bot_runner.state}
        else:
            await bot_runner.pause()
            return {"success": True, "message": "Bot paused", "state": bot_runner.state}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/positions")
async def get_positions():
    """Get current open positions from Delta Exchange."""
    try:
        result = await bot_runner.delta_client.get_positions()
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/balance")
async def get_balance():
    """Get wallet balance from Delta Exchange."""
    try:
        result = await bot_runner.delta_client.get_wallet_balances()
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/candles/{symbol}")
async def get_candles(symbol: str, resolution: str = "15m", count: int = 50):
    """Get OHLC candle data for chart display."""
    try:
        candles = await bot_runner.delta_client.get_candles(
            symbol=symbol,
            resolution=resolution,
            num_candles=count,
        )
        # Format for KLineChart: {timestamp, open, high, low, close, volume}
        formatted = []
        for c in (candles or []):
            formatted.append({
                "timestamp": int(c.get("time", 0)) * 1000,  # KLineChart needs ms
                "open": float(c.get("open", 0)),
                "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)),
                "close": float(c.get("close", 0)),
                "volume": float(c.get("volume", 0)),
            })
        # Sort oldest first (KLineChart expects ascending order)
        formatted.sort(key=lambda x: x["timestamp"])
        return {"success": True, "result": formatted}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    """
    WebSocket endpoint for pushing live price/status updates to frontend.

    Two non-blocking sources feed into a single asyncio.Queue per client:
      1. price ticks from delta_ws.add_subscriber (event-driven, fan-out)
      2. a 1 Hz heartbeat that re-sends current bot status

    A consumer task drains the queue and serialises sends, so a slow
    socket can never stall the price-tick fan-out. Status pushes are
    coalesced to at most one every ~250 ms.
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info(f"WebSocket client connected. Total: {len(_ws_clients)}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    closed = asyncio.Event()
    last_status_push = 0.0
    STATUS_THROTTLE_S = 0.25  # at most 4 status pushes per second per client

    async def enqueue(msg: dict):
        if closed.is_set():
            return
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop the oldest to keep the queue snappy
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def on_price_update(data):
        """Forward price ticks AND piggyback a throttled status push."""
        nonlocal last_status_push
        await enqueue({"type": "price_update", "data": data})
        now = asyncio.get_event_loop().time()
        if now - last_status_push >= STATUS_THROTTLE_S:
            last_status_push = now
            await enqueue({"type": "status_update", "data": bot_runner.get_status()})

    delta_ws.add_subscriber(on_price_update)

    # Send an immediate status snapshot so the dashboard hydrates fast
    await enqueue({"type": "status_update", "data": bot_runner.get_status()})

    async def heartbeat():
        """Push a status snapshot once per second so the badge stays live
        even when the price feed is silent."""
        nonlocal last_status_push
        try:
            while not closed.is_set():
                await asyncio.sleep(1.0)
                now = asyncio.get_event_loop().time()
                if now - last_status_push >= STATUS_THROTTLE_S:
                    last_status_push = now
                    await enqueue({"type": "status_update", "data": bot_runner.get_status()})
        except asyncio.CancelledError:
            pass

    async def consumer():
        """Drain the queue and serialise sends to the socket."""
        try:
            while not closed.is_set():
                msg = await queue.get()
                await websocket.send_json(msg)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug(f"WebSocket consumer error: {e}")
        finally:
            closed.set()

    hb_task = asyncio.create_task(heartbeat())
    consumer_task = asyncio.create_task(consumer())

    try:
        # Block until the client disconnects (recv side just detects close)
        while not closed.is_set():
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        closed.set()
        delta_ws.remove_subscriber(on_price_update)
        for t in (hb_task, consumer_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total: {len(_ws_clients)}")
