"""
Trades API routes — fetch trade history and stats directly from Delta Exchange API with caching.
"""
import csv
import io
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from backend.scheduler.bot_runner import bot_runner
from backend.models.database import async_session
from backend.models.trade_log import TradeLog
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trades", tags=["trades"])

# 5-minute cache for Delta API trade history
_trade_cache = {
    "timestamp": 0,
    "trades": [],
    "stats": {},
    "expires_in": 300  # 5 minutes
}

async def _fetch_and_cache_trades():
    global _trade_cache
    now = time.time()
    
    # Return cache if valid
    if now - _trade_cache["timestamp"] < _trade_cache["expires_in"] and _trade_cache["trades"]:
        return _trade_cache

    try:
        # Fetch from Delta Exchange API
        fills_res = await bot_runner.delta_client.get_fills(page_size=100)
        orders_res = await bot_runner.delta_client.get_order_history(page_size=100)

        fills = fills_res.get("result", []) if isinstance(fills_res, dict) else []
        orders = orders_res.get("result", []) if isinstance(orders_res, dict) else []

        # Map orders by ID to identify exit type (TP/SL)
        # Key fields from Delta API: order_type, stop_order_type, bracket_stop_loss_price, bracket_take_profit_price
        order_map = {str(o.get("id", "")): o for o in orders}

        formatted_trades = []
        total_pnl = 0.0
        today_pnl = 0.0
        total_wins = 0
        total_losses = 0

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        for fill in fills:
            order_id = str(fill.get("order_id", ""))
            order_obj = order_map.get(order_id, {})
            
            # === REAL PNL EXTRACTION ===
            # Delta Exchange API puts realized_pnl inside fill.meta_data.new_position
            # It only appears when a fill CLOSES a position (new_position.size == 0)
            meta_data = fill.get("meta_data", {}) or {}
            new_pos = meta_data.get("new_position", {}) or {}
            
            # Primary source: meta_data.new_position.realized_pnl
            fill_pnl = float(new_pos.get("realized_pnl", 0) or 0)
            
            # === REAL FEE EXTRACTION ===
            # Fee is stored as "commission" at the top level of fills (NOT "fee")
            fill_fee = float(fill.get("commission", 0) or 0)
            
            # Stats
            total_pnl += fill_pnl
            if fill_pnl > 0:
                total_wins += 1
            elif fill_pnl < 0:
                total_losses += 1

            # Parse Timestamp
            created_at_str = fill.get("created_at")
            fill_time = datetime.utcnow()
            if created_at_str:
                try:
                    fill_time = datetime.strptime(created_at_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
                except ValueError:
                    try:
                        fill_time = datetime.strptime(created_at_str.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        pass
            
            if fill_time >= today_start:
                today_pnl += fill_pnl

            # === EXIT TYPE DETECTION ===
            # Delta API uses stop_order_type for stop-based exits
            # Values: "stop_order", "take_profit_order", None
            exit_type = "Entry/Market"
            
            # Check if this fill closes a position (realized PnL > 0 means closing)
            is_closing = fill_pnl != 0 or float(new_pos.get("size", -1)) == 0
            
            if order_obj:
                stop_type = str(order_obj.get("stop_order_type", "") or "").lower()
                
                if "take_profit" in stop_type:
                    exit_type = "Hit TP 🎯"
                elif "stop_loss" in stop_type:
                    exit_type = "Hit SL 🛑"
                elif is_closing:
                    exit_type = "Closed"
                elif "buy" == fill.get("side", "").lower():
                    exit_type = "Entry Long"
                else:
                    exit_type = "Entry Short"

            symbol = fill.get("product_symbol") or fill.get("symbol") or f"Product {fill.get('product_id', '?')}"

            formatted_trades.append({
                "id": str(fill.get("id", "")),
                "symbol": symbol,
                "direction": "long" if fill.get("side", "").lower() == "buy" else "short",
                "size": float(fill.get("size", 0) or 0),
                "price": float(fill.get("price", 0) or 0),
                "pnl": round(fill_pnl, 4),
                "fee": round(fill_fee, 4),
                "timestamp": fill_time.isoformat() + "Z",
                "exit_type": exit_type,
                "status": "closed"
            })

        total_with_pnl = total_wins + total_losses
        _trade_cache["trades"] = formatted_trades
        
        # Calculate gross metrics for profit factor
        gross_profit = sum(t["pnl"] for t in formatted_trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in formatted_trades if t["pnl"] < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)

        _trade_cache["stats"] = {
            "total_trades": len(fills),
            "total_closed": len(fills),
            "total_open": 0,
            "total_wins": total_wins,
            "win_rate": round((total_wins / total_with_pnl * 100), 1) if total_with_pnl > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "today_pnl": round(today_pnl, 2),
            "profit_factor": profit_factor
        }

        _trade_cache["timestamp"] = now

    except Exception as e:
        logger.error(f"Failed to fetch trades from Delta API: {e}", exc_info=True)

    return _trade_cache


@router.get("")
async def get_trades(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Get paginated trade history from Delta API cache with optional filters."""
    cache = await _fetch_and_cache_trades()
    trades = cache["trades"]

    # Apply filters in memory
    filtered = []
    for t in trades:
        if symbol and t["symbol"] != symbol:
            continue
        if start_date:
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d")
                t_time = datetime.fromisoformat(t["timestamp"].replace("Z", ""))
                if t_time < start:
                    continue
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                t_time = datetime.fromisoformat(t["timestamp"].replace("Z", ""))
                if t_time >= end:
                    continue
            except ValueError:
                pass
        filtered.append(t)

    total = len(filtered)
    offset = (page - 1) * page_size
    paginated = filtered[offset:offset + page_size]

    return {
        "success": True,
        "result": paginated,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }
    }


@router.get("/stats")
async def get_trade_stats(
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """Get trade statistics aggregated from Delta API history, with optional date filtering."""
    cache = await _fetch_and_cache_trades()
    trades = cache["trades"]

    # Filter trades by date if provided
    filtered = []
    for t in trades:
        if start_date:
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d")
                t_time = datetime.fromisoformat(t["timestamp"].replace("Z", ""))
                if t_time < start:
                    continue
            except ValueError:
                pass
        if end_date:
            try:
                end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                t_time = datetime.fromisoformat(t["timestamp"].replace("Z", ""))
                if t_time >= end:
                    continue
            except ValueError:
                pass
        filtered.append(t)

    # Recalculate stats based on the filtered trades
    total_closed = len(filtered)
    total_pnl = sum(float(t["pnl"]) for t in filtered)
    total_wins = sum(1 for t in filtered if float(t["pnl"]) > 0)
    
    gross_profit = sum(float(t["pnl"]) for t in filtered if float(t["pnl"]) > 0)
    gross_loss = abs(sum(float(t["pnl"]) for t in filtered if float(t["pnl"]) < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)

    # Today's PnL is always based on today, but if filtering, we can just return it from cache
    today_pnl = cache["stats"].get("today_pnl", 0.0)

    return {
        "success": True,
        "result": {
            "total_trades": total_closed,
            "total_closed": total_closed,
            "total_open": 0,
            "total_wins": total_wins,
            "win_rate": round((total_wins / total_closed * 100), 1) if total_closed > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "today_pnl": today_pnl,
            "profit_factor": profit_factor
        }
    }


@router.get("/export")
async def export_trades_csv(
    symbol: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """Export trade history as CSV file."""
    try:
        cache = await _fetch_and_cache_trades()
        trades = cache["trades"]

        # Filter (basic inline filtering)
        filtered = []
        for t in trades:
            if symbol and t["symbol"] != symbol: continue
            filtered.append(t)

        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Timestamp", "Symbol", "Direction", "Price",
                         "Size", "PnL", "Fee", "Exit Type", "Order ID"])
        for t in filtered:
            writer.writerow([
                t["timestamp"],
                t["symbol"], t["direction"], t["price"],
                t["size"], t["pnl"], t["fee"],
                t["exit_type"], t["id"]
            ])

        output.seek(0)
        filename = f"delta_trades_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Failed to export trades: {e}")
        return {"success": False, "error": str(e)}
