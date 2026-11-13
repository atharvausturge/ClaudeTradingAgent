"""
Run-once-per-day guard for the market-hours jobs.

GitHub's cron fires hours late, so each workflow schedules several attempts
across the trading day. Every attempt calls `should_run`; only the first one
that lands while the market is open (and after `earliest_et`, if given) does
the work. `mark_ran` records the ET date in memory.json so later attempts skip.

Set FORCE_RUN=1 (manual "Run workflow") to bypass the once-per-day check.
The market-open check is never bypassed.
"""
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def today_et(now=None) -> str:
    return (now or datetime.now(ET)).strftime("%Y-%m-%d")


def should_run(job: str, memory: dict, clock, now=None, earliest_et: dtime | None = None) -> bool:
    now = now or datetime.now(ET)
    if not clock.is_open:
        log.info(f"[{job}] market closed — skipping this attempt")
        return False
    if earliest_et and now.time() < earliest_et:
        log.info(f"[{job}] before {earliest_et.strftime('%H:%M')} ET — skipping this attempt")
        return False
    forced = os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes")
    if not forced and memory.get("last_runs", {}).get(job) == today_et(now):
        log.info(f"[{job}] already ran today ({today_et(now)}) — skipping")
        return False
    return True


def mark_ran(job: str, memory: dict, now=None) -> None:
    memory.setdefault("last_runs", {})[job] = today_et(now)
