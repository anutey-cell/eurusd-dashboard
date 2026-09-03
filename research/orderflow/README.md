# Track A — Quote-Level Microstructure Research

**Author's note:** This directory holds RESEARCH SPECIFICATIONS and
NOTES ONLY. Nothing here is imported by any production module. Nothing
here computes a live signal. Production strategies (STRATEGIST,
PREDATOR, VP Trap) remain untouched.

Owning goal: characterise the Track A dataset (`mt5_ticks`) and
maintain the discipline needed to test whether *quote-level* behaviour
carries information the candle-based engines currently discard —
**without ever falsely labelling it as true order flow**.

---

## 1. Terminology discipline (hard rule)

Exness spot XAU/USD publishes bid + ask quote updates. No trade prints,
no size, no aggressor tags. Empirically verified 2026-09-03 over
9,015 real ticks — LAST / VOL / BUY / SELL flags 0.0% populated, `last`
and `volume_real` 0.0% populated.

The dataset is therefore:

> **QUOTE-LEVEL MICROSTRUCTURE**

Never call it, or any feature derived from it:

- "true order flow"
- "true delta"
- "CVD"
- "aggressor pressure"
- "footprint imbalance"
- "trade absorption"
- "traded volume"

Acceptable labels for derived features:

- Quote-change delta (bid-move count − ask-move count)
- Mid-price velocity
- Spread expansion
- Quote-intensity burst
- Bearish/bullish **quote-momentum** exhaustion
- Two-way quote activity
- Directional quote-persistence

These describe what we can actually measure and imply nothing about
exchange trade activity.

---

## 2. Schema — what we store

| Column | Type | Purpose |
|---|---|---|
| `id` | INTEGER PK | ingest sequence |
| `ingest_at` | TIMESTAMPTZ | server-side receipt time |
| `tick_time_msc` | BIGINT | broker-supplied ms since Unix epoch, authoritative |
| `tick_time_utc` | TIMESTAMPTZ | derived from tick_time_msc, query convenience |
| `symbol` | VARCHAR | broker's discovered symbol |
| `bid` | FLOAT (double) | raw broker bid |
| `ask` | FLOAT (double) | raw broker ask |
| `last` | FLOAT (nullable) | raw broker last-trade; NULL when not published |
| `volume_real` | FLOAT (nullable) | raw broker traded volume; NULL when not published |
| `flags` | INTEGER | raw MT5 flag bitfield preserved |
| `content_hash` | VARCHAR(24) | sha1(time_msc \| bid \| ask \| last \| volume_real \| flags) [:24] |
| `broker`, `account`, `daemon_id` | VARCHAR | provenance |

Uniqueness: `UNIQUE (symbol, content_hash)`. Two same-ms events with
different bid/ask/flags land separately; byte-identical replays are
rejected.

### Reconstructability audit (2026-09-03, 23 k live ticks)

Verified:
- Full bid + ask sequence is reconstructable by ordering on `tick_time_msc`
  (breaking ties on `id`).
- `spread(t)` deterministic as `ask(t) - bid(t)`; not stored, since:
  * `spread` is derived, not source
  * float subtraction introduces representation artifacts (e.g.
    `0.182000000000699` vs `0.181999999999789`), so any stored `spread`
    would be a rounded copy that adds no information
  * All research code MUST round derived spreads to `3` decimals or
    quantise to a documented tick size (`0.001`) at compute time
- `bid_transition(t)` = `bid(t) - bid(t-1)`; likewise `ask_transition(t)`.
  Both computable at query time from raw fields.
- Raw wire values preserved: sampled 200 recent bids exhibit max 3
  fractional digits, matching Exness's advertised 3-decimal quote
  precision. FLOAT (double) is comfortably wider than that.
- Ingest order preserved: 0 out-of-order (by `id` vs `tick_time_msc`)
  across 9k+ ticks.
- Same-ms preservation: 16 groups / 33 events / 0 identical-content
  collisions. All distinct-content preserved by design.

**Verdict: schema is sufficient. No ingestion change needed.**

---

## 3. Look-ahead bias convention (hard rule)

Any feature evaluated at timestamp `T` must be computable using only
data satisfying `tick_time_msc <= T`. Never a completed future candle,
never a subsequently-known session high/low, never a swing point
confirmed after `T`.

Outcome labels are exempt. A binary "did price rally +X pts within N
minutes after T?" label may use future ticks — that is the correct
place for future information to live.

Enforcement pattern (documentary only for now):

```
research query:
    features(T)   → SELECT ... FROM mt5_ticks WHERE tick_time_msc <= T
    label(T)      → SELECT ... FROM mt5_ticks WHERE tick_time_msc > T
                                                AND tick_time_msc <= T + horizon
```

Reviewers of any future research script should reject a PR whose
feature query lacks the `<= T` clause.

---

## 4. Session labelling

Standardised bucket labels for research queries. All ranges are
UTC. XAU spot trades 24/5 but liquidity clusters as follows.

| Label | Definition (UTC) | Note |
|---|---|---|
| `ASIA` | 22:00 (prev day) – 06:00 | Tokyo + Sydney; typically thin |
| `LONDON` | 06:00 – 12:00 | London open + morning session |
| `LONDON_NY_OVERLAP` | 12:00 – 16:00 | Peak liquidity |
| `NY_PM` | 16:00 – 22:00 | NY afternoon; wind-down toward Asian re-open |

Weekend rollover 21:00 Friday UTC → 22:00 Sunday UTC — no data expected.

