# EUR/USD Signal Dashboard

A full-stack ICT/SMC signal analysis dashboard for FX and commodities trading.
Provides walk-forward backtesting, multi-pair correlation filtering, COT data,
and a hardened API backend — all runnable locally with zero external dependencies
in demo mode.

> **This is a research and decision-support tool. It does not execute trades.**
> Broker execution is disabled by default and requires deliberate configuration.

---

## Project Overview

| Layer     | Stack                                                                 |
|-----------|-----------------------------------------------------------------------|
| Frontend  | React 18 · Vite · TailwindCSS · Recharts                             |
| Backend   | FastAPI · SQLAlchemy 2 · SQLite · Pydantic v2                         |
| Signal engine | ICT/SMC: HTF bias, liquidity sweep, BOS/CHoCH, FVG, session timing |
| Data      | Demo (seeded mock) or live via Twelve Data / Alpha Vantage / OANDA / Polygon / FMP |
| Pairs     | EUR/USD · GBP/USD · USD/JPY · XAU/USD                                |

---

## Quick Start — Demo Mode (no API keys required)

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

# Copy the example env file (demo mode is the default)
cp .env.example .env

uvicorn main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.
Interactive docs: `http://localhost:8000/docs`

### 2. Frontend

```bash
# From the project root
npm install
npm run dev
```

Open `http://localhost:5173` in your browser.

---

## Environment Variables

Copy `backend/.env.example` to `backend/.env` and adjust values.

```env
# ── Data mode ─────────────────────────────────────────────────────────────────
# "demo"  — deterministic mock data, no API key required
# "live"  — routes to a real data provider (FX_API_KEY required)
DATA_MODE=demo

# ── FX candle provider ────────────────────────────────────────────────────────
# Options: twelvedata | alpha_vantage | oanda | polygon | fmp
FX_DATA_PROVIDER=twelvedata
FX_API_KEY=your_fx_api_key_here

# ── Economic calendar provider ────────────────────────────────────────────────
# Options: fmp | trading_economics | eodhd
CALENDAR_PROVIDER=fmp
CALENDAR_API_KEY=your_calendar_api_key_here

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL=sqlite:///./eurusd_signals.db

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ORIGINS=http://localhost:5173

# ── Broker execution (disabled by default — see Safety Warnings) ──────────────
BROKER_EXECUTION_ENABLED=false
BROKER_PROVIDER=oanda
BROKER_API_KEY=
BROKER_ACCOUNT_ID=
MAX_RISK_PER_TRADE_PERCENT=0.25
DAILY_LOSS_LIMIT_PERCENT=1.0
```

---

## Switching to Live Data

1. Set `DATA_MODE=live` in `.env`.
2. Choose a provider and set `FX_DATA_PROVIDER` accordingly.
3. Add the corresponding API key to `FX_API_KEY`.
4. Restart the backend: `uvicorn main:app --reload`.

The header badge in the UI switches from **Demo Mode** (amber) to **Live Mode** (green)
when connected to a real provider.

**Provider free-tier limits (approximate):**

| Provider       | Free candles/day | Notes                         |
|----------------|-----------------|-------------------------------|
| Twelve Data    | 800 req/day     | Good for H1/H4 FX             |
| Alpha Vantage  | 500 req/day     | Requires `from_sym`/`to_sym`  |
| OANDA          | Unlimited       | Requires broker account       |
| Polygon.io     | 5 req/minute    | Paid tier for FX history      |
| FMP            | 250 req/day     | Also provides calendar        |

---

## Database Setup

SQLite is used by default — no installation required.
The database file is created automatically on first startup at:

```
backend/eurusd_signals.db
```

To reset the database:

```bash
rm backend/eurusd_signals.db
# Restart the backend — tables are recreated automatically
```

For PostgreSQL on a VPS:

```env
DATABASE_URL=postgresql+psycopg2://user:password@localhost/eurusd_signals
```

Install the driver: `pip install psycopg2-binary`

---

## Backend API Reference

