"""Gold Intelligence — research schemas.

Creates NEW tables ONLY. Does not touch existing production tables.
DDL is executed inline (no backend restart needed).
"""
from database import SessionLocal
from sqlalchemy import text

DDL = [
    # ── CFTC Disaggregated COT — raw ────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cftc_cot_raw (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        report_type                 VARCHAR(16) NOT NULL,   -- 'FUT_ONLY' | 'COMBINED'
        cftc_commodity_code         VARCHAR(16) NOT NULL,   -- '088691' gold
        market_and_exchange_names   VARCHAR(128),
        report_date_yyyy_mm_dd      DATE NOT NULL,          -- CFTC 'as-of Tuesday'
        publication_date_utc        DATETIME,               -- when CFTC published
        open_interest_all           BIGINT,
        prod_merc_positions_long_all BIGINT,
        prod_merc_positions_short_all BIGINT,
        swap_positions_long_all     BIGINT,
        swap_positions_short_all    BIGINT,
        swap_positions_spread_all   BIGINT,
        m_money_positions_long_all  BIGINT,
        m_money_positions_short_all BIGINT,
        m_money_positions_spread_all BIGINT,
        other_rept_positions_long_all BIGINT,
        other_rept_positions_short_all BIGINT,
        other_rept_positions_spread_all BIGINT,
        tot_rept_positions_long_all BIGINT,
        tot_rept_positions_short_all BIGINT,
        nonrept_positions_long_all  BIGINT,
        nonrept_positions_short_all BIGINT,
        change_in_open_interest_all BIGINT,
        change_in_prod_merc_long    BIGINT,
        change_in_prod_merc_short   BIGINT,
        change_in_swap_long         BIGINT,
        change_in_swap_short        BIGINT,
        change_in_m_money_long      BIGINT,
        change_in_m_money_short     BIGINT,
        change_in_other_rept_long   BIGINT,
        change_in_other_rept_short  BIGINT,
        raw_json                    TEXT,
        source_url                  VARCHAR(255),
        ingested_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(report_type, cftc_commodity_code, report_date_yyyy_mm_dd)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cftc_raw_date ON cftc_cot_raw(report_date_yyyy_mm_dd)",
    "CREATE INDEX IF NOT EXISTS idx_cftc_raw_type_code ON cftc_cot_raw(report_type, cftc_commodity_code)",

    # ── CFTC Disaggregated COT — derived ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cftc_cot_derived (
        raw_id                      INTEGER PRIMARY KEY,       -- FK cftc_cot_raw.id
        report_type                 VARCHAR(16),
        report_date_yyyy_mm_dd      DATE,
        -- FACTS reissued for query convenience
        oi_all                      BIGINT,
        m_money_long                BIGINT,
        m_money_short               BIGINT,
        m_money_spread              BIGINT,
        -- DERIVED
        m_money_net                 BIGINT,     -- long - short
        prod_merc_net               BIGINT,
        swap_net                    BIGINT,
        other_rept_net              BIGINT,
        -- weekly deltas
        d_m_money_long_1w           BIGINT,
        d_m_money_short_1w          BIGINT,
        d_m_money_net_1w            BIGINT,
        d_oi_1w                     BIGINT,
        -- ratios
        m_money_net_pct_oi          FLOAT,
        m_money_long_pct_oi         FLOAT,
        m_money_short_pct_oi        FLOAT,
        -- rolling percentiles of key metrics (computed against a lookback window)
        m_money_net_pctile_1y       FLOAT,
        m_money_net_pctile_3y       FLOAT,
        m_money_net_pctile_5y       FLOAT,
        m_money_net_z_3y            FLOAT,
        computed_at                 DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,

    # ── Macro / Rates ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS macro_series_raw (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id                   VARCHAR(48) NOT NULL,
        series_name                 VARCHAR(96),
        obs_date                    DATE NOT NULL,          -- observation date
        obs_ts_utc                  DATETIME,               -- if intraday
        value                       FLOAT,
        unit                        VARCHAR(24),
        provider                    VARCHAR(48),
        source_url                  VARCHAR(255),
        frequency                   VARCHAR(16),            -- D|W|M|EOD|INTRADAY
        latency_hint                VARCHAR(48),
        is_stale                    INTEGER DEFAULT 0,
        raw_payload                 TEXT,
        ingested_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(series_id, obs_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_macro_series_date ON macro_series_raw(series_id, obs_date)",

    # ── Gold Data Discovery Register ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS gold_data_register (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_name                VARCHAR(96) NOT NULL,
        category                    VARCHAR(24),            -- MACRO|POSITIONING|OPTIONS|PHYSICAL|FLOWS|STRUCTURE|MICROSTRUCTURE|OTC
        provider                    VARCHAR(64),
        official_source_url         VARCHAR(255),
        instrument                  VARCHAR(48),
        coverage                    VARCHAR(96),
        frequency                   VARCHAR(24),
        latency                     VARCHAR(48),
        historical_depth            VARCHAR(48),
        observed_or_inferred        VARCHAR(24),
        cost_tier                   VARCHAR(24),
        api_available               INTEGER,
        reliability_note            TEXT,
        integration_complexity      VARCHAR(24),
        expected_info_value         VARCHAR(24),
        redundancy_flag             VARCHAR(24),
        research_priority           VARCHAR(4),             -- P1|P2|P3
        status                      VARCHAR(24),            -- DISCOVERED|UNDER_REVIEW|APPROVED_FOR_RESEARCH|TESTING|REJECTED|PROMOTED
        created_at                  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(dataset_name)
    )
    """,

    # ── GEX-Lite schema (design only — no ingestion active) ──────────────
    """
    CREATE TABLE IF NOT EXISTS gc_options_chain_raw (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        obs_date                    DATE NOT NULL,
        contract                    VARCHAR(24),
        expiry_date                 DATE,
        strike                      FLOAT NOT NULL,
        cp                          CHAR(1) NOT NULL,
        underlying_futures_price    FLOAT,
        settlement                  FLOAT,
        iv                          FLOAT,
        delta_greek                 FLOAT,
        gamma_greek                 FLOAT,
        oi                          BIGINT,
        oi_change                   BIGINT,
        volume                      BIGINT,
        bid                         FLOAT,
        ask                         FLOAT,
        provider                    VARCHAR(48),
        source_url                  VARCHAR(255),
        raw_payload                 TEXT,
        ingested_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(obs_date, contract, expiry_date, strike, cp)
    )
    """,

    # ── Daily Intelligence audit trail ───────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS gold_daily_intel_snapshots (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_ts_utc             DATETIME NOT NULL,
        cover_date                  DATE,                   -- date the snapshot represents
        conclusion                  VARCHAR(24),            -- BULLISH|BEARISH|NEUTRAL|CONFLICTED|NO_EDGE|STAND_ASIDE
        payload_json                TEXT,
        version                     VARCHAR(24),
        created_at                  DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

def main():
    with SessionLocal() as db:
        for d in DDL:
            db.execute(text(d))
        db.commit()
        # Confirm
        from sqlalchemy import inspect
        ins = inspect(db.bind)
        wanted = ["cftc_cot_raw", "cftc_cot_derived", "macro_series_raw",
                  "gold_data_register", "gc_options_chain_raw",
                  "gold_daily_intel_snapshots"]
        for t in wanted:
            exists = t in ins.get_table_names()
            n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() if exists else "N/A"
            print(f"  {t:<32} exists={exists}  n={n}")

if __name__ == "__main__":
    main()
