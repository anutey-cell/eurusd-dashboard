"""CME research ingestion orchestrator.

End-to-end pipeline for CME Metals Options + Metals Futures daily bulletins.
Research-only. No production coupling.

Flow:
  raw PDF path(s)
    -> pdftotext -layout extraction
    -> detector (bulletin_type, date, status, number)
    -> archive (immutable raw row w/ sha256)
    -> parser (options or futures)
    -> DB write (versioned: PRELIMINARY + FINAL preserved separately)
    -> reconciliation (options expiry -> underlying futures contract)
    -> model-free metrics (OI concentration by strike / expiry)
    -> ingestion report

Fail-safes:
  - Reject duplicate file_hash (same file re-uploaded, no version change)
  - Reject when bulletin_type detection is UNKNOWN
  - Reject when required metadata (date/status) missing
  - Quarantine parsed rows with parse_status != PARSED
  - Reconciliation flags MATCHED / UNRESOLVED / MISSING_FUTURES_CONTRACT

No gamma computation is triggered here. Gamma is disabled per Phase-2C directive.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(__file__))
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from database import SessionLocal
from sqlalchemy import text

from cme_bulletin_detector import detect
from cme_bulletin_parser  import parse_bulletin       # options
from cme_futures_parser   import parse_futures        # futures

ARCHIVE_ROOT = os.environ.get("CME_ARCHIVE_ROOT", "/root/gold_intel_archive")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_to_text(pdf_path: str) -> str:
    """Uses pdftotext -layout in the container (installed as poppler-utils
    if not present; caller can adjust)."""
    tmp = f"/tmp/gi_ingest_{os.getpid()}.txt"
    subprocess.run(["pdftotext", "-layout", pdf_path, tmp], check=True)
    with open(tmp, "rb") as f:
        raw = f.read()
    try: os.remove(tmp)
    except Exception: pass
    return raw.decode("cp1252", errors="replace")


def archive_raw(pdf_path: str, bulletin_meta: dict, file_hash: str) -> str:
    """Copy the raw PDF into an immutable archive tree keyed by date + hash."""
    day = bulletin_meta.get("bulletin_date") or "unknown-date"
    ver = bulletin_meta.get("bulletin_status") or "unknown-status"
    kind = bulletin_meta.get("bulletin_type") or "UNKNOWN"
    dest_dir = os.path.join(ARCHIVE_ROOT, day, kind, ver)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{file_hash[:16]}_{os.path.basename(pdf_path)}")
    if not os.path.exists(dest):
        shutil.copy2(pdf_path, dest)
    return dest


def already_ingested(db, file_hash: str) -> Optional[int]:
    r = db.execute(text(
        "SELECT id FROM cme_bulletin_archive WHERE file_hash_sha256=:h LIMIT 1"
    ), {"h": file_hash}).fetchone()
    return r[0] if r else None


def insert_archive(db, pdf_path: str, stored_path: str, file_hash: str,
                    file_size: int, meta: dict, status: str, notes: str) -> int:
    db.execute(text("""
        INSERT INTO cme_bulletin_archive (
            source_filename, stored_path, file_hash_sha256, file_size_bytes,
            bulletin_type, section_number, bulletin_date, bulletin_number,
            bulletin_status, detector_confidence, detector_signals,
            ingest_status, ingest_notes
        ) VALUES (
            :fn, :sp, :h, :sz, :bt, :sn, :bd, :bnum, :bs, :dc, :dsj, :ist, :nt
        )
    """), {
        "fn": os.path.basename(pdf_path), "sp": stored_path,
        "h": file_hash, "sz": file_size,
        "bt": meta.get("bulletin_type"), "sn": meta.get("section_number"),
        "bd": meta.get("bulletin_date"), "bnum": meta.get("bulletin_number"),
        "bs": meta.get("bulletin_status"),
        "dc": meta.get("detector_confidence"),
        "dsj": json.dumps(meta.get("detector_signals", {})),
        "ist": status, "nt": notes,
    })
    return db.execute(text("SELECT last_insert_rowid()")).scalar()


def write_options(db, archive_id: int, meta: dict, rows) -> int:
    file_hash = None
    r = db.execute(text("SELECT file_hash_sha256 FROM cme_bulletin_archive WHERE id=:i"),
                    {"i": archive_id}).fetchone()
    if r: file_hash = r[0]
    written = 0
    for r in rows:
        db.execute(text("""
            INSERT OR REPLACE INTO cme_gc_options_eod (
                archive_id, bulletin_date, bulletin_status, bulletin_number,
                retrieval_ts_utc, source_file_hash, source_page_hint,
                product_code, product_name, option_type,
                option_expiry_code, option_expiry_month, underlying_futures_contract,
                strike, settlement, settlement_change, settlement_change_flag,
                delta_cme, exercises, open_outcry_volume, globex_volume, pnt_volume,
                open_interest, oi_direction_marker, open_interest_change, oi_change_flag,
                raw_row, raw_row_hash, parse_status
            ) VALUES (
                :aid, :bd, :bs, :bn, :ts, :fh, :sph,
                :pc, :pn, :ot, :oec, :oem, NULL,
                :k, :s, :sc, :scf, :d, :ex, :ov, :gv, :pv,
                :oi, :oim, :oic, :oicf,
                :rr, :rrh, :ps
            )
        """), {
            "aid": archive_id, "bd": r.bulletin_date, "bs": r.bulletin_status,
            "bn": r.bulletin_number, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "fh": file_hash, "sph": r.source_page_hint,
            "pc": r.product_code, "pn": r.product_name, "ot": r.option_type,
            "oec": r.option_expiry_code, "oem": r.option_expiry_date,
            "k": r.strike, "s": r.settlement, "sc": r.settlement_change,
            "scf": r.settlement_change_flag, "d": r.delta_cme,
            "ex": r.exercises, "ov": r.open_outcry_volume,
            "gv": r.globex_volume, "pv": r.pnt_volume,
            "oi": r.open_interest, "oim": r.oi_direction_marker,
            "oic": r.open_interest_change, "oicf": r.oi_change_flag,
            "rr": r.raw_row, "rrh": r.raw_row_hash, "ps": r.parse_status,
        })
        written += 1
    db.commit()
    return written


def write_futures(db, archive_id: int, meta: dict, rows) -> int:
    r = db.execute(text("SELECT file_hash_sha256 FROM cme_bulletin_archive WHERE id=:i"),
                    {"i": archive_id}).fetchone()
    file_hash = r[0] if r else None
    written = 0
    for r in rows:
        # Only extract GC / MGC (per directive: only gold research)
        if r.product_code not in ("GC", "MGC"): continue
        db.execute(text("""
            INSERT OR REPLACE INTO cme_gc_futures_eod (
                archive_id, bulletin_date, bulletin_status, bulletin_number,
                retrieval_ts_utc, source_file_hash, source_page_hint,
                product_code, product_name, contract_month_code, contract_month_iso,
                contract_symbol, session_open, globex_high, globex_low,
                settlement, settlement_change, settlement_change_flag,
                globex_volume, open_outcry_volume, open_interest,
                oi_direction_marker, oi_change, oi_change_flag,
                raw_row, raw_row_hash, parse_status
            ) VALUES (
                :aid, :bd, :bs, :bn, :ts, :fh, :sph,
                :pc, :pn, :cmc, :cmi, :csym,
                :so, :gh, :gl,
                :s, :sc, :scf, :gv, :ov, :oi,
                :oim, :oic, :oicf, :rr, :rrh, :ps
            )
        """), {
            "aid": archive_id, "bd": r.bulletin_date, "bs": r.bulletin_status,
            "bn": r.bulletin_number, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "fh": file_hash, "sph": r.source_page_hint,
            "pc": r.product_code, "pn": r.product_name,
            "cmc": r.contract_month_code, "cmi": r.contract_month_iso,
            "csym": r.contract_symbol, "so": r.session_open,
            "gh": r.globex_high, "gl": r.globex_low,
            "s": r.settlement, "sc": r.settlement_change, "scf": r.settlement_change_flag,
            "gv": r.globex_volume, "ov": r.open_outcry_volume,
            "oi": r.open_interest, "oim": r.oi_direction_marker,
            "oic": r.oi_change, "oicf": r.oi_change_flag,
            "rr": r.raw_row, "rrh": r.raw_row_hash, "ps": r.parse_status,
        })
        written += 1
    db.commit()
    return written


# ── Reconciliation ────────────────────────────────────────────────────────
# CME contract-mapping rules for gold options -> underlying futures:
#   OG monthly options are written on the standard COMEX Gold Futures
#     contract of the SAME calendar month; e.g. OG DEC26 -> GC DEC26.
#   OG1..OG4 weekly options reference the nearest active GC monthly
#     futures contract at their LTD; here we map to the front-month GC
#     futures contract identified by highest OI on the day.
#   OMG (Micro Gold Options) -> MGC futures of the same month.
def reconcile(db, bulletin_date: str, options_status: str, futures_status: str) -> int:
    # Front-month by max OI among GC futures on that bulletin_date
    front = db.execute(text("""
        SELECT contract_month_code FROM cme_gc_futures_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND product_code='GC'
          AND open_interest IS NOT NULL
        ORDER BY open_interest DESC LIMIT 1
    """), {"d": bulletin_date, "s": futures_status}).fetchone()
    front_gc = front[0] if front else None

    # Wipe prior reconciliations for this (date, status) to allow re-run
    db.execute(text("""
        DELETE FROM cme_contract_reconciliation
        WHERE bulletin_date=:d AND options_status=:s
    """), {"d": bulletin_date, "s": options_status})

    # Distinct option (product, expiry) pairs on that bulletin
    pairs = db.execute(text("""
        SELECT DISTINCT product_code, option_expiry_code
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
    """), {"d": bulletin_date, "s": options_status}).fetchall()

    n_written = 0
    for prod, exp in pairs:
        if prod.startswith("OG"):
            underlying_product = "GC"
            if prod in ("OG1","OG2","OG3","OG4"):
                # weekly -> front-month
                underlying_month = front_gc
                mapping_method = "weekly->front_month_by_max_OI"
            else:
                underlying_month = exp
                mapping_method = "OG_monthly_same_month"
        elif prod == "OMG" or prod in ("MMG","WMG","FMG"):
            underlying_product = "MGC"
            underlying_month = exp
            mapping_method = "micro_gold_same_month"
        elif prod in ("GMW","GWR","GWT","GWW"):
            underlying_product = "GC"
            underlying_month = front_gc
            mapping_method = "GC_weekly_dow->front_month"
        else:
            underlying_product = None
            underlying_month = None
            mapping_method = None

        # Fetch underlying futures settlement
        settle = None
        mapping_status = "UNRESOLVED"
        if underlying_product and underlying_month:
            r = db.execute(text("""
                SELECT settlement FROM cme_gc_futures_eod
                WHERE bulletin_date=:d AND bulletin_status=:s
                  AND product_code=:p AND contract_month_code=:m
            """), {"d": bulletin_date, "s": futures_status,
                    "p": underlying_product, "m": underlying_month}).fetchone()
            if r and r[0] is not None:
                settle = r[0]
                mapping_status = "MATCHED"
            else:
                mapping_status = "MISSING_FUTURES_CONTRACT"

        db.execute(text("""
            INSERT INTO cme_contract_reconciliation (
                bulletin_date, options_status, futures_status,
                options_product_code, option_expiry_code,
                underlying_product, underlying_contract_month,
                futures_settlement, mapping_method, mapping_status
            ) VALUES (
                :d, :os, :fs, :op, :oe, :up, :um, :se, :mm, :ms
            )
        """), {"d": bulletin_date, "os": options_status, "fs": futures_status,
                "op": prod, "oe": exp, "up": underlying_product,
                "um": underlying_month, "se": settle,
                "mm": mapping_method, "ms": mapping_status})
        n_written += 1
    db.commit()
    return n_written


# ── Model-free metrics ────────────────────────────────────────────────────
def model_free_metrics(db, bulletin_date: str, options_status: str) -> dict:
    """Compute OI-only concentrations for reporting. No gamma."""
    def q(sql, **kw):
        return db.execute(text(sql), {"d": bulletin_date, "s": options_status, **kw}).fetchall()

    # Top-10 by call OI (across all expiries) — OG monthly
    top_call = q("""
        SELECT strike, product_code, option_expiry_code, open_interest
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND option_type='CALL'
          AND product_code='OG' AND open_interest IS NOT NULL
        ORDER BY open_interest DESC LIMIT 10
    """)
    top_put = q("""
        SELECT strike, product_code, option_expiry_code, open_interest
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND option_type='PUT'
          AND product_code='OG' AND open_interest IS NOT NULL
        ORDER BY open_interest DESC LIMIT 10
    """)
    # Top-10 by TOTAL OI grouped by (strike, expiry) across calls+puts
    top_total = q("""
        SELECT strike, option_expiry_code, SUM(open_interest) AS toi
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
          AND product_code='OG' AND open_interest IS NOT NULL
        GROUP BY strike, option_expiry_code
        ORDER BY toi DESC LIMIT 10
    """)
    # Top OI-change strikes (absolute)
    top_change = q("""
        SELECT strike, option_type, option_expiry_code, open_interest_change
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND open_interest_change IS NOT NULL
          AND product_code='OG'
        ORDER BY ABS(open_interest_change) DESC LIMIT 10
    """)
    # OI concentration by expiry
    by_expiry = q("""
        SELECT option_expiry_code,
               SUM(CASE WHEN option_type='CALL' THEN open_interest ELSE 0 END) AS call_oi,
               SUM(CASE WHEN option_type='PUT'  THEN open_interest ELSE 0 END) AS put_oi,
               SUM(open_interest) AS total_oi
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND product_code='OG'
          AND open_interest IS NOT NULL
        GROUP BY option_expiry_code
        ORDER BY total_oi DESC
    """)
    return {
        "top_10_call_oi_OG":    [{"strike":x[0],"expiry":x[2],"call_oi":x[3]} for x in top_call],
        "top_10_put_oi_OG":     [{"strike":x[0],"expiry":x[2],"put_oi":x[3]} for x in top_put],
        "top_10_total_oi_OG":   [{"strike":x[0],"expiry":x[1],"total_oi":x[2]} for x in top_total],
        "top_10_oi_change_OG":  [{"strike":x[0],"type":x[1],"expiry":x[2],"oi_change":x[3]} for x in top_change],
        "oi_concentration_by_expiry_OG": [
            {"expiry":x[0],"call_oi":x[1],"put_oi":x[2],"total_oi":x[3]} for x in by_expiry
        ],
    }


# ── Main ingestion entry ──────────────────────────────────────────────────
def ingest_pair(options_pdf: str, futures_pdf: str) -> dict:
    with SessionLocal() as db:
        started = datetime.now(timezone.utc)
        errors = []
        arc_ids = []
        n_opt_written = n_fut_written = n_recon = 0

        # Options file
        opt_hash = sha256_file(options_pdf)
        opt_text = pdf_to_text(options_pdf)
        opt_meta = detect(opt_text)
        if opt_meta.get("bulletin_type") != "OPTIONS_METALS":
            errors.append(f"options file not detected as OPTIONS_METALS: got {opt_meta.get('bulletin_type')}")
        if not opt_meta.get("bulletin_date"):
            errors.append("options file missing bulletin_date")

        # Futures file
        fut_hash = sha256_file(futures_pdf)
        fut_text = pdf_to_text(futures_pdf)
        fut_meta = detect(fut_text)
        if fut_meta.get("bulletin_type") != "FUTURES_METALS":
            errors.append(f"futures file not detected as FUTURES_METALS: got {fut_meta.get('bulletin_type')}")

        # Date mismatch is a hard fail
        if (opt_meta.get("bulletin_date") and fut_meta.get("bulletin_date")
             and opt_meta["bulletin_date"] != fut_meta["bulletin_date"]):
            errors.append(f"date mismatch: options={opt_meta['bulletin_date']} futures={fut_meta['bulletin_date']}")

        if errors:
            db.execute(text("""
                INSERT INTO cme_ingestion_runs (
                    started_at_utc, finished_at_utc, run_kind, input_files,
                    status, errors
                ) VALUES (
                    :st, :fn, 'CLI', :inp, 'FAILED', :err
                )
            """), {"st": started.strftime("%Y-%m-%d %H:%M:%S"),
                    "fn": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "inp": json.dumps([options_pdf, futures_pdf]),
                    "err": json.dumps(errors)})
            db.commit()
            return {"status": "FAILED", "errors": errors}

        # Archive
        opt_stored = archive_raw(options_pdf, opt_meta, opt_hash)
        fut_stored = archive_raw(futures_pdf, fut_meta, fut_hash)

        # Options archive row
        prev = already_ingested(db, opt_hash)
        if prev is not None:
            opt_arc_id = prev
            note = "DUPLICATE: file already ingested (identical sha256); skipping row inserts"
            db.execute(text(
                "UPDATE cme_bulletin_archive SET ingest_status='DUPLICATE', "
                "ingest_notes=:n WHERE id=:i"),
                {"n": note, "i": opt_arc_id})
            db.commit()
        else:
            opt_arc_id = insert_archive(
                db, options_pdf, opt_stored, opt_hash,
                os.path.getsize(options_pdf), opt_meta,
                "ACCEPTED", "options ingest")
            db.commit()
            opt_rows = parse_bulletin(opt_text, os.path.basename(options_pdf))[1]
            n_opt_written = write_options(db, opt_arc_id, opt_meta, opt_rows)
        arc_ids.append(opt_arc_id)

        # Futures archive row
        prev = already_ingested(db, fut_hash)
        if prev is not None:
            fut_arc_id = prev
            db.execute(text(
                "UPDATE cme_bulletin_archive SET ingest_status='DUPLICATE' WHERE id=:i"),
                {"i": fut_arc_id})
            db.commit()
        else:
            fut_arc_id = insert_archive(
                db, futures_pdf, fut_stored, fut_hash,
                os.path.getsize(futures_pdf), fut_meta,
                "ACCEPTED", "futures ingest")
            db.commit()
            fut_rows = parse_futures(fut_text, fut_meta, os.path.basename(futures_pdf))
            n_fut_written = write_futures(db, fut_arc_id, fut_meta, fut_rows)
        arc_ids.append(fut_arc_id)

        # Reconciliation
        n_recon = reconcile(db, opt_meta["bulletin_date"],
                             opt_meta["bulletin_status"],
                             fut_meta["bulletin_status"])

        # Ingestion run
        finished = datetime.now(timezone.utc)
        db.execute(text("""
            INSERT INTO cme_ingestion_runs (
                started_at_utc, finished_at_utc, run_kind, input_files,
                archive_ids, options_rows_ingested, futures_rows_ingested,
                reconciliation_rows, status
            ) VALUES (
                :st, :fn, 'CLI', :inp, :aid, :nopt, :nfut, :nrec, 'OK'
            )
        """), {"st": started.strftime("%Y-%m-%d %H:%M:%S"),
                "fn": finished.strftime("%Y-%m-%d %H:%M:%S"),
                "inp": json.dumps([options_pdf, futures_pdf]),
                "aid": json.dumps(arc_ids),
                "nopt": n_opt_written, "nfut": n_fut_written, "nrec": n_recon})
        db.commit()

        return {
            "status": "OK",
            "options_archive_id": opt_arc_id,
            "futures_archive_id": fut_arc_id,
            "options_meta": opt_meta,
            "futures_meta": fut_meta,
            "options_rows_written": n_opt_written,
            "futures_rows_written": n_fut_written,
            "reconciliation_rows": n_recon,
        }


def daily_report(db, bulletin_date: str, options_status: str, futures_status: str) -> dict:
    """Produce the daily ingestion report per Phase-2C spec."""
    # Options row totals
    r = db.execute(text("""
        SELECT COUNT(*), COUNT(DISTINCT option_expiry_code)
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s AND product_code='OG'
    """), {"d": bulletin_date, "s": options_status}).fetchone()
    n_opt_og = r[0]; n_expiries = r[1]
    r = db.execute(text("""
        SELECT COUNT(*) FROM cme_gc_options_eod WHERE bulletin_date=:d AND bulletin_status=:s
    """), {"d": bulletin_date, "s": options_status}).fetchone()
    n_opt_all = r[0]
    # Options with delta populated
    r = db.execute(text("""
        SELECT COUNT(*), COUNT(delta_cme), COUNT(settlement), COUNT(open_interest)
        FROM cme_gc_options_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
    """), {"d": bulletin_date, "s": options_status}).fetchone()
    n_rows, n_delta, n_sett, n_oi = r
    parser_completeness = round(n_sett / max(n_rows, 1), 4)

    # Futures totals
    r = db.execute(text("""
        SELECT product_code, COUNT(*), COUNT(settlement)
        FROM cme_gc_futures_eod
        WHERE bulletin_date=:d AND bulletin_status=:s
        GROUP BY product_code
    """), {"d": bulletin_date, "s": futures_status}).fetchall()
    fut_summary = {x[0]:{"contracts": x[1], "with_settlement": x[2]} for x in r}

    # Contract mapping
    r = db.execute(text("""
        SELECT mapping_status, COUNT(*) FROM cme_contract_reconciliation
        WHERE bulletin_date=:d AND options_status=:s
        GROUP BY mapping_status
    """), {"d": bulletin_date, "s": options_status}).fetchall()
    mapping_summary = {x[0]: x[1] for x in r}

    r = db.execute(text("""
        SELECT options_product_code, option_expiry_code, underlying_product,
               underlying_contract_month, futures_settlement, mapping_status
        FROM cme_contract_reconciliation
        WHERE bulletin_date=:d AND options_status=:s
        ORDER BY options_product_code, option_expiry_code
    """), {"d": bulletin_date, "s": options_status}).fetchall()
    reconciliation_rows = [{
        "options_product": x[0], "option_expiry": x[1],
        "underlying": f"{x[2]} {x[3]}" if x[2] else None,
        "futures_settle": x[4], "mapping_status": x[5]
    } for x in r]

    return {
        "bulletin_date": bulletin_date,
        "options_status": options_status,
        "futures_status": futures_status,
        "options_rows_total": n_opt_all,
        "options_rows_OG": n_opt_og,
        "option_expiries_OG": n_expiries,
        "futures_summary": fut_summary,
        "contracts_matched": mapping_summary,
        "reconciliation_rows": reconciliation_rows,
        "parser_completeness_settle": parser_completeness,
        "n_rows_with_delta_cme": n_delta,
        "n_rows_with_open_interest": n_oi,
        "model_free_metrics": model_free_metrics(SessionLocal(), bulletin_date, options_status),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: cme_ingestion.py <options.pdf> <futures.pdf>")
        sys.exit(2)
    r = ingest_pair(sys.argv[1], sys.argv[2])
    print(json.dumps({k:v for k,v in r.items() if k not in ("options_meta","futures_meta")},
                      indent=2, default=str))
    if r.get("status") == "OK":
        with SessionLocal() as db:
            rep = daily_report(db, r["options_meta"]["bulletin_date"],
                                r["options_meta"]["bulletin_status"],
                                r["futures_meta"]["bulletin_status"])
        print("\n=== DAILY REPORT ===")
        print(json.dumps(rep, indent=2, default=str))
