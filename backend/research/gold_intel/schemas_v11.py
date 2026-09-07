"""Phase-1 closure — v1.1 research schemas.

Additive; existing tables (cftc_cot_raw, cftc_cot_derived, macro_series_raw,
gold_data_register, gc_options_chain_raw, gold_daily_intel_snapshots) are
untouched. New tables:

  gold_intel_events           research-only event calendar (never overwrites macro_events)
  gold_intel_gc_bars          research-only GC futures history (not gc_futures_bars)
  cftc_cot_versions           immutable revision log
  research_pipeline_runs      operational heartbeat + last-run timestamps
"""
from database import SessionLocal
from sqlalchemy import text

DDL = [
    """
    CREATE TABLE IF NOT EXISTS gold_intel_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        event_time_utc    DATETIME NOT NULL,
        currency          VARCHAR(8),
        event_title       VARCHAR(256) NOT NULL,
        impact            VARCHAR(16),         -- high|medium|low
        forecast          VARCHAR(64),
        previous_value    VARCHAR(64),
        actual_value      VARCHAR(64),
        source            VARCHAR(32) DEFAULT 'forexfactory',
        source_row_hash   VARCHAR(32),
        gold_relevance    VARCHAR(24),         -- CORE|CONTEXT|LOW
        last_updated_utc  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(event_time_utc, currency, event_title)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intel_events_time ON gold_intel_events(event_time_utc)",
    "CREATE INDEX IF NOT EXISTS idx_intel_events_impact ON gold_intel_events(impact)",

    """
    CREATE TABLE IF NOT EXISTS gold_intel_gc_bars (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        contract          VARCHAR(24) NOT NULL,     -- e.g. GC1! (continuous) or GCZ2026
        timeframe         VARCHAR(8)  NOT NULL,     -- D1|H1|M15|M5
        bar_time_utc      DATETIME NOT NULL,
        open              FLOAT, high FLOAT, low FLOAT, close FLOAT,
        volume            BIGINT,
        provider          VARCHAR(32),              -- 'tradingview_anon'
        exchange          VARCHAR(16),              -- 'COMEX'
        ingested_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(contract, timeframe, bar_time_utc)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gc_bars_time ON gold_intel_gc_bars(contract, timeframe, bar_time_utc)",

    """
    CREATE TABLE IF NOT EXISTS cftc_cot_versions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        report_type       VARCHAR(16),
        cftc_commodity_code VARCHAR(16),
        report_date_yyyy_mm_dd DATE,
        retrieval_ts_utc  DATETIME DEFAULT CURRENT_TIMESTAMP,
        source_url        VARCHAR(255),
        raw_json          TEXT,
        content_hash      VARCHAR(64)      -- sha256(raw_json)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cftc_ver_lookup ON cftc_cot_versions(report_type, report_date_yyyy_mm_dd, retrieval_ts_utc)",

    """
    CREATE TABLE IF NOT EXISTS research_pipeline_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_name     VARCHAR(48) NOT NULL,     -- 'cftc_weekly'|'macro_daily'|'events_hourly'|'snapshot'
        started_at_utc    DATETIME NOT NULL,
        finished_at_utc   DATETIME,
        status            VARCHAR(24),              -- OK|FAILED|NO_NEW_DATA
        detail            TEXT,
        rows_seen         INTEGER,
        rows_written      INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_lookup ON research_pipeline_runs(pipeline_name, started_at_utc)",
]

with SessionLocal() as db:
    for d in DDL:
        db.execute(text(d))
    db.commit()
    from sqlalchemy import inspect
    ins = inspect(db.bind)
    for t in ["gold_intel_events", "gold_intel_gc_bars",
              "cftc_cot_versions", "research_pipeline_runs"]:
        n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        print(f"  {t:<28} exists=True  n={n}")
