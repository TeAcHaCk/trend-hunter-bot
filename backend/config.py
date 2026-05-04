"""
Configuration module for Trend Hunter Bot.
Loads environment variables and provides dynamic URL selection.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Explicitly load .env from the project root
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env", override=True)


class Settings:
    """Application settings loaded from environment variables."""

    # Delta Exchange India URLs
    PROD_REST_URL = "https://api.india.delta.exchange"
    TESTNET_REST_URL = "https://cdn-ind.testnet.deltaex.org"

    PROD_WS_URL = "wss://socket.india.delta.exchange"
    TESTNET_WS_URL = "wss://socket.testnet.deltaex.org"

    # Default Strategy Settings
    DEFAULT_SETTINGS = {
        "btc_enabled": True,
        "eth_enabled": True,
        "btc_quantity": 10,
        "eth_quantity": 10,
        "breakout_buffer_pct": 0.05,
        "cooldown_minutes": 3,
        "max_daily_loss": 100.0,
        "leverage": 10,
        # Faster default poll — REST is async now, so this is cheap
        "poll_interval_seconds": 3,
        "candle_resolution": "5m",
        "lookback_candles": 8,
        # ATR-based bracket exits (with sensible bounds)
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 2.5,
        "min_sl_pct": 0.15,
        "max_sl_pct": 1.5,
        # Filters (volume filter OFF by default — enable for production)
        "require_volume_confirmation": False,
        # How long pending stop orders live before being cancelled & re-armed
        "order_expiry_seconds": 600,
        # EMA trend filter
        "ema_period": 20,
        # Minimum range spread % to avoid noise breakouts
        "min_range_pct": 0.08,
        # Trailing stop-loss
        "use_trailing_sl": True,
        "trail_activation_pct": 0.15,
    }

    def __init__(self):
        """Load all settings from environment on init."""
        self._load_from_env()
        self.DATABASE_URL: str = "sqlite+aiosqlite:///./trend_hunter.db"

    def _load_from_env(self):
        """Load/reload settings from environment variables."""
        self.DELTA_API_KEY: str = os.getenv("DELTA_API_KEY", "")
        self.DELTA_API_SECRET: str = os.getenv("DELTA_API_SECRET", "")
        self.DELTA_TESTNET: bool = os.getenv("DELTA_TESTNET", "true").lower() == "true"
        self.TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
        self.APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "default-secret-key")

        key_hint = '***' + self.DELTA_API_KEY[-4:] if len(self.DELTA_API_KEY) > 4 else '(empty)'
        print(f"[Config] Loaded | Key: {key_hint} | Testnet: {self.DELTA_TESTNET} | URL: {self.rest_url}")

    @property
    def rest_url(self) -> str:
        return self.TESTNET_REST_URL if self.DELTA_TESTNET else self.PROD_REST_URL

    @property
    def ws_url(self) -> str:
        return self.TESTNET_WS_URL if self.DELTA_TESTNET else self.PROD_WS_URL

    def update_from_env(self):
        """Reload settings from environment."""
        self._load_from_env()


settings = Settings()