| Method | Endpoint                          | Rate limit    | Description                      |
|--------|-----------------------------------|---------------|----------------------------------|
| GET    | `/api/v1/health`                  | —             | Server + DB status               |
| GET    | `/api/v1/candles`                 | —             | OHLCV candles (demo or live)     |
| GET    | `/api/v1/calendar`                | —             | Macro calendar events            |
| GET    | `/api/v1/signal/current`          | —             | Current signal (mock)            |
| POST   | `/api/v1/signal/analyze`          | 10/min/IP     | Run ICT engine on candles        |
| POST   | `/api/v1/signal/confirm`          | —             | Save confirmed signal to DB      |
| PUT    | `/api/v1/signal/{id}/result`      | —             | Record trade outcome             |
| GET    | `/api/v1/backtest/run`            | 5/min/IP      | Walk-forward backtest            |
| GET    | `/api/v1/backtest/optimize`       | 3/min/IP      | Walk-forward parameter optimizer |
| GET    | `/api/v1/execution/status`        | —             | Broker execution state           |
| POST   | `/api/v1/execution/place-order`   | 5/min/IP (disabled) | Place market order (disabled) |
| POST   | `/api/v1/execution/kill-switch`   | —             | Emergency execution block        |

Full interactive documentation: `http://localhost:8000/docs`

---

## Backtesting

The backtester runs the live ICT signal engine over historical candles with
strict no-look-ahead-bias guarantees:

- Each bar only sees candles up to and including itself.
- TP/SL resolution scans forward *after* entry — this is permitted and represents
  what actually happened after the trade was taken.
- Spread (default 1.0 pip) and slippage (default 0.5 pip) are applied at entry.

```bash
# Run a backtest via curl
curl "http://localhost:8000/api/v1/backtest/run?pair=EUR/USD&timeframe=H4&lookback=500"

# Walk-forward parameter optimization
curl "http://localhost:8000/api/v1/backtest/optimize?pair=EUR/USD&timeframe=H4&lookback=1000"
```

Spread and slippage can be adjusted in `.env`:

```env
BACKTEST_SPREAD_PIPS=1.0
BACKTEST_SLIPPAGE_PIPS=0.5
```

---

## Manual Trading Workflow

This dashboard is designed for a **manual confirmation workflow**:

1. **Analyze** — Click "Run Analysis" or wait for the auto-refresh to show a signal.
2. **Review** — Check the trade plan: entry, stop-loss, take-profit, invalidation.
3. **Confirm** — If the setup meets your criteria, click "Confirm Signal" to save it.
4. **Execute manually** — Place the trade in your broker platform manually.
5. **Record result** — After the trade closes, update the outcome in the dashboard.
6. **Review analytics** — Track win rate, expectancy, and drawdown over time.

---

## Production Deployment (VPS)

### Backend (systemd service)

```bash
# Install dependencies
pip install -r requirements.txt

# Run with production ASGI server
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

> Use `--workers 1` — the in-memory rate limiter and kill-switch state are
> not shared across processes. For multi-worker setups, configure a Redis
> backend for SlowAPI.

Example `/etc/systemd/system/eurusd-api.service`:

```ini
[Unit]
Description=EUR/USD Signal API
After=network.target

[Service]
WorkingDirectory=/opt/eurusd-dashboard/backend
ExecStart=/opt/eurusd-dashboard/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
Environment=PATH=/opt/eurusd-dashboard/.venv/bin

[Install]
WantedBy=multi-user.target
```

### Frontend (build and serve)

```bash
npm run build
# Serve the dist/ folder via nginx or any static file server
```

Update `CORS_ORIGINS` in `.env` to your public domain:

```env
CORS_ORIGINS=https://yourdomain.com
```

### Nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        root /opt/eurusd-dashboard/dist;
        try_files $uri $uri/ /index.html;
    }
}
```

---

## Logging

The backend emits structured JSON logs to stdout:

```json
{"time": "2026-05-13T18:00:00Z", "level": "INFO", "name": "routers.signal",
 "message": "Signal analyzed signal=BUY quality=85 session=London"}
```

