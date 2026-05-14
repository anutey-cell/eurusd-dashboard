# -*- coding: utf-8 -*-
"""
MyFXBook Community Sentiment Provider
======================================
Fetches live community outlook data (long/short ratios) from MyFXBook's
official API. Used as a supplementary sentiment factor in the signal engine.

API documentation: https://www.myfxbook.com/api
Endpoints used:
  POST /api/login.json            → obtain session token
  GET  /api/get-community-outlook.json  → community long/short percentages

Session tokens are cached until a 401/403 response triggers re-login.

Environment variables:
  MYFXBOOK_ENABLED   true | false   (default: false)
  MYFXBOOK_EMAIL     account email
  MYFXBOOK_PASSWORD  account password

Sentiment interpretation:
  long% > 65  → strongly bullish crowd (contrarian: often marks tops)
  long% > 55  → mild bullish crowd
  long% 45-55 → neutral / mixed
  long% < 45  → mild bearish crowd
  long% < 35  → strongly bearish crowd (contrarian: often marks bottoms)

The signal engine uses sentiment as a CONFIRMATION factor:
  Signal direction aligns with minority sentiment → ICT contrarian edge
  Signal direction aligns with majority sentiment → reduces edge score

Score contribution (added to model output, NOT to the 80-pt gate):
  Perfect contrarian alignment: +10 sentiment bonus
  Neutral:                        0
  Crowd alignment:               -5 (reduces effective quality score)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MYFXBOOK_ENABLED  = os.getenv("MYFXBOOK_ENABLED",  "false").lower() in ("1", "true", "yes")
MYFXBOOK_EMAIL    = os.getenv("MYFXBOOK_EMAIL",    "")
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD", "")

BASE_URL        = "https://www.myfxbook.com/api"
REQUEST_TIMEOUT = 8          # seconds
SESSION_TTL     = 3600 * 6   # re-authenticate every 6 hours
SENTIMENT_TTL   = 900        # cache sentiment for 15 min


# ── Internal state ────────────────────────────────────────────────────────────

_session_token:   str   = ""
_session_created: float = 0.0
_sentiment_cache: dict  = {}   # {pair_code: SentimentData}
_cache_fetched:   float = 0.0


@dataclass
class SentimentData:
    pair:            str
    long_pct:        float    # 0–100 percentage of traders long
    short_pct:       float    # 0–100 percentage of traders short
    long_volume:     float    # open long volume (lots)
    short_volume:    float    # open short volume (lots)
    score:           int      # -5 / 0 / +10 — signal engine contribution
    signal_bias:     str      # "CONTRARIAN_BUY" | "CONTRARIAN_SELL" | "NEUTRAL" | "CROWD_BUY" | "CROWD_SELL"
    interpretation:  str      # human-readable summary
    fetched_at:      str      # ISO timestamp


# ── Session management ────────────────────────────────────────────────────────

def _login() -> str | None:
    """Authenticate and return a session token. Returns None on failure."""
    global _session_token, _session_created

    if not MYFXBOOK_ENABLED:
        return None
    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        log.warning("[myfxbook] MYFXBOOK_EMAIL or MYFXBOOK_PASSWORD not set")
        return None

    try:
        resp = requests.get(
            f"{BASE_URL}/login.json",
            params={"email": MYFXBOOK_EMAIL, "password": MYFXBOOK_PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data.get("error") is False and data.get("session"):
            _session_token   = data["session"]
            _session_created = time.time()
            log.info("[myfxbook] Authenticated successfully as %s", MYFXBOOK_EMAIL)
            return _session_token
        else:
            msg = data.get("message", "Unknown error")
            log.warning("[myfxbook] Login failed: %s", msg)
            return None
    except Exception as exc:
        log.warning("[myfxbook] Login error: %s", exc)
        return None


def _get_session() -> str | None:
    """Return a valid session token, re-authenticating if expired."""
    if not MYFXBOOK_ENABLED:
        return None
    now = time.time()
    if _session_token and (now - _session_created) < SESSION_TTL:
        return _session_token
    return _login()


# ── Sentiment fetch ────────────────────────────────────────────────────────────

def _fetch_all_sentiment() -> dict[str, dict] | None:
    """Fetch community outlook for all pairs and return raw dict keyed by symbol name."""
    session = _get_session()
    if session is None:
        return None

    try:
        resp = requests.get(
            f"{BASE_URL}/get-community-outlook.json",
            params={"session": session},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()

        if data.get("error") is True:
            msg = data.get("message", "Unknown")
            log.warning("[myfxbook] Sentiment fetch failed: %s", msg)
            if "session" in msg.lower():
                # Session expired — force re-login on next call
                global _session_token
                _session_token = ""
            return None

        symbols = data.get("symbols", [])
        result: dict[str, dict] = {}
        for sym in symbols:
            name = sym.get("name", "").upper().replace("/", "")
            result[name] = sym
        return result

    except Exception as exc:
        log.warning("[myfxbook] Sentiment fetch error: %s", exc)
        return None


def _interpret(pair: str, long_pct: float, short_pct: float) -> tuple[int, str, str]:
    """
    Return (score, signal_bias, interpretation) from crowd positioning.
    ICT + smart-money logic: retail crowd is usually wrong at extremes.
    """
    if long_pct > 65:
        # Strongly long crowd → contrarian SELL
        score = 10 if True else 0    # see below for direction-aware logic
        return (10, "CONTRARIAN_SELL",
                f"Crowd {long_pct:.0f}% long — contrarian pressure favours SELL")
    if long_pct > 55:
        return (5, "CONTRARIAN_SELL",
                f"Crowd mildly long ({long_pct:.0f}%) — slight bearish edge")
    if long_pct < 35:
        # Strongly short crowd → contrarian BUY
        return (10, "CONTRARIAN_BUY",
                f"Crowd {short_pct:.0f}% short — contrarian pressure favours BUY")
    if long_pct < 45:
        return (5, "CONTRARIAN_BUY",
                f"Crowd mildly short ({short_pct:.0f}%) — slight bullish edge")

    return (0, "NEUTRAL",
            f"Crowd split {long_pct:.0f}% long / {short_pct:.0f}% short — no strong edge")


def get_sentiment(pair: str) -> SentimentData | None:
    """
    Return community sentiment for the given pair code (e.g. 'eurusd', 'xauusd').
    Returns None when MyFXBook is disabled or unavailable.
    """
    if not MYFXBOOK_ENABLED:
        return None

    pair_code = pair.lower().replace("/", "")
    now = time.time()

    # Return cached if fresh
    if pair_code in _sentiment_cache and (now - _cache_fetched) < SENTIMENT_TTL:
        return _sentiment_cache[pair_code]

    # Fetch all at once (one API call covers all pairs)
    all_data = _fetch_all_sentiment()
    if all_data is None:
        return _sentiment_cache.get(pair_code)  # return stale if available

    # Build symbol lookup key variants
    lookup_keys = [
        pair_code.upper(),
        pair_code.upper()[:3] + "/" + pair_code.upper()[3:],
        pair_code.upper()[:6],
    ]

    raw = None
    for k in lookup_keys:
        if k in all_data:
            raw = all_data[k]
            break

    # Populate cache for all pairs returned
    global _cache_fetched
    _cache_fetched = now

    for symbol, sym_data in all_data.items():
        sym_pair = symbol.lower().replace("/", "")
        long_pct  = float(sym_data.get("longPercentage",  sym_data.get("long_percentage",  50)))
        short_pct = float(sym_data.get("shortPercentage", sym_data.get("short_percentage", 50)))
        long_vol  = float(sym_data.get("longVolume",      sym_data.get("long_volume",  0)))
        short_vol = float(sym_data.get("shortVolume",     sym_data.get("short_volume", 0)))
        score, bias, interp = _interpret(sym_pair, long_pct, short_pct)
        _sentiment_cache[sym_pair] = SentimentData(
            pair=sym_pair,
            long_pct=round(long_pct, 1),
            short_pct=round(short_pct, 1),
            long_volume=round(long_vol, 2),
            short_volume=round(short_vol, 2),
            score=score,
            signal_bias=bias,
            interpretation=interp,
            fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    return _sentiment_cache.get(pair_code)


def get_sentiment_for_signal(pair: str, signal_direction: str) -> dict:
    """
    Return a sentiment dict formatted for inclusion in the signal engine model output.
    Adjusts score based on whether sentiment confirms or contradicts the signal direction.

    signal_direction: "BUY" | "SELL" | "WAIT"
    """
    sentiment = get_sentiment(pair)

    if sentiment is None:
        return {
            "available":     False,
            "long_pct":      None,
            "short_pct":     None,
            "signal_bias":   "UNAVAILABLE",
            "interpretation": "MyFXBook sentiment unavailable",
            "adjusted_score": 0,
            "confirms_signal": None,
        }

    # Determine if sentiment confirms or contradicts the signal
    confirms = None
    adjusted_score = sentiment.score

    if signal_direction in ("BUY", "SELL"):
        # Smart money contrarian: if crowd is mostly long → sentiment edge is SELL
        # If our signal matches the contrarian edge → confirming
        if signal_direction == "SELL" and sentiment.signal_bias in ("CONTRARIAN_SELL",):
            confirms = True
            adjusted_score = sentiment.score          # full contrarian bonus
        elif signal_direction == "BUY" and sentiment.signal_bias in ("CONTRARIAN_BUY",):
            confirms = True
            adjusted_score = sentiment.score
        elif signal_direction == "BUY" and "SELL" in sentiment.signal_bias:
            confirms = False
            adjusted_score = -5                        # crowd and our signal oppose
        elif signal_direction == "SELL" and "BUY" in sentiment.signal_bias:
            confirms = False
            adjusted_score = -5
        else:
            confirms = None                            # neutral — no bonus or penalty
            adjusted_score = 0

    return {
        "available":      True,
        "long_pct":       sentiment.long_pct,
        "short_pct":      sentiment.short_pct,
        "long_volume":    sentiment.long_volume,
        "short_volume":   sentiment.short_volume,
        "signal_bias":    sentiment.signal_bias,
        "interpretation": sentiment.interpretation,
        "adjusted_score": adjusted_score,
        "confirms_signal": confirms,
        "fetched_at":     sentiment.fetched_at,
    }


def myfxbook_status() -> dict:
    """Status dict for health / engine endpoints."""
    return {
        "enabled":        MYFXBOOK_ENABLED,
        "authenticated":  bool(_session_token),
        "email":          MYFXBOOK_EMAIL or "(not set)",
        "cached_pairs":   list(_sentiment_cache.keys()),
        "cache_age_s":    round(time.time() - _cache_fetched) if _cache_fetched else None,
    }
