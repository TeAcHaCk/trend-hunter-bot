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
    """WebSocket endpoint for pushing live price updates to frontend."""
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info(f"WebSocket client connected. Total: {len(_ws_clients)}")

    async def on_price_update(data):
        """Forward price updates to this WebSocket client."""
        try:
            await websocket.send_json({"type": "price_update", "data": data})
        except Exception:
            pass

    delta_ws.add_subscriber(on_price_update)

    try:
        while True:
            # Send periodic status updates
            status = bot_runner.get_status()
            await websocket.send_json({"type": "status_update", "data": status})
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        delta_ws.remove_subscriber(on_price_update)
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
