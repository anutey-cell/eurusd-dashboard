"""
Killzone Edge Analyzer
======================

Studies XAU/USD price action AND paper-observation outcomes across the canonical
ICT killzones so the trader can:

  - See which killzone is paying right now (edge ranking)
  - Get a recommended posture per killzone (PRESS / TRADE / OBSERVE / AVOID)
  - Compare directional bias per session (BUY vs SELL win-rate)
  - See a 24-hour hour-by-hour edge heatmap
  - Spot the best setup_type for each killzone

Two data sources, blended:
  1. PaperObservation rows  → expectancy, win rate, BUY/SELL split, best setup
  2. M15 OHLCV candles      → range, body, momentum (body/range), breakout/reversal mix

Canonical killzones (UTC):
  asian_early   22:00-00:00   Tokyo open hangover (low-vol)
  asian         00:00-06:00   Tokyo + Sydney range
  london_pre    06:00-07:00   Pre-London prep (last hour Asian)
  london_kz     07:00-10:00   ICT London kill zone - high probability
  overlap       10:00-13:00   London/NY pre-overlap (continuation)
  ny_kz         13:00-16:00   ICT NY kill zone (includes 13:00-15:30 sweet spot)
  ny_pm         16:00-22:00   NY afternoon - fading liquidity

Public API:
  - get_killzone_for(at: datetime) -> KZ key
  - analyze_killzones(db, lookback_days=60) -> dict (full edge report)
  - get_current_recommendation(db) -> dict (now + posture + reason)
  - get_hour_heatmap(db, lookback_days=60) -> list[24] (0-100 edge per UTC hour)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from db_models import PaperObservation

log = logging.getLogger(__name__)


# ── Canonical killzone windows (UTC hour, inclusive start, exclusive end) ────
# Order matters for `get_killzone_for` - windows are scanned top-to-bottom.
KILLZONES: list[dict] = [
    {"key": "asian_early", "label": "Asian Early",   "start": 22, "end": 24,
     "icon": "moon",   "priority": 1,
     "blurb": "Tokyo open hangover. Thin liquidity, mostly chop."},
    {"key": "asian",       "label": "Asian Range",   "start": 0,  "end": 6,
     "icon": "moon",   "priority": 2,
     "blurb": "Tokyo + Sydney range - define HOD/LOD for London raid."},
    {"key": "london_pre",  "label": "Pre-London",    "start": 6,  "end": 7,
     "icon": "sunrise","priority": 3,
     "blurb": "Final hour of Asian range. Watch for early manipulation wicks."},
    {"key": "london_kz",   "label": "London KZ",     "start": 7,  "end": 10,
     "icon": "sun",    "priority": 5,
     "blurb": "Highest-probability ICT kill zone. Asian range raid + reversal."},
    {"key": "overlap",     "label": "Pre-Overlap",   "start": 10, "end": 13,
     "icon": "layers", "priority": 4,
     "blurb": "London mid-session into NY pre-market. Continuation moves."},
    {"key": "ny_kz",       "label": "New York KZ",   "start": 13, "end": 16,
     "icon": "sun",    "priority": 5,
     "blurb": "NY kill zone (13:00-15:30 sweet spot). NY open raid + reversal."},
    {"key": "ny_pm",       "label": "NY Afternoon",  "start": 16, "end": 22,
     "icon": "sunset", "priority": 2,
     "blurb": "Fading liquidity, drift toward daily close. Avoid new entries."},
]

KZ_BY_KEY: dict[str, dict] = {k["key"]: k for k in KILLZONES}


# ── Mapping free-text PaperObservation.session → canonical KZ key ─────────────
_SESSION_TEXT_MAP = {
    # exact strings produced by detect_session()
    "asian session":              "asian",
    "london session":             "london_pre",       # 7-8 UTC pre-KZ
    "london kill zone":           "london_kz",
    "london/new york overlap":    "overlap",
    "new york kill zone":         "ny_kz",
    "new york session":           "ny_pm",
    "off-session":                "asian_early",
}


def get_killzone_for(at: datetime) -> str:
    """Return canonical killzone key for a given UTC timestamp."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    h = at.astimezone(timezone.utc).hour
    for kz in KILLZONES:
        s, e = kz["start"], kz["end"]
        if s <= h < e:
            return kz["key"]
    # 22-24 wraps around - caught by asian_early definition; if anything slips:
    return "asian_early"


