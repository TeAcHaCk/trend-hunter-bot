"""
Trend Hunter Futures Bot — FastAPI Application Entry Point.
"""
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

from backend.models.database import init_db
from backend.scheduler.bot_runner import bot_runner
from backend.routers import dashboard, settings, trades

# Configure logging — output to both console and file
import sys
import io

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler — force UTF-8 to avoid cp1252 emoji crashes
utf8_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
console_handler = logging.StreamHandler(utf8_stream)
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# File handler — captures ALL output including scheduler
file_handler = logging.FileHandler("bot_debug.log", mode="w", encoding="utf-8")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.DEBUG)

# Set up root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — startup and shutdown."""
    # Startup
    logger.info("=" * 60)
    logger.info("🚀 Trend Hunter Futures Bot starting...")
    logger.info("=" * 60)

    # Initialize database
    await init_db()
    logger.info("✅ Database initialized")

    # Initialize bot with saved/default settings
    bot_runner.initialize()
    logger.info("✅ Bot initialized")

    yield

    # Shutdown
    logger.info("Shutting down...")
    if bot_runner.state != "STOPPED":
        await bot_runner.stop()
    logger.info("👋 Trend Hunter Bot stopped")


# Create FastAPI app
app = FastAPI(
    title="Trend Hunter Futures Bot",
    description="Automated BTC/ETH breakout trading bot for Delta Exchange India",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Include API routers
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(trades.router)


# ─── HTML Page Routes ──────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the main dashboard page."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/settings", response_class=HTMLResponse)
async def serve_settings():
    """Serve the settings page."""
    return FileResponse(str(FRONTEND_DIR / "settings.html"))


@app.get("/trades", response_class=HTMLResponse)
async def serve_trades():
    """Serve the trade log page."""
    return FileResponse(str(FRONTEND_DIR / "trades.html"))


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "bot_state": bot_runner.state,
        "version": "1.0.0",
    }


@app.get("/debug/modules")
async def debug_modules():
    """Debug: show loaded module paths."""
    import inspect
    from backend.strategy.trend_hunter import TrendHunterStrategy
    from backend.scheduler import bot_runner as br_module
    
    strat_src = inspect.getsource(TrendHunterStrategy.get_status)
    runner_src = inspect.getsource(br_module.BotRunner._run_strategy_check)
    
    return {
        "trend_hunter_file": inspect.getfile(TrendHunterStrategy),
        "bot_runner_file": inspect.getfile(br_module.BotRunner),
        "get_status_has_range_high": "range_high" in strat_src,
        "get_status_has_high_24h": "high_24h" in strat_src,
        "strategy_check_has_get_candles": "get_candles" in runner_src,
        "strategy_check_has_strategy_debug": "strategy_debug" in runner_src,
        "cwd": os.getcwd(),
    }
