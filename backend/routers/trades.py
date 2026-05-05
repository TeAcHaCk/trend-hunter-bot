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

        # Map orders by ID to identify TP/SL
        order_map = {str(o.get("id", "")): o for o in orders}

        formatted_trades = []
        total_pnl = 0.0
        today_pnl = 0.0
        total_wins = 0
        total_closed = len(fills)

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        for fill in fills:
            # Parse PnL & Fee
            fill_pnl = float(fill.get("realized_pnl", 0) or 0)
            fill_fee = float(fill.get("fee", 0) or 0)
            
            # For accurate stats, realized_pnl from delta is generally what we want
            total_pnl += fill_pnl
            if fill_pnl > 0:
                total_wins += 1

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

            # Determine Exit Type (TP/SL/Manual)
            exit_type = "Market/Manual"
            order_id = str(fill.get("order_id", ""))
            
            if order_id and order_id in order_map:
                o_type = order_map[order_id].get("order_type", "").lower()
                if "take_profit" in o_type:
                    exit_type = "TP"
                elif "stop_loss" in o_type or "stop_limit" in o_type or "stop_market" in o_type:
                    exit_type = "SL"

            # Fills symbol is typically returned as 'symbol' or 'product_symbol'
            symbol = fill.get("symbol") or fill.get("product_symbol") or f"Product {fill.get('product_id', '?')}"

            formatted_trades.append({
                "id": str(fill.get("id", "")),
                "symbol": symbol,
                "direction": "long" if fill.get("side", "").lower() == "buy" else "short",
                "size": float(fill.get("size", 0) or 0),
                "price": float(fill.get("price", 0) or 0),
                "pnl": fill_pnl,
                "fee": fill_fee,
                "timestamp": fill_time.isoformat() + "Z",
                "exit_type": exit_type,
                "status": "closed"
            })

        _trade_cache["trades"] = formatted_trades
        
        # Calculate gross metrics for profit factor
        gross_profit = sum(float(f.get("realized_pnl", 0) or 0) for f in fills if float(f.get("realized_pnl", 0) or 0) > 0)
        gross_loss = abs(sum(float(f.get("realized_pnl", 0) or 0) for f in fills if float(f.get("realized_pnl", 0) or 0) < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)

        _trade_cache["stats"] = {
            "total_trades": total_closed,
            "total_closed": total_closed,
            "total_open": 0,
            "total_wins": total_wins,
            "win_rate": round((total_wins / total_closed * 100), 1) if total_closed > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "today_pnl": round(today_pnl, 2),
            "profit_factor": profit_factor
        }

        _trade_cache["timestamp"] = now

    except Exception as e:
        logger.error(f"Failed to fetch trades from Delta API: {e}")

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
async def get_trade_stats():
    """Get trade statistics aggregated from Delta API history."""
    cache = await _fetch_and_cache_trades()
    return {
        "success": True,
        "result": cache["stats"]
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
