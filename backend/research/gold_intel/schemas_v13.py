"""Phase-2C research schemas for CME bulletin ingestion.

Research-only. Additive; does not modify any production table.
Nothing here is imported by strategist / predator / vp_trap / execution.
"""
import sys
sys.path.insert(0, "/app")
from database import SessionLocal
from sqlalchemy import text

DDL = [
    # ── Immutable raw bulletin archive: one row per file received ─────────
    """
    CREATE TABLE IF NOT EXISTS cme_bulletin_archive (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at_utc        DATETIME DEFAULT CURRENT_TIMESTAMP,
        source_filename        VARCHAR(255) NOT NULL,
        stored_path            VARCHAR(512) NOT NULL,
        file_hash_sha256       VARCHAR(64) NOT NULL,
        file_size_bytes        BIGINT,
        bulletin_type          VARCHAR(48),      -- OPTIONS_METALS | FUTURES_METALS | UNKNOWN
        section_number         VARCHAR(16),      -- 64, 62, or dynamic (never hard-coded)
        bulletin_date          DATE,
        bulletin_number        VARCHAR(16),
        bulletin_status        VARCHAR(24),      -- PRELIMINARY | FINAL
        detector_confidence    VARCHAR(16),      -- HIGH | MEDIUM | LOW
        detector_signals       TEXT,             -- json of matched signals
        ingest_status          VARCHAR(24),      -- ACCEPTED | QUARANTINED | REJECTED | DUPLICATE
        ingest_notes           TEXT,
        UNIQUE(file_hash_sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cme_archive_date ON cme_bulletin_archive(bulletin_date, bulletin_status)",
    "CREATE INDEX IF NOT EXISTS idx_cme_archive_type ON cme_bulletin_archive(bulletin_type)",

    # ── Options rows (COMEX gold options) ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cme_gc_options_eod (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_id                INTEGER NOT NULL,       -- FK cme_bulletin_archive.id
        bulletin_date             DATE NOT NULL,
        bulletin_status           VARCHAR(24) NOT NULL,   -- PRELIMINARY | FINAL
        bulletin_number           VARCHAR(16),
        retrieval_ts_utc          DATETIME,
        source_file_hash          VARCHAR(64),
        source_page_hint          INTEGER,
        product_code              VARCHAR(8) NOT NULL,
        product_name              VARCHAR(64),
        option_type               VARCHAR(4) NOT NULL,    -- CALL | PUT | OPT
        option_expiry_code        VARCHAR(8) NOT NULL,    -- e.g. OCT26
        option_expiry_month       VARCHAR(7),             -- e.g. 2026-10
        underlying_futures_contract VARCHAR(16),          -- e.g. GCV6 (populated by reconciler)
        strike                    FLOAT NOT NULL,
        settlement                FLOAT,
        settlement_change         FLOAT,
        settlement_change_flag    VARCHAR(8),
        delta_cme                 FLOAT,                  -- OBSERVED (bulletin-published)
        exercises                 INTEGER,
        open_outcry_volume        INTEGER,
        globex_volume             INTEGER,
        pnt_volume                INTEGER,
        open_interest             INTEGER,
        oi_direction_marker       VARCHAR(2),
        open_interest_change      INTEGER,
        oi_change_flag            VARCHAR(8),
        raw_row                   TEXT,
        raw_row_hash              VARCHAR(32),
        parse_status              VARCHAR(24),            -- PARSED | QUARANTINED
        UNIQUE(bulletin_date, bulletin_status, product_code, option_type,
                option_expiry_code, strike)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gc_opt_lookup ON cme_gc_options_eod(bulletin_date, product_code, option_expiry_code)",
    "CREATE INDEX IF NOT EXISTS idx_gc_opt_status ON cme_gc_options_eod(bulletin_status)",

    # ── Futures rows (GC + MGC only) ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cme_gc_futures_eod (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_id                INTEGER NOT NULL,
        bulletin_date             DATE NOT NULL,
        bulletin_status           VARCHAR(24) NOT NULL,
        bulletin_number           VARCHAR(16),
        retrieval_ts_utc          DATETIME,
        source_file_hash          VARCHAR(64),
        source_page_hint          INTEGER,
        product_code              VARCHAR(8) NOT NULL,    -- GC | MGC | 1OZ
        product_name              VARCHAR(64),
        contract_month_code       VARCHAR(8) NOT NULL,    -- e.g. DEC26
        contract_month_iso        VARCHAR(7),             -- 2026-12
        contract_symbol           VARCHAR(16),            -- e.g. GCZ26 (derived)
        session_open              FLOAT,
        globex_high               FLOAT,
        globex_low                FLOAT,
        settlement                FLOAT,
        settlement_change         FLOAT,
        settlement_change_flag    VARCHAR(8),
        globex_volume             INTEGER,
        open_outcry_volume        INTEGER,
        open_interest             INTEGER,
        oi_direction_marker       VARCHAR(2),
        oi_change                 INTEGER,
        oi_change_flag            VARCHAR(8),
        raw_row                   TEXT,
        raw_row_hash              VARCHAR(32),
        parse_status              VARCHAR(24),
        UNIQUE(bulletin_date, bulletin_status, product_code, contract_month_code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gc_fut_lookup ON cme_gc_futures_eod(bulletin_date, product_code, contract_month_code)",

    # ── Reconciliation between options-expiry and underlying futures ─────
    """
    CREATE TABLE IF NOT EXISTS cme_contract_reconciliation (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        bulletin_date          DATE NOT NULL,
        options_status         VARCHAR(24),
        futures_status         VARCHAR(24),
        options_product_code   VARCHAR(8) NOT NULL,
        option_expiry_code     VARCHAR(8) NOT NULL,
        underlying_product     VARCHAR(8),          -- GC | MGC
        underlying_contract_month VARCHAR(8),
        futures_settlement     FLOAT,
        mapping_method         VARCHAR(64),
        mapping_status         VARCHAR(32) NOT NULL, -- MATCHED | UNRESOLVED | MISSING_FUTURES_CONTRACT
        computed_at_utc        DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(bulletin_date, options_status, options_product_code, option_expiry_code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reconciliation_date ON cme_contract_reconciliation(bulletin_date)",

    # ── Ingestion runs log ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cme_ingestion_runs (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at_utc         DATETIME DEFAULT CURRENT_TIMESTAMP,
        finished_at_utc        DATETIME,
        run_kind               VARCHAR(24),           -- CLI | TIMER | UPLOAD
        input_files            TEXT,                  -- json list
        archive_ids            TEXT,                  -- json list
        options_rows_ingested  INTEGER DEFAULT 0,
        futures_rows_ingested  INTEGER DEFAULT 0,
        reconciliation_rows    INTEGER DEFAULT 0,
        quarantined_rows       INTEGER DEFAULT 0,
        parser_completeness    FLOAT,                 -- 0..1
        status                 VARCHAR(24),           -- OK | PARTIAL | FAILED
        errors                 TEXT
    )
    """,

    # ── Canonical view helper table: LATEST-per-date rows ────────────────
    # Instead of a view we resolve at query time; keeping a helper index only.
    "CREATE INDEX IF NOT EXISTS idx_opt_canonical ON cme_gc_options_eod(bulletin_date, product_code, option_type, option_expiry_code, strike, bulletin_status)",
]

def apply_ddl():
    with SessionLocal() as db:
        for d in DDL:
            db.execute(text(d))
        db.commit()
        from sqlalchemy import inspect
        ins = inspect(db.bind)
        report = []
        for t in ["cme_bulletin_archive", "cme_gc_options_eod",
                  "cme_gc_futures_eod", "cme_contract_reconciliation",
                  "cme_ingestion_runs"]:
            n = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            report.append((t, n))
        return report

if __name__ == "__main__":
    for t, n in apply_ddl():
        print(f"  {t:<34} exists=True  n={n}")