Events logged:
- Every HTTP request: method, path, status, duration (ms), request ID
- Signal analyzed / confirmed / result recorded
- Backtest and optimizer runs with pair and key results
- Data provider errors with provider name
- Execution attempts (all blocked with reason)
- Kill switch activations (CRITICAL level)

To increase verbosity, set `LOG_LEVEL=DEBUG` in `.env` and update `setup_logging()` in `main.py`.

---

## Safety Warnings

**Read this section before making any changes to the execution configuration.**

### Broker execution is disabled by default

The `BROKER_EXECUTION_ENABLED` flag defaults to `false`. The `place_market_order()`
function is a non-functional placeholder that raises `NotImplementedError`.

Do not set `BROKER_EXECUTION_ENABLED=true` until **all** of the following are true:

- [ ] You have completed at least **100 backtested setups** with positive expectancy
- [ ] You have paper-traded at least **50 signals** and reviewed each manually
- [ ] Both in-sample and out-of-sample backtest expectancy are positive
- [ ] Maximum drawdown is within your personal risk tolerance
- [ ] You have integrated and tested a real broker SDK in `broker_provider.py`
- [ ] You have tested the broker integration against a **practice/demo account first**

### Risk limits

The safety chain rejects orders that do not meet:
- Quality score ≥ 80
- Risk-reward ≥ 2.5
- News blackout not active
- Stop-loss, take-profit, and invalidation condition all specified
- Max risk per trade: 0.25% of account
- Daily loss limit: 1.0% of account

### Emergency kill switch

`POST /api/v1/execution/kill-switch` immediately blocks all execution.
It **cannot be reversed via API** — a server restart is required.

### This tool does not guarantee profitability

Past backtest performance is indicative only. Real trading involves slippage,
gaps, liquidity constraints, and psychological factors that cannot be modelled.
Always use a regulated broker, proper risk management, and consult a qualified
financial advisor before trading real money.

---

## Frontend Scripts

```bash
npm run dev        # Start development server (http://localhost:5173)
npm run build      # Production build to dist/
npm run preview    # Preview production build locally
npm run lint       # ESLint check
```

---

## Project Structure

```
eurusd-dashboard/
├── backend/
│   ├── data/                 # Candle + calendar data adapters
│   ├── models/               # Pydantic response models
│   ├── routers/              # FastAPI route handlers
│   │   ├── health.py
│   │   ├── candles.py
│   │   ├── calendar.py
│   │   ├── signal.py
│   │   ├── analytics.py
│   │   ├── backtest.py
│   │   └── execution.py      # Disabled broker skeleton
│   ├── services/
│   │   ├── signal_engine.py  # ICT/SMC signal logic
│   │   ├── backtester.py     # Walk-forward backtester
│   │   ├── optimizer.py      # IS/OOS parameter optimizer
│   │   ├── candle_provider.py
│   │   ├── cot_provider.py   # CFTC COT data
│   │   ├── correlation_filter.py
│   │   └── broker_provider.py  # Placeholder — not functional
│   ├── config.py             # pydantic-settings from .env
│   ├── logging_config.py     # Structured JSON logging
│   ├── middleware.py         # Request logging middleware
│   ├── rate_limit.py         # slowapi limiter instance
│   ├── pair_config.py        # Multi-pair pip/target registry
│   ├── exceptions.py         # Typed provider errors
│   ├── main.py               # App factory + middleware wiring
│   ├── requirements.txt
│   └── .env.example
├── src/
│   ├── components/           # React UI components
│   │   ├── Dashboard.jsx
│   │   ├── SignalCard.jsx
│   │   ├── TradePlanCard.jsx
│   │   ├── BacktestDashboard.jsx
│   │   ├── ExecutionPanel.jsx  # Disabled broker UI
│   │   └── ...
│   ├── services/
│   │   └── api.js            # Typed API client
│   └── data/
│       └── mockData.js       # Fallback mock data
├── package.json
├── vite.config.js
└── README.md
```Auto-deploy test.
