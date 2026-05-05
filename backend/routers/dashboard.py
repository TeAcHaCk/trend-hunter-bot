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


@router.post("/bot/reset")
async def reset_bot():
    """Full reset: stop bot, cancel all orders, wipe all state and cache."""
    try:
        await bot_runner.reset()
        return {
            "success": True,
            "message": "Bot reset — all state cleared, orders cancelled",
            "state": bot_runner.state,
        }
    except Exception as e:
        logger.error(f"Failed to reset bot: {e}")
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


@router.get("/account-summary")
async def get_account_summary():
    """Combined account summary: wallet balance + trade stats + risk metrics.
    
    Replaces the old wallet-only panel with a trader-focused hybrid view.
    """
    from datetime import datetime
    from sqlalchemy import select, func, and_
    from backend.models.database import async_session
    from backend.models.trade_log import TradeLog

    summary = {
        "wallet": None,
        "stats": None,
        "risk": None,
    }

    # 1. Wallet balance from Delta Exchange
    try:
        wallet_raw = await bot_runner.delta_client.get_wallet_balances()
        balances = []
        if isinstance(wallet_raw, dict):
            raw_list = wallet_raw.get("result", [])
            if isinstance(raw_list, list):
                for b in raw_list:
                    bal = float(b.get("balance", 0) or 0)
                    avail = float(b.get("available_balance", 0) or 0)
                    if bal > 0:
                        balances.append({
                            "symbol": b.get("asset_symbol", "?"),
                            "balance": bal,
                            "available": avail,
                        })
        # Compute a total USD value (for USD-margined accounts, this is straightforward)
        usd_balance = 0.0
        for b in balances:
            if b["symbol"] in ("USD", "USDT", "USDC"):
                usd_balance += b["balance"]
        summary["wallet"] = {
            "balances": balances,
            "total_usd": round(usd_balance, 2),
        }
    except Exception as e:
        logger.warning(f"Account summary: wallet fetch failed: {e}")

    # 2. Trade stats from database
    try:
        async with async_session() as session:
            # Total closed trades
            total_closed = (await session.execute(
                select(func.count()).select_from(TradeLog).where(TradeLog.status == "closed")
            )).scalar() or 0

            # Wins
            total_wins = (await session.execute(
                select(func.count()).select_from(TradeLog).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl > 0)
                )
            )).scalar() or 0

            # Losses
            total_losses = (await session.execute(
                select(func.count()).select_from(TradeLog).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl < 0)
                )
            )).scalar() or 0

            # Total PnL
            total_pnl = (await session.execute(
                select(func.sum(TradeLog.pnl)).where(TradeLog.status == "closed")
            )).scalar() or 0.0

            # Today's PnL
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today_pnl = (await session.execute(
                select(func.sum(TradeLog.pnl)).where(
                    and_(TradeLog.status == "closed", TradeLog.timestamp >= today_start)
                )
            )).scalar() or 0.0

            # Today's trades
            today_trades = (await session.execute(
                select(func.count()).select_from(TradeLog).where(
                    and_(TradeLog.status == "closed", TradeLog.timestamp >= today_start)
                )
            )).scalar() or 0

            # Today's wins
            today_wins = (await session.execute(
                select(func.count()).select_from(TradeLog).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl > 0,
                         TradeLog.timestamp >= today_start)
                )
            )).scalar() or 0

            # Gross wins / gross losses for profit factor
            gross_wins = (await session.execute(
                select(func.sum(TradeLog.pnl)).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl > 0)
                )
            )).scalar() or 0.0

            gross_losses = abs((await session.execute(
                select(func.sum(TradeLog.pnl)).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl < 0)
                )
            )).scalar() or 0.0)

            # Best / worst trade
            best_trade = (await session.execute(
                select(func.max(TradeLog.pnl)).where(TradeLog.status == "closed")
            )).scalar() or 0.0

            worst_trade = (await session.execute(
                select(func.min(TradeLog.pnl)).where(TradeLog.status == "closed")
            )).scalar() or 0.0

            profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0.0

            summary["stats"] = {
                "total_closed": total_closed,
                "total_wins": total_wins,
                "total_losses": total_losses,
                "win_rate": round((total_wins / total_closed * 100), 1) if total_closed > 0 else 0,
                "total_pnl": round(total_pnl, 2),
                "today_pnl": round(today_pnl, 2),
                "today_trades": today_trades,
                "today_wins": today_wins,
                "today_win_rate": round((today_wins / today_trades * 100), 1) if today_trades > 0 else 0,
                "profit_factor": profit_factor,
                "best_trade": round(best_trade, 2),
                "worst_trade": round(worst_trade, 2),
            }
    except Exception as e:
        logger.warning(f"Account summary: stats fetch failed: {e}")

    # 3. Risk metrics from position manager
    try:
        pm = bot_runner.position_manager
        summary["risk"] = {
            "daily_pnl": round(pm._daily_pnl, 2),
            "max_daily_loss": pm.max_daily_loss,
            "daily_loss_pct": round(abs(pm._daily_pnl) / pm.max_daily_loss * 100, 1) if pm.max_daily_loss > 0 else 0,
            "leverage": pm.leverage,
        }
    except Exception as e:
        logger.warning(f"Account summary: risk fetch failed: {e}")

    return {"success": True, "result": summary}



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
