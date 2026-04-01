# 🎯 Trend Hunter Futures Bot

Automated BTC/ETH breakout trading bot for **Delta Exchange India** — pure price action, no lagging indicators.

## Strategy

The Trend Hunter strategy monitors the **24-hour high and low** of BTC and ETH perpetual futures. When price breaks out above the 24h high (plus a configurable buffer), it opens a **LONG** position. When price breaks below the 24h low (minus the buffer), it opens a **SHORT** position. Opposite signals trigger position reversal.

**Key features:**
- Pure price action — no EMAs, RSI, or other lagging indicators
- Configurable breakout buffer (default 0.1%)
- Whipsaw cooldown timer (default 5 minutes)
- Max daily loss auto-stop
- One active position per asset at a time

## Tech Stack

- **Backend:** Python (FastAPI + APScheduler)
- **Frontend:** HTML/CSS/JS (dark glassmorphism theme)
- **Database:** SQLite (async via aiosqlite)
- **Exchange:** Delta Exchange India (REST + WebSocket API)

## Quick Start

### 1. Install Dependencies
```bash
cd "C:\Users\Project Alpha\Stock Market\trend-hunter-bot"
conda activate base
pip install -r requirements.txt
```

### 2. Configure API Keys
Copy the example environment file and add your Delta Exchange India API credentials:
```bash
cp .env.example .env
# Edit .env with your API key and secret
```

### 3. Run the Bot
```bash
conda activate base
python -m uvicorn backend.main:app --reload --port 8000
```

> **Note:** Use `python -m uvicorn` instead of bare `uvicorn` since the script directory may not be on your PATH.

### 4. Open Dashboard
Navigate to [http://localhost:8000](http://localhost:8000) in your browser.

## Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Live prices, bot controls, positions |
| Settings | `/settings` | API keys, strategy params, safety guards |
| Trade Log | `/trades` | Trade history, PnL stats, CSV export |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DELTA_API_KEY` | Delta Exchange API Key | — |
| `DELTA_API_SECRET` | Delta Exchange API Secret | — |
| `DELTA_TESTNET` | Use testnet (`true`/`false`) | `true` |
| `APP_SECRET_KEY` | App encryption key | — |

## Safety Rules

1. **Testnet first** — always starts in testnet mode
2. **Max daily loss** — auto-pauses bot when threshold hit
3. **Whipsaw cooldown** — minimum wait between trades
4. **Duplicate position guard** — checks existing positions before ordering
5. **Rate limit handler** — retries with backoff on 429 errors

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Bot status + prices |
| POST | `/api/bot/start` | Start trading |
| POST | `/api/bot/stop` | Stop trading |
| POST | `/api/bot/pause` | Pause/resume |
| GET | `/api/settings` | Get settings |
| POST | `/api/settings` | Update settings |
| POST | `/api/settings/test-connection` | Test API |
| GET | `/api/trades` | Trade history |
| GET | `/api/trades/stats` | PnL stats |
| GET | `/api/trades/export` | CSV download |

## Docker

```bash
docker build -t trend-hunter-bot .
docker run -p 8000:8000 --env-file .env trend-hunter-bot
```

## License

MIT
