"""
Trades API routes — trade history, filtering, CSV export, stats.
"""
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func, and_

from backend.models.database import async_session
from backend.models.trade_log import TradeLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("")
async def get_trades(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    status: Optional[str] = Query(None, description="Filter by status"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Get paginated trade history with optional filters."""
    try:
        async with async_session() as session:
            query = select(TradeLog).order_by(desc(TradeLog.timestamp))

            # Apply filters
            conditions = []
            if symbol:
                conditions.append(TradeLog.symbol == symbol)
            if status:
                conditions.append(TradeLog.status == status)
            if start_date:
                try:
                    start = datetime.strptime(start_date, "%Y-%m-%d")
                    conditions.append(TradeLog.timestamp >= start)
                except ValueError:
                    pass
            if end_date:
                try:
                    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                    conditions.append(TradeLog.timestamp < end)
                except ValueError:
                    pass

            if conditions:
                query = query.where(and_(*conditions))

            # Count total
            count_query = select(func.count()).select_from(TradeLog)
            if conditions:
                count_query = count_query.where(and_(*conditions))
            total_result = await session.execute(count_query)
            total = total_result.scalar() or 0

            # Paginate
            offset = (page - 1) * page_size
            query = query.offset(offset).limit(page_size)

            result = await session.execute(query)
            trades = result.scalars().all()

            return {
                "success": True,
                "result": [t.to_dict() for t in trades],
                "meta": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                }
            }
    except Exception as e:
        logger.error(f"Failed to fetch trades: {e}")
        return {"success": False, "error": str(e)}


@router.get("/stats")
async def get_trade_stats():
    """Get trade statistics — daily PnL, total PnL, win rate."""
    try:
        async with async_session() as session:
            # Total closed trades
            total_result = await session.execute(
                select(func.count()).select_from(TradeLog).where(TradeLog.status == "closed")
            )
            total_closed = total_result.scalar() or 0

            # Winning trades
            wins_result = await session.execute(
                select(func.count()).select_from(TradeLog).where(
                    and_(TradeLog.status == "closed", TradeLog.pnl > 0)
                )
            )
            total_wins = wins_result.scalar() or 0

            # Total PnL
            pnl_result = await session.execute(
                select(func.sum(TradeLog.pnl)).where(TradeLog.status == "closed")
            )
            total_pnl = pnl_result.scalar() or 0.0

            # Today's PnL
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today_pnl_result = await session.execute(
                select(func.sum(TradeLog.pnl)).where(
                    and_(TradeLog.status == "closed", TradeLog.timestamp >= today_start)
                )
            )
            today_pnl = today_pnl_result.scalar() or 0.0

            # Total open trades
            open_result = await session.execute(
                select(func.count()).select_from(TradeLog).where(TradeLog.status == "open")
            )
            total_open = open_result.scalar() or 0

            # All-time trade count
            all_result = await session.execute(
                select(func.count()).select_from(TradeLog)
            )
            total_all = all_result.scalar() or 0

            return {
                "success": True,
                "result": {
                    "total_trades": total_all,
                    "total_closed": total_closed,
                    "total_open": total_open,
                    "total_wins": total_wins,
                    "win_rate": round((total_wins / total_closed * 100), 1) if total_closed > 0 else 0,
                    "total_pnl": round(total_pnl, 2),
                    "today_pnl": round(today_pnl, 2),
                }
            }
    except Exception as e:
        logger.error(f"Failed to fetch stats: {e}")
        return {"success": False, "error": str(e)}


@router.get("/export")
async def export_trades_csv(
    symbol: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """Export trade history as CSV file."""
    try:
        async with async_session() as session:
            query = select(TradeLog).order_by(desc(TradeLog.timestamp))

            conditions = []
            if symbol:
                conditions.append(TradeLog.symbol == symbol)
            if start_date:
                try:
                    conditions.append(TradeLog.timestamp >= datetime.strptime(start_date, "%Y-%m-%d"))
                except ValueError:
                    pass
            if end_date:
                try:
                    conditions.append(
                        TradeLog.timestamp < datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                    )
                except ValueError:
                    pass

            if conditions:
                query = query.where(and_(*conditions))

            result = await session.execute(query)
            trades = result.scalars().all()

            # Generate CSV
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Timestamp", "Symbol", "Direction", "Entry Price",
                             "Exit Price", "Quantity", "PnL", "Status", "Order ID"])
            for t in trades:
                writer.writerow([
                    t.timestamp.isoformat() if t.timestamp else "",
                    t.symbol, t.direction, t.entry_price,
                    t.exit_price or "", t.quantity, t.pnl or "",
                    t.status, t.order_id or ""
                ])

            output.seek(0)
            filename = f"trades_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
    except Exception as e:
        logger.error(f"Failed to export trades: {e}")
        return {"success": False, "error": str(e)}
