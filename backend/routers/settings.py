"""
Settings API routes — strategy configuration, API keys, connection testing.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.scheduler.bot_runner import bot_runner
from backend.config import settings
from backend.models.database import async_session
from backend.models.trade_log import BotSettings
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class StrategySettingsRequest(BaseModel):
    btc_enabled: bool = True
    eth_enabled: bool = True
    btc_quantity: int = 10
    eth_quantity: int = 10
    breakout_buffer_pct: float = 0.1
    cooldown_minutes: int = 5
    max_daily_loss: float = 100.0
    leverage: int = 10
    candle_resolution: str = "15m"
    lookback_candles: int = 6
    poll_interval_seconds: int = 3
    sl_atr_mult: float = 1.0
    tp_atr_mult: float = 1.5
    min_sl_pct: float = 0.15
    max_sl_pct: float = 1.5
    require_volume_confirmation: bool = False
    order_expiry_seconds: int = 1800


class ApiKeysRequest(BaseModel):
    api_key: str
    api_secret: str
    testnet: bool = True


@router.get("")
async def get_settings():
    """Get current strategy settings."""
    try:
        saved = await _load_settings()
        return {
            "success": True,
            "result": {
                **saved,
                "testnet": settings.DELTA_TESTNET,
                "has_api_key": bool(settings.DELTA_API_KEY),
                "rest_url": settings.rest_url,
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("")
async def update_settings(req: StrategySettingsRequest):
    """Update strategy settings."""
    try:
        new_settings = req.model_dump()

        # Save to database
        await _save_settings(new_settings)

        # Apply to running bot
        bot_runner.initialize(new_settings)

        return {"success": True, "message": "Settings updated", "result": new_settings}
    except Exception as e:
        logger.error(f"Failed to update settings: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api-keys")
async def update_api_keys(req: ApiKeysRequest):
    """Update Delta Exchange API credentials."""
    try:
        # Update in-memory settings
        settings.DELTA_API_KEY = req.api_key
        settings.DELTA_API_SECRET = req.api_secret
        settings.DELTA_TESTNET = req.testnet

        # Update bot client
        bot_runner.update_client(req.api_key, req.api_secret)

        # Save to database (encrypted in production, plain for now)
        await _save_setting("api_key_configured", "true")
        await _save_setting("testnet", str(req.testnet).lower())

        return {
            "success": True,
            "message": "API keys updated",
            "testnet": req.testnet,
            "rest_url": settings.rest_url,
        }
    except Exception as e:
        logger.error(f"Failed to update API keys: {e}")
        return {"success": False, "error": str(e)}


@router.post("/test-connection")
async def test_connection():
    """Test API connection and return account balance."""
    try:
        client = bot_runner.delta_client
        key_hint = '***' + client.api_key[-4:] if len(client.api_key) > 4 else '(empty)'
        logger.info(f"Testing connection | Key: {key_hint} | URL: {client.base_url}")

        result = await client.test_connection()

        if result.get("success"):
            balances = result.get("result", [])
            return {
                "success": True,
                "message": "Connection successful!",
                "balances": balances,
                "testnet": settings.DELTA_TESTNET,
            }
        else:
            error = result.get("error", {})
            error_code = error.get("code", "unknown") if isinstance(error, dict) else str(error)
            error_msg = error.get("message", error_code) if isinstance(error, dict) else str(error)

            # Provide user-friendly messages for common error codes
            friendly_messages = {
                "invalid_api_key": "Invalid API key. Make sure you copied the full key from Delta Exchange. Also verify you're using TESTNET keys with TESTNET mode (or PRODUCTION keys with PRODUCTION mode).",
                "ip_not_whitelisted": "Your IP is not whitelisted for this API key. Add your current IP in Delta Exchange API settings.",
                "signature_expired": "Signature expired — your system clock may be out of sync. Make sure your PC time is accurate.",
                "missing_credentials": "API key or secret is empty. Please enter both in the Settings page.",
                "unauthorized": f"Authentication failed: {error_msg}",
            }

            friendly = friendly_messages.get(error_code, f"Connection failed: {error_code} — {error_msg}")

            return {
                "success": False,
                "message": friendly,
                "error_code": error_code,
                "api_url": client.base_url,
            }
    except Exception as e:
        logger.error(f"Connection test failed: {e}")
        return {"success": False, "message": str(e)}


# ─── Database helpers ──────────────────────

async def _save_setting(key: str, value: str):
    """Save a single setting to the database."""
    async with async_session() as session:
        existing = await session.execute(select(BotSettings).where(BotSettings.key == key))
        setting = existing.scalar_one_or_none()
        if setting:
            setting.value = value
        else:
            session.add(BotSettings(key=key, value=value))
        await session.commit()


async def _save_settings(settings_dict: dict):
    """Save all strategy settings to the database."""
    await _save_setting("strategy_settings", json.dumps(settings_dict))


async def _load_settings() -> dict:
    """Load strategy settings from the database."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(BotSettings).where(BotSettings.key == "strategy_settings")
            )
            setting = result.scalar_one_or_none()
            if setting and setting.value:
                return json.loads(setting.value)
    except Exception:
        pass
    return settings.DEFAULT_SETTINGS.copy()
