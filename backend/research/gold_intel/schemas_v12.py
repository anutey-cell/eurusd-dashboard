"""Phase-2 research schemas — additive only.
Nothing existing is dropped.
"""
from database import SessionLocal
from sqlalchemy import text

DDL = [
    # Explicit-audit source status registry
    """
    CREATE TABLE IF NOT EXISTS options_source_audit (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name         VARCHAR(64),
        url                 VARCHAR(255),
        access_method       VARCHAR(24),      -- HTTP|HTTPS_JSON|CSV|FTP
        reachable           INTEGER,          -- 0/1
        http_status         INTEGER,
        notes               TEXT,
        probed_at_utc       DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Options chain — GENERIC across underlyings; CME GC, CBOE GLD (labelled), etc.
    """
    CREATE TABLE IF NOT EXISTS options_chain_v2 (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange                    VARCHAR(16) NOT NULL,   -- CME|CBOE
        underlying                  VARCHAR(24) NOT NULL,   -- GC (futures) | GLD (etf)
        underlying_kind             VARCHAR(16) NOT NULL,   -- FUTURES_OPTION | EQUITY_OPTION
        observation_date            DATE NOT NULL,
        retrieval_ts_utc            DATETIME NOT NULL,
        underlying_contract         VARCHAR(24),            -- GCZ2026 | GLD
        underlying_expiry           DATE,                   -- futures expiry or NULL for equity
        option_expiry               DATE NOT NULL,
        days_to_expiry              INTEGER,
        strike                      FLOAT NOT NULL,
        option_type                 CHAR(1) NOT NULL,       -- C|P
        open_interest               BIGINT,
        change_in_open_interest     BIGINT,
        volume                      BIGINT,
        settlement                  FLOAT,
        last_price                  FLOAT,
        bid                         FLOAT,
        ask                         FLOAT,
        iv                          FLOAT,                  -- observed if vendor supplies
        iv_source                   VARCHAR(24),            -- OBSERVED|DERIVED_BLACK76
        delta_observed              FLOAT,
        gamma_observed              FLOAT,
        vega_observed               FLOAT,
        theta_observed              FLOAT,
        delta_derived               FLOAT,
        gamma_derived               FLOAT,
        contract_multiplier         FLOAT,                  -- 100 for GC and GLD both
        greeks_status               VARCHAR(24),            -- OBSERVED|DERIVED_BLACK76|MIXED|NONE
        underlying_price_ref        FLOAT,
        underlying_price_ts         DATETIME,
        source                      VARCHAR(48),            -- 'cboe_delayed'|'cme_bulletin'
        source_url                  VARCHAR(255),
        raw_row_hash                VARCHAR(32),
        UNIQUE(observation_date, exchange, underlying, underlying_contract, option_expiry, strike, option_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_options_chain_lookup ON options_chain_v2(underlying, observation_date, option_expiry)",
    # Options daily aggregates and abs-gamma-concentration
    """
    CREATE TABLE IF NOT EXISTS options_gamma_concentration (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        observation_date            DATE NOT NULL,
        exchange                    VARCHAR(16) NOT NULL,
        underlying                  VARCHAR(24) NOT NULL,
        underlying_price_ref        FLOAT,
        option_expiry               DATE NOT NULL,
        days_to_expiry              INTEGER,
        strike                      FLOAT NOT NULL,
        total_oi                    BIGINT,
        call_oi                     BIGINT,
        put_oi                      BIGINT,
        total_volume                BIGINT,
        gamma_per_contract          FLOAT,     -- Black-76/BS gamma (1/price units)
        contract_multiplier         FLOAT,
        abs_gamma_concentration     FLOAT,     -- OI × mult × F² × Γ  (dollars per 1% move)
        iv_atm_snapshot             FLOAT,
        computed_at_utc             DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(observation_date, exchange, underlying, option_expiry, strike)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gamma_concentration_lookup ON options_gamma_concentration(underlying, observation_date, option_expiry)",
    # GC-XAU basis tracking dataset
    """
    CREATE TABLE IF NOT EXISTS gc_xau_basis_observations (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        observed_at_utc             DATETIME NOT NULL,
        gc_contract                 VARCHAR(16),        -- GC1! | GCZ2026 | ...
        gc_price                    FLOAT,
        xau_source                  VARCHAR(24),
        xau_price                   FLOAT,
        basis_pts                   FLOAT,              -- gc_price - xau_price
        match_lag_seconds           FLOAT,
        session_label               VARCHAR(24),
        UNIQUE(observed_at_utc, gc_contract)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gc_xau_basis ON gc_xau_basis_observations(gc_contract, observed_at_utc)",
    # Options-vs-structural confluence log
    """
    CREATE TABLE IF NOT EXISTS options_confluence_v2 (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_ts_utc             DATETIME NOT NULL,
        underlying                  VARCHAR(24),
        options_level_kind          VARCHAR(24),   -- TOP_TOTAL_OI|TOP_CALL_OI|TOP_PUT_OI|TOP_GAMMA
        options_level_native        FLOAT,
        options_level_xauusd_equiv  FLOAT,
        mapping_status              VARCHAR(24),   -- PROVISIONAL|NOT_AVAILABLE|DIRECT
        anchor_kind                 VARCHAR(24),   -- PDH|PDL|PWH|PWL|ASIA_H|ASIA_L|POC|VAH|VAL
        anchor_price                FLOAT,
        distance_pts                FLOAT,
        distance_pct                FLOAT
    )
    """,
]

with SessionLocal() as db:
    for d in DDL:
        db.execute(text(d))
    db.commit()
    from sqlalchemy import inspect
    ins = inspect(db.bind)
    for t in ["options_source_audit","options_chain_v2","options_gamma_concentration",
              "gc_xau_basis_observations","options_confluence_v2"]:
        n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        print(f"  {t:<34} exists=True  n={n}")