---

## 5. NFP event windows (Friday 2026-09-04)

NFP scheduled release: **12:30 UTC** (08:30 ET, EDT active in September).

| Label | Range (UTC) | Purpose |
|---|---|---|
| `PRE_NFP_60` | 11:30 – 12:29:59 | 60-minute preparation window |
| `PRE_NFP_15` | 12:15 – 12:29:59 | Final-15-minute quiet zone |
| `NFP_0_1` | 12:30:00 – 12:30:59 | Release second-to-minute reaction |
| `NFP_1_5` | 12:31 – 12:34:59 | Immediate follow-through |
| `NFP_5_15` | 12:35 – 12:44:59 | Continuation vs mean-reversion |
| `NFP_15_60` | 12:45 – 13:29:59 | Post-shock exploration |
| `POST_NFP` | 13:30 – 22:00 | Return to normal-regime dynamics |

Research queries should attach these labels via a small helper that
takes `tick_time_utc` and returns the label. Written once, reused.

---

## 6. Candidate feature families (specification only — DO NOT COMPUTE)

Every feature below is deferred until sufficient data + at least one
formal check-in report has been produced. Listed for taxonomy only.

### A. Quote intensity

- `ticks_per_1s`, `ticks_per_5s`, `ticks_per_30s`
- `tick_rate_acceleration` = intensity(now) − rolling_median(intensity, prior_window)
- `burst_flag` = 1 if intensity > percentile_95 of prior window

### B. Directional quote movement

- `bid_upticks_5s`, `bid_downticks_5s`, `ask_upticks_5s`, `ask_downticks_5s`
- `mid_change_count_5s` (mid = (bid + ask) / 2, count of ticks where mid moves)
- `directional_persistence` = |Σ(sign(mid_change))| / count

### C. Quote imbalance proxies (NOT executed-volume imbalance)

- `bid_only_updates_5s` = ticks where bid changed but ask did not
- `ask_only_updates_5s` = ticks where ask changed but bid did not
- `both_updated_5s` (co-move counter)
- `directional_asymmetry_5s` = (bid_only - ask_only) / (bid_only + ask_only + 1)

**Warning:** these count quote-side updates, not executed volume. A
`bid_only_up` count is NOT "buyers lifting". It's "the broker adjusted
the bid without adjusting the ask". Interpretation must remain
quote-side.

### D. Spread behaviour

- `spread_now`, `spread_5s_median`, `spread_5s_p90`
- `spread_expansion` = spread_now / rolling_median − 1
- `spread_instability` = std(spread_5s) / mean(spread_5s)

### E. Micro-displacement

- `mid_velocity_5s` = (mid(T) − mid(T-5s)) / 5s
- `mid_acceleration_5s` = (velocity_5s(T) − velocity_5s(T-5s)) / 5s
- `distance_per_quote_5s` = Σ|mid_change| / n_quotes
- `directional_efficiency_5s` = net_move / distance_travelled

### F. Quote exhaustion (candidate)

- Rising intensity + falling per-quote displacement + widening spread
- Rising intensity + rising two-way ratio + narrowing net move
- Directional persistence decaying below rolling median

### G. Quote re-acceleration (candidate)

- Rising intensity after a pullback
- Directional persistence flipping positive after neutral window
- Spread returning to session median
- Break of local micro high/low with intensity confirmation

**None of the above are predictive by claim. All are TO BE MEASURED.**

---

## 7. Central research question

Not:
> Can MT5 ticks tell us true order flow?

But:
> Can quote-level microstructure improve the TIMING of STRATEGIST's
> existing structural signals?

Follow-on:
> Can quote behaviour distinguish
>   IMPULSE → HEALTHY PULLBACK → DEFENCE → RE-ACCELERATION
> from
>   IMPULSE → EXHAUSTION → FAILED RETEST → REVERSAL?

Quote microstructure remains a **confirmation layer**. It is not
authorised to originate a BUY / SELL signal.

Hierarchy (unchanged):

```
MARKET STRUCTURE
  → REGIME
    → PRICE ACTION
      → VOLUME / EXISTING CONFIRMATION
        → QUOTE MICROSTRUCTURE CONFIRMATION
          → EXECUTION
```

---

## 8. Today's 4496 → 4456 case — preservation note

Capture began at **2026-09-03 14:29:53 UTC**.

Available for later study within the captured window:
- Approach to 4494–4495 zone at 15:03:38 UTC and touches
- Continuous quote sequence through session high area
- Broader 4466 – 4511 price journey during capture

**Not available:**
- The 4456 defence / retracement basing region, if it occurred BEFORE
  14:29:53 UTC (which appears to be the case — 4456 is outside
  our current captured price range low of 4466).

Any Phase-A → Phase-E reconstruction MUST tag which phases we have
raw ticks for and which are pre-capture history. The pre-capture phases
are OFF LIMITS for feature computation but may be used as narrative
context.

---

## 9. Discipline order (repeat)

```
1. CAPTURE FIRST     ← current phase
2. MEASURE SECOND
3. FORM HYPOTHESES THIRD
4. BACKTEST FOURTH
5. MODIFY STRATEGIST LAST
```

Track A remains instrumentation. No production integration until
multiple sessions and different regimes have been captured, measured,
and formally reported on.

Track B (GC MBP-1 exchange data) remains HOLD.

No premature ML.
No parameter tuning from initial samples.
No feature weights derived from a single-session dataset.
