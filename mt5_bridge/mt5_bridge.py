"""
MT5 Windows Price Bridge
========================
Runs NATIVELY on Windows (outside Docker).
Connects to Exness MT5, streams real-time prices and historical candles,
and serves them over HTTP so the Docker backend can consume them.

Endpoint summary:
  GET /health                          → connection status
  GET /tick/{symbol}                   → latest bid/ask tick
  GET /candles/{symbol}/{timeframe}    → OHLCV candle list
  GET /pairs                           → available symbols

Usage:
  pip install -r requirements.txt
  python mt5_bridge.py

  Or double-click start_bridge.bat

The backend reads from: http://host.docker.internal:8765
(Docker's alias for the Windows host machine)
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv

# ── Load .env from project root (one level up from mt5_bridge/) ───────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', 'backend', '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MT5-BRIDGE] %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('mt5_bridge')

# ── Config ────────────────────────────────────────────────────────────────────

MT5_LOGIN    = int(os.getenv('MT5_LOGIN', '0'))
MT5_PASSWORD = os.getenv('MT5_PASSWORD', '')
MT5_SERVER   = os.getenv('MT5_SERVER', 'Exness-MT5Trial9')
BRIDGE_PORT  = int(os.getenv('MT5_BRIDGE_PORT', '8765'))

# Symbol map: our pair codes → MT5 broker symbols
SYMBOL_MAP = {
    'eurusd': 'EURUSDm',   # Exness uses 'm' suffix for micro accounts; adjust if needed
    'xauusd': 'XAUUSDm',
}

# Timeframe map
TF_MAP = {
    'M1':  mt5.TIMEFRAME_M1,
    'M5':  mt5.TIMEFRAME_M5,
    'M15': mt5.TIMEFRAME_M15,
    'H1':  mt5.TIMEFRAME_H1,
    'H4':  mt5.TIMEFRAME_H4,
    'D1':  mt5.TIMEFRAME_D1,
}

# ── MT5 connection ─────────────────────────────────────────────────────────────

def _connect() -> bool:
    if not mt5.initialize():
        log.error("MT5 initialize() failed — is MetaTrader 5 installed?")
        return False
    if MT5_LOGIN:
        ok = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            info = mt5.last_error()
            log.error("MT5 login failed: %s", info)
            return False
        log.info("MT5 connected — login=%s server=%s", MT5_LOGIN, MT5_SERVER)
    else:
        log.warning("MT5_LOGIN not set — using already-open terminal session")
    return True


connected = _connect()

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MT5 Windows Price Bridge",
    description="Relays live Exness MT5 prices to the Docker backend.",
    version="1.0.0",
)


def _ok(data) -> JSONResponse:
    return JSONResponse({"status": "ok", "data": data, "ts": datetime.now(tz=timezone.utc).isoformat()})


def _resolve_symbol(pair: str) -> str:
    """Convert pair code (eurusd) to broker symbol (EURUSDm). Auto-detects suffix."""
    base = pair.upper()
    # Try exact first
    if mt5.symbol_info(base):
        return base
    # Try with 'm' suffix (Exness micro)
    if mt5.symbol_info(base + 'm'):
        return base + 'm'
    # Try without suffix
    plain = base.replace('m', '').replace('M', '')
    if mt5.symbol_info(plain):
        return plain
    # Fall back to map
    return SYMBOL_MAP.get(pair.lower(), base)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    info = mt5.terminal_info()
    account = mt5.account_info()
    return _ok({
        "mt5_connected":  connected,
        "terminal_build": getattr(info, 'build', None) if info else None,
        "server":         getattr(account, 'server', None) if account else MT5_SERVER,
        "login":          getattr(account, 'login', None) if account else MT5_LOGIN,
        "currency":       getattr(account, 'currency', None) if account else None,
        "balance":        getattr(account, 'balance', None) if account else None,
    })


@app.get("/pairs")
def pairs():
    """List all available symbols in the connected MT5 terminal."""
    symbols = mt5.symbols_get()
    if symbols is None:
        raise HTTPException(503, "MT5 not connected")
    names = [s.name for s in symbols if s.visible]
    return _ok({"symbols": names, "count": len(names)})


@app.get("/tick/{pair}")
def tick(pair: str):
    """Latest bid/ask for the given pair."""
    if not connected:
        raise HTTPException(503, "MT5 bridge not connected")
    symbol = _resolve_symbol(pair)
    t = mt5.symbol_info_tick(symbol)
    if t is None:
        raise HTTPException(404, f"No tick data for {symbol} — check symbol name")
    return _ok({
        "pair":    pair.lower(),
        "symbol":  symbol,
        "bid":     t.bid,
        "ask":     t.ask,
        "spread":  round((t.ask - t.bid) * (100000 if 'usd' in pair.lower() and 'xau' not in pair.lower() else 10), 1),
        "time":    datetime.fromtimestamp(t.time, tz=timezone.utc).isoformat(),
        "volume":  t.volume,
    })


@app.get("/candles/{pair}/{timeframe}")
def candles(pair: str, timeframe: str = "H4", limit: int = 300):
    """
    Historical OHLCV candles from MT5.
    Returns up to `limit` completed candles (newest last).
    """
    if not connected:
        raise HTTPException(503, "MT5 bridge not connected")
    tf_code = TF_MAP.get(timeframe.upper())
    if tf_code is None:
        raise HTTPException(400, f"Unknown timeframe '{timeframe}'. Valid: {list(TF_MAP.keys())}")
    symbol = _resolve_symbol(pair)
    # Enable the symbol for market watch if not already
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, tf_code, 0, limit + 1)
    if rates is None or len(rates) == 0:
        raise HTTPException(404, f"No candle data for {symbol}/{timeframe}")
    # Drop the last (potentially incomplete) candle
    rates = rates[:-1]
    result = [
        {
            "time":   datetime.fromtimestamp(r['time'], tz=timezone.utc).isoformat(),
            "open":   float(r['open']),
            "high":   float(r['high']),
            "low":    float(r['low']),
            "close":  float(r['close']),
            "volume": int(r['tick_volume']),
        }
        for r in rates
    ]
    return _ok({
        "pair":      pair.lower(),
        "symbol":    symbol,
        "timeframe": timeframe.upper(),
        "count":     len(result),
        "candles":   result,
    })


@app.get("/multi/{pair}")
def multi_candles(pair: str):
    """
    Returns candles for all timeframes needed by the ICT engine in one call:
    D1 (HTF bias), H4 (structure/liquidity), H1 (FVG), M15 (entry timing).
    """
    if not connected:
        raise HTTPException(503, "MT5 bridge not connected")
    symbol = _resolve_symbol(pair)
    mt5.symbol_select(symbol, True)
    result = {}
    limits = {"D1": 100, "H4": 300, "H1": 200, "M15": 100}
    for tf_name, lim in limits.items():
        tf_code = TF_MAP[tf_name]
        rates = mt5.copy_rates_from_pos(symbol, tf_code, 0, lim + 1)
        if rates is not None and len(rates) > 1:
            rates = rates[:-1]  # drop incomplete candle
            result[tf_name] = [
                {
                    "time":   datetime.fromtimestamp(r['time'], tz=timezone.utc).isoformat(),
                    "open":   float(r['open']),
                    "high":   float(r['high']),
                    "low":    float(r['low']),
                    "close":  float(r['close']),
                    "volume": int(r['tick_volume']),
                }
                for r in rates
            ]
        else:
            result[tf_name] = []
    return _ok({"pair": pair.lower(), "symbol": symbol, "timeframes": result})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting MT5 Bridge on port %d", BRIDGE_PORT)
    log.info("Docker backend should connect via: http://host.docker.internal:%d", BRIDGE_PORT)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT, log_level="warning")
