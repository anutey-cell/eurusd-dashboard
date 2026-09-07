"""Systemd-timer entrypoint — refresh CBOE GLD options + concentration."""
import sys, importlib.util, os
# Reuse the p2 ingest module regardless of path (works in container OR here)
p2_paths = [
    "/app/p2_10_ingest_and_math.py",
    os.path.join(os.path.dirname(__file__), "p2_ingest_and_math.py"),
]
mod = None
for p in p2_paths:
    if os.path.exists(p):
        spec = importlib.util.spec_from_file_location("p2ingest", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        break
if mod is None:
    raise SystemExit("pipeline_options: cannot locate p2 ingest module")

from database import SessionLocal
from datetime import datetime, timezone
from sqlalchemy import text

with SessionLocal() as db:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        n, S, ts = mod.ingest_cboe_gld(db)
        mod.compute_gld_concentration(db)
        status, detail = "OK", f"S={S}"
    except Exception as e:
        n, status, detail = 0, "FAILED", str(e)
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(text(
        "INSERT INTO research_pipeline_runs "
        "(pipeline_name, started_at_utc, finished_at_utc, status, detail, rows_written) "
        "VALUES ('options_pull_timer', :s, :f, :st, :d, :n)"
    ), {"s": started, "f": finished, "st": status, "d": detail, "n": n})
    db.commit()
    print("options rows written:", n, "status:", status)
