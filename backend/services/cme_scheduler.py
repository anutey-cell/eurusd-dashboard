"""Autonomous CME bulletin refresh loop.

CME publishes the previous trade-date Preliminary bulletin around 00:00 CT
and Final around 10:00 CT. The loop wakes every 30 minutes and attempts one
refresh per publication window, retrying failures until the next window.

Market-intelligence only; no execution code is referenced here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_last_success_slot: str | None = None


def _slot(now: datetime) -> str | None:
    # September is CDT: 00:00 CT≈05:00 UTC, 10:00 CT≈15:00 UTC.
    # The loose windows also tolerate DST changes without missing a day.
    if now.hour >= 15:
        return f"{now.date().isoformat()}:FINAL_WINDOW"
    if now.hour >= 5:
        return f"{now.date().isoformat()}:PRELIM_WINDOW"
    return None


async def _loop() -> None:
    global _last_success_slot
    log.info("[cme_scheduler] started (30m cadence, PRELIM+FINAL windows)")
    # Give DB migrations / normal candle pipeline time to start first.
    await asyncio.sleep(45)
    while True:
        try:
            now = datetime.now(timezone.utc)
            slot = _slot(now)
            if slot and slot != _last_success_slot:
                from research.gold_intel.cme_live_refresh import refresh_cme_bulletins
                result = await asyncio.to_thread(refresh_cme_bulletins)
                if result.get("status") == "OK":
                    _last_success_slot = slot
                    log.info("[cme_scheduler] refresh OK slot=%s bulletin=%s %s",
                             slot, result.get("bulletin_date"), result.get("bulletin_status"))
                else:
                    log.warning("[cme_scheduler] refresh failed slot=%s detail=%s",
                                slot, result.get("detail"))
        except asyncio.CancelledError:
            log.info("[cme_scheduler] cancelled")
            raise
        except Exception as exc:
            log.warning("[cme_scheduler] iteration failed: %s", exc)
        await asyncio.sleep(1800)


async def start_cme_scheduler() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="cme-bulletin-refresh")


async def stop_cme_scheduler() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
