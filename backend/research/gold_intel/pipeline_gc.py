"""Systemd-timer entrypoint — refresh gold_intel_gc_bars + basis observation."""
import sys
sys.path.insert(0, "/app")
from database import SessionLocal
from datetime import datetime, timezone
from sqlalchemy import text
from closure_20_snapshot_v11 import refresh_gc_bars, provisional_basis

with SessionLocal() as db:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    n = refresh_gc_bars(db)
    b = provisional_basis(db, max_lag_min=60)
    if b.get("status") == "PROVISIONAL":
        db.execute(text("""
            INSERT OR IGNORE INTO gc_xau_basis_observations (
                observed_at_utc, gc_contract, gc_price, xau_source, xau_price,
                basis_pts, match_lag_seconds, session_label
            ) VALUES (:t, :c, :gc, 'mt5_ticks_mid', :xa, :b, 0, NULL)
        """), {"t": b["gc_obs_utc"], "c": b["gc_contract"],
                "gc": b["gc_close"], "xa": b["xau_mid"],
                "b": b["PROVISIONAL_basis_pts"]})
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(text(
        "INSERT INTO research_pipeline_runs "
        "(pipeline_name, started_at_utc, finished_at_utc, status, detail, rows_written) "
        "VALUES ('gc_pull_timer', :s, :f, 'OK', :d, :n)"
    ), {"s": started, "f": finished, "d": str(b.get("PROVISIONAL_basis_pts", "-")), "n": n})
    db.commit()
    print("gc bars written:", n, "basis:", b.get("PROVISIONAL_basis_pts"))
