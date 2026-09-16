"""HOME LAPTOP CME bulletin acquisition relay.

Why this exists
---------------
CME's public web edge can reject requests from cloud-hosting address ranges.
The Windows HOME machine already acts as the trusted market-data edge for MT5,
so this small, independent process retrieves the published CME Metals daily
bulletins and relays them to the VPS for validation, parsing, storage, and
read-only options context.

Safety
------
- Does not import MetaTrader5.
- Does not poll, create, claim, or execute orders.
- Uses the existing bridge shared-secret only for the CME relay endpoint.
- The VPS re-validates PDF type/date/status before accepting data.
- Identical bulletin pairs are skipped locally by SHA-256 fingerprint.

Usage
-----
  python deploy/cme_bulletin_relay.py          # one attempt
  python deploy/cme_bulletin_relay.py --loop   # immediate attempt, then hourly
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    raise SystemExit("requests is required: pip install requests")

LOG_FORMAT = "%(asctime)s %(levelname)-7s cme_relay: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("cme_relay")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.bridge"
STATE_FILE = SCRIPT_DIR / ".cme_relay_state.json"

CME_DAILY_PAGE = "https://www.cmegroup.com/market-data/daily-bulletin.html"
CME_BASE = "https://www.cmegroup.com/daily_bulletin/current"
OPTIONS_URL = f"{CME_BASE}/Section64_Metals_Option_Products.pdf"
FUTURES_URL = f"{CME_BASE}/Section62_Metals_Futures_Products.pdf"

MIN_PDF_BYTES = 10_000
MAX_PDF_BYTES = 12 * 1024 * 1024
DEFAULT_INTERVAL_SEC = 3600


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


_ENV = _load_env_file(ENV_FILE)


def env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(key) or _ENV.get(key) or default


DASHBOARD_URL = (env("DASHBOARD_URL") or "").rstrip("/")
BRIDGE_SECRET = env("BRIDGE_SECRET", "") or ""
DAEMON_ID = env("DAEMON_ID", "home-windows-edge") or "home-windows-edge"
INTERVAL_SEC = max(900, int(env("CME_RELAY_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC)) or DEFAULT_INTERVAL_SEC))


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": CME_DAILY_PAGE,
}


def _validate_config() -> None:
    if not DASHBOARD_URL:
        raise RuntimeError(f"DASHBOARD_URL missing from {ENV_FILE}")
    if not BRIDGE_SECRET:
        raise RuntimeError(f"BRIDGE_SECRET missing from {ENV_FILE}")


def _validate_pdf(raw: bytes, label: str) -> None:
    if len(raw) < MIN_PDF_BYTES:
        raise RuntimeError(f"{label}: response too small ({len(raw)} bytes)")
    if len(raw) > MAX_PDF_BYTES:
        raise RuntimeError(f"{label}: response too large ({len(raw)} bytes)")
    if not raw.startswith(b"%PDF"):
        prefix = raw[:80].decode("utf-8", errors="replace").replace("\n", " ")
        raise RuntimeError(f"{label}: CME response is not a PDF; prefix={prefix!r}")


def _download(session: requests.Session, url: str, label: str) -> bytes:
    response = session.get(url, headers={**_BROWSER_HEADERS, "Accept": "application/pdf,*/*;q=0.8"}, timeout=30)
    if response.status_code == 403:
        raise RuntimeError(
            f"{label}: CME returned HTTP 403 from this network. "
            "The relay will preserve the last accepted VPS bulletin and retry later."
        )
    response.raise_for_status()
    raw = response.content
    _validate_pdf(raw, label)
    return raw


def _pair_fingerprint(options_raw: bytes, futures_raw: bytes) -> str:
    h = hashlib.sha256()
    h.update(hashlib.sha256(options_raw).digest())
    h.update(hashlib.sha256(futures_raw).digest())
    return h.hexdigest()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(fingerprint: str, result: dict) -> None:
    state = {
        "pair_sha256": fingerprint,
        "bulletin_date": (result.get("data") or {}).get("bulletin_date"),
        "bulletin_status": (result.get("data") or {}).get("bulletin_status"),
        "updated_at_epoch": int(time.time()),
    }
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def run_once() -> dict:
    """Fetch both official bulletins and relay them once. Raises on failure."""
    _validate_config()

    with requests.Session() as session:
        # Best-effort warm-up to establish ordinary CME web cookies. Failure is
        # intentionally ignored because some CME edge configurations permit the
        # PDF while denying the HTML page (or vice versa).
        try:
            session.get(CME_DAILY_PAGE, headers={**_BROWSER_HEADERS, "Accept": "text/html,*/*;q=0.8"}, timeout=15)
        except Exception:
            pass

        options_raw = _download(session, OPTIONS_URL, "Section64 Metals Options")
        futures_raw = _download(session, FUTURES_URL, "Section62 Metals Futures")

    fingerprint = _pair_fingerprint(options_raw, futures_raw)
    state = _load_state()
    if state.get("pair_sha256") == fingerprint:
        result = {
            "status": "UNCHANGED",
            "bulletin_date": state.get("bulletin_date"),
            "bulletin_status": state.get("bulletin_status"),
            "pair_sha256": fingerprint,
        }
        log.info("CME bulletin pair unchanged; relay skipped (%s)", fingerprint[:12])
        return result

    payload = {
        "source": f"home:{DAEMON_ID}"[:64],
        "options_pdf_b64": base64.b64encode(options_raw).decode("ascii"),
        "futures_pdf_b64": base64.b64encode(futures_raw).decode("ascii"),
    }
    headers = {
        "X-Bridge-Secret": BRIDGE_SECRET,
        "X-Bridge-Daemon-Id": DAEMON_ID,
        "Content-Type": "application/json",
        "User-Agent": "xauusd-cme-relay/1.0",
    }
    relay_url = f"{DASHBOARD_URL}/api/v1/cme/bulletin-relay"
    response = requests.post(relay_url, headers=headers, json=payload, timeout=90)
    if not response.ok:
        detail = (response.text or "")[:500]
        raise RuntimeError(f"VPS relay HTTP {response.status_code}: {detail}")

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"VPS relay did not confirm acceptance: {result}")
    _save_state(fingerprint, result)

    data = result.get("data") or {}
    log.info(
        "CME relay accepted date=%s status=%s options_rows=%s futures_rows=%s reconciliation=%s",
        data.get("bulletin_date"),
        data.get("bulletin_status"),
        data.get("options_rows_written"),
        data.get("futures_rows_written"),
        data.get("reconciliation_rows"),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay CME Metals daily bulletins from HOME to VPS")
    parser.add_argument("--loop", action="store_true", help="run continuously at CME_RELAY_INTERVAL_SEC cadence")
    args = parser.parse_args()

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            log.warning("CME relay attempt failed: %s", exc)
            if not args.loop:
                return 1

        if not args.loop:
            return 0
        try:
            time.sleep(INTERVAL_SEC)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
