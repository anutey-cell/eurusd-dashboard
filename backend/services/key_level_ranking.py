"""
Key Level Ranking — Phase 9
============================

The brief:
"Create a key-level engine that calculates and ranks 26 level types.
 Output three tiers so the user isn't overwhelmed with unranked levels."

This module consumes the canonical snapshot (Phase 2) + optional liquidity
map + optional breakout assessments (Phase 6), computes a ranking score
per level, and emits three tiers:

  Tier 1 — immediate decision levels (up to 4)
  Tier 2 — important supporting levels (up to 6)
  Tier 3 — secondary intraday references (up to 8)

Behind `xauusd_key_level_ranking_enabled`. Read-only diagnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Score weights
# ─────────────────────────────────────────────────────────────────────────────

_TF_WEIGHT = {
    "W1":       25,
    "D1":       25,
    "H4":       18,
    "H1":       10,
    "M15":       8,
    "session":  22,       # PDH/PDL/Asian/London/NY — session anchors are
                           # more decisive than H4 pivots because every trader
                           # watches them; bump above H4.
    "structure": 10,
}

_LIQUIDITY_TAG_BOOST = {
    "PDH":         15, "PDL":         15,
    "PWH":         15, "PWL":         15,
    "PWO":         10, "DAILY_OPEN":  10,
    "ASIAN_HIGH":  10, "ASIAN_LOW":   10,
    "LONDON_HIGH":  8, "LONDON_LOW":   8,
    "NY_HIGH":      8, "NY_LOW":       8,
    "TODAY_HIGH":   8, "TODAY_LOW":    8,
    "PROTECTED_HIGH": 12, "PROTECTED_LOW": 12,
    "H4_SUPPLY":   10, "H4_DEMAND":   10,
    "H1_SWING_HI":  6, "H1_SWING_LO":  6,
    "POC":         12, "VAH":          9, "VAL":         9,
    "FVG":          6,
    "BREAKOUT":    10, "RETEST":       8,
    "INVALIDATION_BULL": 14, "INVALIDATION_BEAR": 14,
}

# Distance thresholds (in H1 ATRs)
_DIST_TIER1_MAX = 2.0
_DIST_TIER2_MAX = 4.0
_DIST_HARD_CAP  = 8.0     # anything beyond → dropped


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RankedLevel:
    price:              float
    label:              str
    tag:                str       # e.g. PDH, ASIAN_HIGH, H4_SUPPLY
    tier:               int
    score:              float
    side:               str       # ABOVE | BELOW | AT
    distance_pts:       float
    distance_atr:       float
    timeframe_source:   str       # W1/D1/H4/H1/session/structure
    reactions:          int  = 1
    swept:              bool = False
    accepted_beyond:    bool = False
    flipped_role:       bool = False
    is_liquidity_pool:  bool = True
    reasons:            list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KeyLevelRanking:
    current_price:   float
    atr_h1:          float
    tier1:           list[RankedLevel] = field(default_factory=list)
    tier2:           list[RankedLevel] = field(default_factory=list)
    tier3:           list[RankedLevel] = field(default_factory=list)
    dropped_count:   int = 0
    warnings:        list[str] = field(default_factory=list)
    generated_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "current_price": self.current_price,
            "atr_h1":        self.atr_h1,
            "tier1":         [l.to_dict() for l in self.tier1],
            "tier2":         [l.to_dict() for l in self.tier2],
            "tier3":         [l.to_dict() for l in self.tier3],
            "dropped_count": self.dropped_count,
            "warnings":      self.warnings,
            "generated_at":  self.generated_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = ((a * (n - 1)) + tr) / n
    return a


def _swing_high_low_all(bars, k=3):
    """Return list of (index, price) for all pivots (high or low).

    Requires STRICT inequality against at least one neighbour on each side —
    otherwise flat data (identical bars) reports every bar as a pivot.
    """
    highs, lows = [], []
    if len(bars) < 2 * k + 1:
        return (highs, lows)
    for i in range(k, len(bars) - k):
        h, l = bars[i].high, bars[i].low
        left_high = [bars[j].high for j in range(i - k, i)]
        right_high = [bars[j].high for j in range(i + 1, i + k + 1)]
        left_low = [bars[j].low for j in range(i - k, i)]
        right_low = [bars[j].low for j in range(i + 1, i + k + 1)]
        # Strict-inequality on both sides — flat bars never register
        if all(h > x for x in left_high) and all(h > x for x in right_high):
            highs.append((i, h))
        if all(l < x for x in left_low) and all(l < x for x in right_low):
            lows.append((i, l))
    return (highs, lows)


def _count_reactions(bars, price, tolerance):
    """Count bars whose high/low touched `price` within `tolerance`."""
    cnt = 0
    for b in bars:
        if abs(b.high - price) <= tolerance or abs(b.low - price) <= tolerance:
            cnt += 1
    return cnt


def _was_swept(bars_recent, price, side):
    """True if any recent bar wicked past the level in the sweep direction."""
    for b in bars_recent:
        if side == "ABOVE" and b.high > price:
            return True
        if side == "BELOW" and b.low < price:
            return True
    return False


def _accepted_beyond(bars_recent, price, side, min_closes=2):
    """True if ≥ min_closes recent bars closed past the level in that side."""
    if side == "ABOVE":
        return sum(1 for b in bars_recent if b.close > price) >= min_closes
    if side == "BELOW":
        return sum(1 for b in bars_recent if b.close < price) >= min_closes
    return False


def _side_of(price, current_price, atr):
    diff = price - current_price
    if abs(diff) < 0.1 * atr:
        return "AT"
    return "ABOVE" if diff > 0 else "BELOW"


def _register(level_map, price, label, tag, tf_source, **kwargs):
    """Merge a candidate level into level_map by price (dedup near duplicates)."""
    key = round(price, 2)
    if key in level_map:
        # Merge — add tag/label to reasons
        existing = level_map[key]
        if tag not in existing["tags"]:
            existing["tags"].add(tag)
            existing["labels"].append(label)
        for k, v in kwargs.items():
            if v is True:
                existing[k] = True
        # Increment TF weight if new source is heavier
        existing["reactions"] = max(existing["reactions"], kwargs.get("reactions", 1))
        return
    level_map[key] = {
        "price": price, "labels": [label], "tags": {tag}, "tf_source": tf_source,
        "reactions": kwargs.get("reactions", 1),
        "swept": kwargs.get("swept", False),
        "accepted_beyond": kwargs.get("accepted_beyond", False),
        "flipped_role": kwargs.get("flipped_role", False),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Level harvesting
# ─────────────────────────────────────────────────────────────────────────────

def _harvest_from_snapshot(snapshot) -> dict:
    """Collect every candidate level from the canonical snapshot."""
    out: dict = {}
    if snapshot is None:
        return out

    lb = snapshot.levels
    if lb:
        for name, tag in (
            ("pdh", "PDH"), ("pdl", "PDL"), ("pdc", "PDC"),
            ("pwh", "PWH"), ("pwl", "PWL"), ("pwo", "PWO"),
            ("daily_open", "DAILY_OPEN"),
            ("asian_high", "ASIAN_HIGH"), ("asian_low", "ASIAN_LOW"),
        ):
            v = getattr(lb, name, None)
            if v is not None:
                _register(out, float(v), tag, tag, "session", reactions=2)

    # Session hi/lo from SessionInfo
    if snapshot.session:
        if snapshot.session.session_high is not None:
            _register(out, float(snapshot.session.session_high),
                       f"Session high ({snapshot.session.kz_label})",
                       "TODAY_HIGH", "session")
        if snapshot.session.session_low is not None:
            _register(out, float(snapshot.session.session_low),
                       f"Session low ({snapshot.session.kz_label})",
                       "TODAY_LOW", "session")

    # H4 + H1 swing pivots — keep only the 2 most recent per side so we
    # don't drown the tier list in stale structure.
    tfs = snapshot.timeframes or {}
    for tf, tag_hi, tag_lo in (
        ("H4", "H4_SUPPLY", "H4_DEMAND"),
        ("H1", "H1_SWING_HI", "H1_SWING_LO"),
    ):
        slice_ = tfs.get(tf)
        if not slice_ or not slice_.candles:
            continue
        highs, lows = _swing_high_low_all(slice_.candles, k=3)
        for _, price in highs[-2:]:
            _register(out, float(price), f"{tf} swing high {price:.2f}",
                       tag_hi, tf, reactions=1)
        for _, price in lows[-2:]:
            _register(out, float(price), f"{tf} swing low {price:.2f}",
                       tag_lo, tf, reactions=1)

    return out


def _harvest_from_liquidity_map(lm, level_map: dict):
    """Merge in zones from services.liquidity_map.LiquidityMap if present."""
    if lm is None:
        return
    for pools_name in ("buy_side_pools", "sell_side_pools"):
        pools = getattr(lm, pools_name, []) or []
        for z in pools:
            tag = getattr(z, "zone_type", "LIQ").upper()
            price = float(getattr(z, "price", 0))
            if price <= 0:
                continue
            _register(level_map, price, f"{tag} @ {price:.2f}",
                       tag, "session",
                       reactions=int(getattr(z, "touches", 1) or 1),
                       is_liquidity_pool=True)


def _harvest_from_breakouts(breakouts, level_map: dict):
    """Merge in level+context info from breakout assessments."""
    if not breakouts:
        return
    for b in breakouts:
        price = float(b.level)
        cls = b.classification
        if cls in ("BREAKOUT_ACCEPTANCE", "BREAKOUT_CONFIRMED", "CONTINUATION"):
            _register(level_map, price, f"{b.level_name} broken → {cls.lower()}",
                       "BREAKOUT", "session", accepted_beyond=True, swept=True)
        elif cls == "BREAKOUT_RETEST":
            _register(level_map, price, f"{b.level_name} retest",
                       "RETEST", "session", flipped_role=True, swept=True)
        elif cls == "FAILED_BREAKOUT":
            _register(level_map, price, f"{b.level_name} failed breakout",
                       "BREAKOUT", "session", swept=True)
        elif cls == "LIQUIDITY_PROBE":
            _register(level_map, price, f"{b.level_name} liquidity probe",
                       "BREAKOUT", "session", swept=True)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_level(cand: dict, current_price: float, atr: float,
                   confluence_count: int) -> tuple[float, list[str]]:
    """Compute a 0-100 ranking score and human-readable reasons."""
    reasons: list[str] = []
    score = 0.0

    tf = cand["tf_source"]
    score += _TF_WEIGHT.get(tf, 5)
    reasons.append(f"TF={tf} (+{_TF_WEIGHT.get(tf, 5)})")

    # Tag boosts: heaviest tag full weight, additional tags at 50% for confluence
    tag_boosts = sorted((_LIQUIDITY_TAG_BOOST.get(t, 0) for t in cand["tags"]),
                          reverse=True)
    if tag_boosts:
        tag_score = tag_boosts[0] + sum(tb * 0.5 for tb in tag_boosts[1:])
        score += tag_score
        reasons.append(f"tags={','.join(sorted(cand['tags']))} (+{tag_score:.0f})")

    # Reactions
    reactions = cand.get("reactions", 1)
    if reactions >= 3:
        score += 8
        reasons.append(f"{reactions} reactions (+8)")
    elif reactions >= 2:
        score += 4
        reasons.append(f"{reactions} reactions (+4)")

    # Swept / accepted / flipped
    if cand.get("swept"):
        if cand.get("accepted_beyond"):
            score += 8
            reasons.append("swept + accepted beyond (+8)")
        else:
            score -= 6
            reasons.append("swept, no acceptance (-6)")
    if cand.get("flipped_role"):
        score += 12
        reasons.append("flipped role (+12)")
    if cand.get("accepted_beyond") and not cand.get("swept"):
        score += 6
        reasons.append("acceptance beyond (+6)")

    # Distance — favour near-price levels
    distance_pts = abs(cand["price"] - current_price)
    distance_atr = distance_pts / max(atr, 0.1)
    if distance_atr <= 1.0:
        score += 15
        reasons.append(f"dist {distance_atr:.2f} ATR (+15)")
    elif distance_atr <= 2.0:
        score += 10
        reasons.append(f"dist {distance_atr:.2f} ATR (+10)")
    elif distance_atr <= 3.0:
        score += 5
        reasons.append(f"dist {distance_atr:.2f} ATR (+5)")
    elif distance_atr <= 5.0:
        pass
    else:
        score -= 5 * (distance_atr - 5)
        reasons.append(f"dist {distance_atr:.2f} ATR (penalty)")

    # Confluence bonus
    if confluence_count >= 2:
        score += 8 * (confluence_count - 1)
        reasons.append(f"confluence with {confluence_count-1} other tags (+{8*(confluence_count-1)})")

    # Clamp
    score = max(0.0, min(100.0, score))
    return (score, reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def rank_key_levels(snapshot, *, liquidity_map=None,
                     breakouts=None) -> KeyLevelRanking:
    """
    Fails open — always returns a KeyLevelRanking. Empty tiers when
    inputs are missing.
    """
    warnings: list[str] = []
    if snapshot is None:
        return KeyLevelRanking(current_price=0.0, atr_h1=0.0,
                                warnings=["snapshot is None"])

    tfs = snapshot.timeframes or {}
    h1 = tfs.get("H1", None).candles if tfs.get("H1") else []
    m15 = tfs.get("M15", None).candles if tfs.get("M15") else []

    if not m15:
        return KeyLevelRanking(current_price=0.0, atr_h1=0.0,
                                warnings=["no M15 bars"])

    current_price = m15[-1].close
    atr = _atr(h1, 14) or 5.0    # fallback so scoring doesn't div/0

    # Gather candidates
    level_map = _harvest_from_snapshot(snapshot)
    _harvest_from_liquidity_map(liquidity_map, level_map)
    _harvest_from_breakouts(breakouts, level_map)

    if not level_map:
        return KeyLevelRanking(current_price=current_price, atr_h1=atr,
                                warnings=["no candidate levels harvested"])

    # For each candidate: compute reactions from recent M15 (last 100 bars)
    tolerance = 0.1 * atr
    recent_m15 = m15[-100:] if len(m15) > 100 else m15
    for key, cand in level_map.items():
        touch_count = _count_reactions(recent_m15, cand["price"], tolerance)
        cand["reactions"] = max(cand["reactions"], touch_count)

    # Score every candidate
    scored: list[RankedLevel] = []
    dropped_hard_cap = 0
    for key, cand in level_map.items():
        # Confluence: how many other levels within tolerance?
        confluence_count = sum(1 for other_key in level_map
                                  if other_key != key
                                  and abs(other_key - key) <= tolerance) + 1
        score, reasons = _score_level(cand, current_price, atr, confluence_count)
        distance_pts = abs(cand["price"] - current_price)
        distance_atr = distance_pts / max(atr, 0.1)

        # Hard drop if way too far
        if distance_atr > _DIST_HARD_CAP:
            dropped_hard_cap += 1
            continue

        # Compose primary label — use PDH/PDL/POC etc first, then fallback to first label
        primary_tag = None
        for pri in ("PDH", "PDL", "PWH", "PWL", "DAILY_OPEN",
                     "ASIAN_HIGH", "ASIAN_LOW", "POC", "VAH", "VAL",
                     "BREAKOUT", "RETEST",
                     "PROTECTED_HIGH", "PROTECTED_LOW",
                     "H4_SUPPLY", "H4_DEMAND",
                     "H1_SWING_HI", "H1_SWING_LO"):
            if pri in cand["tags"]:
                primary_tag = pri
                break
        primary_tag = primary_tag or next(iter(cand["tags"]))

        # Build a clean label
        display = primary_tag.replace("_", " ").title()
        if len(cand["tags"]) > 1:
            display += f" (+{len(cand['tags'])-1} confluence)"

        scored.append(RankedLevel(
            price=cand["price"], label=display, tag=primary_tag,
            tier=3, score=round(score, 2),
            side=_side_of(cand["price"], current_price, atr),
            distance_pts=round(distance_pts, 2),
            distance_atr=round(distance_atr, 2),
            timeframe_source=cand["tf_source"],
            reactions=cand["reactions"],
            swept=cand.get("swept", False),
            accepted_beyond=cand.get("accepted_beyond", False),
            flipped_role=cand.get("flipped_role", False),
            is_liquidity_pool=cand.get("is_liquidity_pool", True),
            reasons=reasons,
        ))

    # Rank descending by score, then by distance ascending
    scored.sort(key=lambda l: (-l.score, l.distance_atr))

    # Tier assignment: prefer near AND high-scoring for tier 1
    tier1: list[RankedLevel] = []
    tier2: list[RankedLevel] = []
    tier3: list[RankedLevel] = []
    for lv in scored:
        if len(tier1) < 4 and lv.distance_atr <= _DIST_TIER1_MAX and lv.score >= 40:
            lv.tier = 1
            tier1.append(lv)
        elif len(tier2) < 6 and lv.distance_atr <= _DIST_TIER2_MAX and lv.score >= 25:
            lv.tier = 2
            tier2.append(lv)
        elif len(tier3) < 8:
            lv.tier = 3
            tier3.append(lv)

    dropped_from_tiers = len(scored) - (len(tier1) + len(tier2) + len(tier3))
    total_dropped = dropped_from_tiers + dropped_hard_cap

    return KeyLevelRanking(
        current_price=current_price, atr_h1=round(atr, 2),
        tier1=tier1, tier2=tier2, tier3=tier3, dropped_count=total_dropped,
        warnings=warnings,
    )


__all__ = ["rank_key_levels", "KeyLevelRanking", "RankedLevel"]
