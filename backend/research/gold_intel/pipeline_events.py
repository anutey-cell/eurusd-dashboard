"""Systemd-timer entrypoint — refresh gold_intel_events."""
import sys
sys.path.insert(0, "/app")
from database import SessionLocal
from datetime import datetime, timezone
from sqlalchemy import text
from closure_20_snapshot_v11 import refresh_events

with SessionLocal() as db:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    r = refresh_events(db)
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(text(
        "INSERT INTO research_pipeline_runs "
        "(pipeline_name, started_at_utc, finished_at_utc, status, detail, rows_written) "
        "VALUES ('events_pull_timer', :s, :f, :st, :d, :n)"
    ), {"s": started, "f": finished, "st": r["status"],
         "d": r.get("reason",""), "n": r.get("written",0)})
    db.commit()
    print(r)