def _classify_observation(obs: PaperObservation) -> str:
    """
    Pick a canonical KZ for a paper observation. Prefers the timestamp
    (more reliable than free-text session field), but falls back to the
    text map if observed_at is missing.
    """
    if obs.observed_at:
        return get_killzone_for(obs.observed_at)
    text = (obs.session or "").strip().lower()
    return _SESSION_TEXT_MAP.get(text, "asian_early")


# ── Aggregation primitives ────────────────────────────────────────────────────

@dataclass
class KZObservationStats:
    count:        int   = 0
    wins:         int   = 0
    losses:       int   = 0
    expired:      int   = 0
    pending:      int   = 0
    buys:         int   = 0
    sells:        int   = 0
    buy_wins:     int   = 0
    sell_wins:    int   = 0
    r_sum:        float = 0.0
    score_sum:    int   = 0
    setup_counts: dict[str, int] = field(default_factory=dict)
    setup_wins:   dict[str, int] = field(default_factory=dict)

    @property
    def resolved(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return round(100 * self.wins / self.resolved, 1) if self.resolved else 0.0

    @property
    def expectancy_r(self) -> float:
        return round(self.r_sum / self.resolved, 2) if self.resolved else 0.0

    @property
    def avg_score(self) -> float:
        return round(self.score_sum / self.count, 1) if self.count else 0.0

    @property
    def buy_win_rate(self) -> float:
        return round(100 * self.buy_wins / self.buys, 1) if self.buys else 0.0

    @property
    def sell_win_rate(self) -> float:
        return round(100 * self.sell_wins / self.sells, 1) if self.sells else 0.0

    @property
    def directional_bias(self) -> str:
        """Which side won more reliably in this KZ?"""
        if self.resolved < 4:
            return "INSUFFICIENT"
        if self.buys >= 2 and self.sells >= 2:
            d = self.buy_win_rate - self.sell_win_rate
            if abs(d) < 10:
                return "NEUTRAL"
            return "BUY_FAVORED" if d > 0 else "SELL_FAVORED"
        return "BUY_ONLY" if self.buys > self.sells else "SELL_ONLY"

    @property
    def best_setup(self) -> Optional[str]:
        """The setup_type with the highest win count in this KZ."""
        if not self.setup_wins:
            return None
        return max(self.setup_wins.items(), key=lambda kv: kv[1])[0]


@dataclass
class KZPriceStats:
    candles:        int   = 0
    avg_range_pts:  float = 0.0    # avg (high-low)
    avg_body_pts:   float = 0.0    # avg |close-open|
    pct_up_candles: float = 0.0
    momentum:       float = 0.0    # body/range - 1.0 = pure trend, 0 = pure wick
    expansion:      float = 0.0    # this-KZ avg_range / overall-avg_range
    breakouts:      int   = 0      # # candles that took out prior KZ window high
    reversals:      int   = 0      # # candles that wicked beyond but closed back


# ── Observation aggregation ───────────────────────────────────────────────────

def _aggregate_observations(
    db: Session,
    lookback_days: int,
    engine_id: Optional[str] = None,
) -> dict[str, KZObservationStats]:
    """Bucket every PaperObservation in the window into its KZ."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    q = db.query(PaperObservation).filter(PaperObservation.observed_at >= since)
    if engine_id:
        q = q.filter(PaperObservation.engine_id == engine_id)

    buckets: dict[str, KZObservationStats] = {kz["key"]: KZObservationStats() for kz in KILLZONES}

    for obs in q.all():
        kz_key = _classify_observation(obs)
        s = buckets[kz_key]
        s.count += 1
        if obs.score: s.score_sum += int(obs.score)

        # Directional split
        if obs.signal == "BUY":
            s.buys += 1
        elif obs.signal == "SELL":
            s.sells += 1

        # Setup counts
        setup = obs.setup_type or "unknown"
        s.setup_counts[setup] = s.setup_counts.get(setup, 0) + 1

        # Result split + R sum
        if obs.result == "WIN":
            s.wins += 1
            if obs.r_multiple is not None: s.r_sum += float(obs.r_multiple)
            s.setup_wins[setup] = s.setup_wins.get(setup, 0) + 1
            if obs.signal == "BUY":  s.buy_wins  += 1
            if obs.signal == "SELL": s.sell_wins += 1
        elif obs.result == "LOSS":
            s.losses += 1
            if obs.r_multiple is not None: s.r_sum += float(obs.r_multiple)
        elif obs.result == "EXPIRED":
            s.expired += 1
        else:
            s.pending += 1

    return buckets


# ── Price-action aggregation from M15 candles ─────────────────────────────────

def _aggregate_price_action(lookback_days: int) -> dict[str, KZPriceStats]:
    """
    Pull M15 candles and bucket them by killzone, computing range/body/momentum.
    96 M15 bars per day x lookback_days, capped by candle endpoint at 5000.
    """
    from data.candles import get_candles

    bars_needed = min(96 * lookback_days, 5000)
    try:
        resp = get_candles(interval="M15", limit=bars_needed, pair="xauusd")
        candles = resp.candles
    except Exception as e:
        log.warning("[killzone] price-action fetch failed: %s", e)
        return {kz["key"]: KZPriceStats() for kz in KILLZONES}

    if not candles:
        return {kz["key"]: KZPriceStats() for kz in KILLZONES}

    buckets: dict[str, list[dict]] = {kz["key"]: [] for kz in KILLZONES}

    # First pass: bucket + collect ranges/bodies
    for c in candles:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        key = get_killzone_for(ct)
        buckets[key].append({
            "h": float(c.high),  "l": float(c.low),
            "o": float(c.open),  "c": float(c.close),
        })

    # Pass 2: compute window-level extremes for breakout/reversal classification
    # Group candles into "killzone windows" (one window per (day, kz_key))
    kz_windows: dict[tuple[str, str], dict] = {}
    for c in candles:
        ct = c.time if c.time.tzinfo else c.time.replace(tzinfo=timezone.utc)
        key = get_killzone_for(ct)
        day = ct.strftime("%Y-%m-%d")
        w = kz_windows.setdefault((day, key), {"high": -1e9, "low": 1e9, "bars": []})
        w["high"] = max(w["high"], float(c.high))
        w["low"]  = min(w["low"],  float(c.low))
        w["bars"].append((ct, float(c.high), float(c.low), float(c.close)))

    # Overall average range across ALL killzones for expansion ratio
    all_ranges = [b["h"] - b["l"] for kz_bars in buckets.values() for b in kz_bars]
    overall_avg_range = (sum(all_ranges) / len(all_ranges)) if all_ranges else 1.0

    # Build KZ price stats
    result: dict[str, KZPriceStats] = {}
    for kz_key, bars in buckets.items():
        s = KZPriceStats()
        s.candles = len(bars)
        if not bars:
            result[kz_key] = s
            continue
        ranges  = [b["h"] - b["l"] for b in bars]
        bodies  = [abs(b["c"] - b["o"]) for b in bars]
        ups     = sum(1 for b in bars if b["c"] > b["o"])
        s.avg_range_pts  = round(sum(ranges) / len(ranges), 2)
        s.avg_body_pts   = round(sum(bodies) / len(bodies), 2)
        s.pct_up_candles = round(100 * ups / len(bars), 1)
        s.momentum       = round(s.avg_body_pts / s.avg_range_pts, 3) if s.avg_range_pts else 0.0
        s.expansion      = round(s.avg_range_pts / overall_avg_range, 2) if overall_avg_range else 1.0
        result[kz_key] = s

    # Compute breakout/reversal counts using prior-KZ extremes
    # For each window, prior window of same KZ from previous day
    kz_prior_extremes: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for (day, key), w in kz_windows.items():
        kz_prior_extremes[key].append((day, w["high"], w["low"]))
    for key in kz_prior_extremes:
        kz_prior_extremes[key].sort()  # by day ascending

    for key, day_list in kz_prior_extremes.items():
        breakouts = 0
        reversals = 0
        for i in range(1, len(day_list)):
            prev_day, prev_high, prev_low = day_list[i - 1]
            today,    today_high, today_low = day_list[i]
            today_bars = kz_windows[(today, key)]["bars"]
            took_high = any(h > prev_high for _, h, _, _ in today_bars)
            took_low  = any(l < prev_low  for _, _, l, _ in today_bars)
            closed_above = today_bars[-1][3] > prev_high if today_bars else False
            closed_below = today_bars[-1][3] < prev_low  if today_bars else False
            if took_high and closed_above: breakouts += 1
            elif took_low and closed_below: breakouts += 1
            elif (took_high and not closed_above) or (took_low and not closed_below):
                reversals += 1
        result[key].breakouts = breakouts
        result[key].reversals = reversals

    return result


# ── Edge scoring + posture ────────────────────────────────────────────────────

def _composite_edge(obs: KZObservationStats, px: KZPriceStats) -> tuple[int, dict]:
    """
    Blend paper-observation outcomes with raw price-action stats into one
    0-100 edge score. Returns (score, components_dict).

    Weights:
      - Expectancy   50%  (only when resolved >= 5; else 0)
      - Win rate     20%  (only when resolved >= 5)
      - Momentum     15%  (body/range from candles)
      - Expansion    15%  (this KZ's range vs overall avg)

    When resolved < 5 → reweight: momentum 60%, expansion 40%.
    """
    momentum_score  = max(0, min(100, int(px.momentum  * 100)))           # 0-100
    expansion_score = max(0, min(100, int((px.expansion - 0.5) * 100)))   # 0.5x→0, 1.5x→100

    if obs.resolved >= 5:
        # Map expectancy in [-1, +2] → [0, 100]
        exp_score   = max(0, min(100, int((obs.expectancy_r + 1) / 3 * 100)))
        wr_score    = max(0, min(100, int(obs.win_rate)))
        composite = int(0.50 * exp_score + 0.20 * wr_score
                        + 0.15 * momentum_score + 0.15 * expansion_score)
        comps = {
            "expectancy_score": exp_score, "winrate_score": wr_score,
            "momentum_score":   momentum_score, "expansion_score": expansion_score,
            "weights": {"expectancy": 0.50, "winrate": 0.20,
                        "momentum": 0.15, "expansion": 0.15},
        }
    else:
        composite = int(0.60 * momentum_score + 0.40 * expansion_score)
        comps = {
            "momentum_score": momentum_score, "expansion_score": expansion_score,
            "weights": {"momentum": 0.60, "expansion": 0.40},
            "note": "Not enough resolved observations (n<5); using price-action only.",
        }

    return composite, comps


def _posture_for_score(score: int) -> tuple[str, str]:
    """Map edge score to actionable posture + reason."""
    if score >= 75:
        return ("PRESS",   "High-edge zone - increase position size up to 1.5x.")
    if score >= 60:
        return ("TRADE",   "Solid edge - trade with normal risk.")
    if score >= 40:
        return ("OBSERVE", "Marginal edge - paper-trade only, no live size.")
    return ("AVOID",       "Negative or no edge - stand aside this killzone.")


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_killzones(
    db: Session,
    lookback_days: int = 60,
    engine_id: Optional[str] = None,
) -> dict:
    """
    Build the full per-killzone edge report. Returns:
      {
        lookback_days, engine_id, generated_at,
        killzones: [ {key, label, blurb, window_utc, observations:{...},
                      price_action:{...}, edge_score, posture, posture_reason,
                      components:{...}}, ... ],
        ranking:    [ kz_key sorted best→worst by edge_score ],
        best_kz:    kz_key,
        worst_kz:   kz_key,
      }
    """
    obs_buckets = _aggregate_observations(db, lookback_days, engine_id=engine_id)
    px_buckets  = _aggregate_price_action(lookback_days)

    rows: list[dict] = []
    for kz in KILLZONES:
        key = kz["key"]
        obs = obs_buckets[key]
        px  = px_buckets.get(key, KZPriceStats())
        score, comps = _composite_edge(obs, px)
        posture, reason = _posture_for_score(score)

        rows.append({
            "key":   key,
            "label": kz["label"],
            "blurb": kz["blurb"],
            "icon":  kz["icon"],
            "window_utc":     f"{kz['start']:02d}:00-{kz['end'] % 24:02d}:00",
            "start_hour_utc": kz["start"],
            "end_hour_utc":   kz["end"],
            "observations": {
                "count":            obs.count,
                "resolved":         obs.resolved,
                "wins":             obs.wins,
                "losses":           obs.losses,
                "expired":          obs.expired,
                "pending":          obs.pending,
                "win_rate":         obs.win_rate,
                "expectancy_r":     obs.expectancy_r,
                "avg_score":        obs.avg_score,
                "buy_count":        obs.buys,
                "sell_count":       obs.sells,
                "buy_win_rate":     obs.buy_win_rate,
                "sell_win_rate":    obs.sell_win_rate,
                "directional_bias": obs.directional_bias,
                "best_setup":       obs.best_setup,
                "setup_counts":     obs.setup_counts,
            },
            "price_action": {
                "candles":        px.candles,
                "avg_range_pts":  px.avg_range_pts,
                "avg_body_pts":   px.avg_body_pts,
                "pct_up_candles": px.pct_up_candles,
                "momentum":       px.momentum,
                "expansion":      px.expansion,
                "breakouts":      px.breakouts,
                "reversals":      px.reversals,
            },
            "edge_score":     score,
            "posture":        posture,
            "posture_reason": reason,
            "components":     comps,
        })

    rows_sorted = sorted(rows, key=lambda r: r["edge_score"], reverse=True)
    return {
        "lookback_days": lookback_days,
        "engine_id":     engine_id,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "killzones":     rows,
        "ranking":       [r["key"] for r in rows_sorted],
        "best_kz":       rows_sorted[0]["key"] if rows_sorted else None,
        "worst_kz":      rows_sorted[-1]["key"] if rows_sorted else None,
    }


def get_current_recommendation(db: Session, lookback_days: int = 60) -> dict:
    """
    What killzone is active now, what's our edge there, what posture
    should we take? Returns a small dict ready for the dashboard banner.
    """
    now = datetime.now(timezone.utc)
    kz_key = get_killzone_for(now)
    report = analyze_killzones(db, lookback_days=lookback_days)
    row = next((r for r in report["killzones"] if r["key"] == kz_key), None)
    minutes_left = _minutes_until_next_kz(now)

    return {
        "current_kz":       kz_key,
        "label":            row["label"] if row else "Unknown",
        "window_utc":       row["window_utc"] if row else None,
        "minutes_remaining":minutes_left,
        "edge_score":       row["edge_score"] if row else 0,
        "posture":          row["posture"] if row else "OBSERVE",
        "posture_reason":   row["posture_reason"] if row else "",
        "win_rate":         row["observations"]["win_rate"] if row else 0,
        "expectancy_r":     row["observations"]["expectancy_r"] if row else 0,
        "directional_bias": row["observations"]["directional_bias"] if row else "INSUFFICIENT",
        "best_setup":       row["observations"]["best_setup"] if row else None,
        "best_kz_overall":  report["best_kz"],
        "best_kz_score":    next((r["edge_score"] for r in report["killzones"]
                                  if r["key"] == report["best_kz"]), 0),
        "next_high_edge_kz": _next_high_edge_kz(now, report),
        "generated_at":     report["generated_at"],
    }


def get_hour_heatmap(db: Session, lookback_days: int = 60) -> list[dict]:
    """
    24-cell hour-of-day heatmap. Each cell carries:
       {hour, kz_key, kz_label, edge_score, posture}
    Frontend renders as a 24-col strip.
    """
    report = analyze_killzones(db, lookback_days=lookback_days)
    score_by_kz = {r["key"]: r["edge_score"] for r in report["killzones"]}
    posture_by_kz = {r["key"]: r["posture"] for r in report["killzones"]}
    label_by_kz = {r["key"]: r["label"] for r in report["killzones"]}
    cells: list[dict] = []
    for h in range(24):
        kz_key = get_killzone_for(datetime(2000, 1, 1, h, 30, tzinfo=timezone.utc))
        cells.append({
            "hour":       h,
            "kz_key":     kz_key,
            "kz_label":   label_by_kz.get(kz_key, kz_key),
            "edge_score": score_by_kz.get(kz_key, 0),
            "posture":    posture_by_kz.get(kz_key, "OBSERVE"),
        })
    return cells


# ── Helpers ──────────────────────────────────────────────────────────────────

def _minutes_until_next_kz(at: datetime) -> int:
    """How many minutes until the active KZ window ends."""
    h = at.astimezone(timezone.utc).hour
    m = at.astimezone(timezone.utc).minute
    for kz in KILLZONES:
        if kz["start"] <= h < kz["end"]:
            end_h = kz["end"]
            # convert to minutes from now to end_h:00
            mins = (end_h - h) * 60 - m
            return max(0, mins)
    return 0


def _next_high_edge_kz(at: datetime, report: dict) -> Optional[dict]:
    """Find the next killzone window with edge_score >= 60, looking forward 24h."""
    current_hour = at.astimezone(timezone.utc).hour
    for offset in range(1, 25):
        h = (current_hour + offset) % 24
        kz_key = get_killzone_for(datetime(2000, 1, 1, h, 30, tzinfo=timezone.utc))
        row = next((r for r in report["killzones"] if r["key"] == kz_key), None)
        if row and row["edge_score"] >= 60:
            return {
                "kz_key":      kz_key,
                "label":       row["label"],
                "window_utc":  row["window_utc"],
                "edge_score":  row["edge_score"],
                "posture":     row["posture"],
                "hours_away":  offset,
            }
    return None
