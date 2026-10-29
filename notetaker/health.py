"""Readiness checks behind GET /crm/health: can a bot join a meeting now (or at a given time)?

Blocking checks (any failing -> ready: false):
    bot_account   the bot's Google login exists, isn't expired, and Google still accepts it
                  (live check against Google, run at startup and hourly, ~2 s, cached)
    browser       Playwright's Chromium is installed
    capacity      bots in calls < NOTETAKER_MAX_BOTS (default 5) and enough free memory;
                  for a future time: bookings overlapping that slot < NOTETAKER_MAX_BOTS
    disk          at least NOTETAKER_MIN_FREE_GB (default 2) free for recordings
    scheduler     the API's background loop ran recently (it launches booked bots)
Informational (warnings only):
    transcription queue length, whether the Whisper model is on disk
    notes         whether a Claude API key is configured
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from notetaker import jobs
from notetaker.backends import meet_meeto
from notetaker.config import PROJECT_ROOT

_logger = logging.getLogger(__name__)

LIVE_CHECK_INTERVAL_S = 3600
MEMORY_MIN_FREE_PERCENT = 15


def max_bots() -> int:
    return int(os.environ.get("NOTETAKER_MAX_BOTS", "5"))


def min_free_gb() -> float:
    return float(os.environ.get("NOTETAKER_MIN_FREE_GB", "2"))


# --- bot Google login -------------------------------------------------------------------------

_live = {"ok": None, "checked_at": None, "detail": "not checked yet", "account": None}
_live_lock = threading.Lock()


def live_login_check() -> dict:
    """Open Google's account page with the bot's saved session. Signed in: the page stays on
    myaccount.google.com and shows the address. Signed out: Google redirects elsewhere."""
    from playwright.sync_api import sync_playwright

    state = meet_meeto.storage_state_path()
    result = {"checked_at": jobs.now_iso()}
    if not state:
        return {**result, "ok": False, "detail": "no saved bot login", "account": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_context(storage_state=state).new_page()
                page.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1000)
                url = page.url
                found = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", page.inner_text("body") or "")
            finally:
                browser.close()
    except Exception as err:
        return {**result, "ok": None, "account": None,
                "detail": f"could not reach Google to check the login ({type(err).__name__})"}
    if "myaccount.google.com" in url:
        return {**result, "ok": True, "account": found.group(0) if found else None,
                "detail": "Google accepts the bot's login"}
    return {**result, "ok": False, "account": None,
            "detail": "Google no longer accepts the bot's login; sign it in again (python -m notetaker.login)"}


def refresh_live_check() -> dict:
    with _live_lock:
        _live.update(live_login_check())
        _logger.info("bot login check: %s (%s)", _live["ok"], _live["detail"])
        return dict(_live)


def start_periodic_live_check() -> threading.Thread:
    def _loop():
        while True:
            try:
                refresh_live_check()
            except Exception:
                _logger.exception("bot login check failed")
            time.sleep(LIVE_CHECK_INTERVAL_S)
    t = threading.Thread(target=_loop, name="login-check", daemon=True)
    t.start()
    return t


def _check_bot_account() -> dict:
    state = meet_meeto.storage_state_path()
    if not state:
        return {"ok": False, "code": "bot_login_missing",
                "message": "The bot has no Google login; an admin must sign it in (python -m notetaker.login)."}
    try:
        cookies = json.loads(Path(state).read_text()).get("cookies", [])
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "code": "bot_login_missing", "message": "The bot's saved login file is unreadable."}
    expiry = [c.get("expires", -1) for c in cookies
              if c.get("name") in ("SID", "__Secure-1PSID") and "google.com" in c.get("domain", "")]
    expires_at = datetime.fromtimestamp(min(expiry), timezone.utc) if expiry and min(expiry) > 0 else None
    out = {"expires_at": expires_at.isoformat() if expires_at else None,
           "live_check": {k: _live[k] for k in ("ok", "checked_at", "detail", "account")}}
    if expires_at and expires_at < datetime.now(timezone.utc):
        return {**out, "ok": False, "code": "bot_login_expired",
                "message": "The bot's Google login has expired; an admin must sign it in again."}
    if _live["ok"] is False:
        return {**out, "ok": False, "code": "bot_login_rejected",
                "message": "Google no longer accepts the bot's login; an admin must sign it in again."}
    return {**out, "ok": True}


# --- other checks -----------------------------------------------------------------------------

def _check_browser() -> dict:
    import playwright

    spec = Path(playwright.__file__).parent / "driver/package/browsers.json"
    wanted = {b["name"]: b["revision"] for b in json.loads(spec.read_text())["browsers"]}
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "Library/Caches/ms-playwright")
    missing = [n for n, dirname in (("chromium", "chromium"), ("chromium-headless-shell", "chromium_headless_shell"))
               if n in wanted and not (root / f"{dirname}-{wanted[n]}").exists()]
    if missing:
        return {"ok": False, "code": "browser_missing", "missing": missing,
                "message": "The bot's browser is not installed; restarting Notetaker installs it."}
    return {"ok": True}


def _memory_free_percent() -> Optional[int]:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"free percentage:\s*(\d+)%", out)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _window(doc: dict) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(doc.get("join_at") or doc.get("launched_at") or doc["created_at"])
    return start, start + timedelta(minutes=int(doc.get("max_minutes") or 120))


def _check_capacity(active: list[dict], at: Optional[datetime], minutes: int) -> dict:
    limit = max_bots()
    if at is None:
        in_calls = sum(d.get("status") in jobs.IN_CALL for d in active)
        mem = _memory_free_percent()
        out = {"bots_in_calls": in_calls, "max_bots": limit, "memory_free_percent": mem}
        if in_calls >= limit:
            return {**out, "ok": False, "code": "capacity_full",
                    "message": f"All {limit} recording bots are busy right now; try again later."}
        if mem is not None and mem < MEMORY_MIN_FREE_PERCENT:
            return {**out, "ok": False, "code": "memory_low",
                    "message": "The recording server is low on memory right now; try again later."}
        return {**out, "ok": True}
    slot_end = at + timedelta(minutes=minutes)
    overlapping = sum(1 for d in active if d.get("status") in ({"scheduled"} | jobs.IN_CALL)
                      and _window(d)[0] < slot_end and at < _window(d)[1])
    out = {"at": at.isoformat(), "minutes": minutes, "bots_booked_in_slot": overlapping, "max_bots": limit}
    if overlapping >= limit:
        return {**out, "ok": False, "code": "capacity_full",
                "message": f"All {limit} recording bots are already booked at that time."}
    return {**out, "ok": True}


def _check_disk() -> dict:
    free_gb = round(shutil.disk_usage(PROJECT_ROOT).free / 1e9, 1)
    if free_gb < min_free_gb():
        return {"ok": False, "code": "disk_low", "free_gb": free_gb,
                "message": "The recording server is almost out of disk space."}
    return {"ok": True, "free_gb": free_gb}


def _check_scheduler(last_run: Optional[float]) -> dict:
    age = None if last_run is None else round(time.time() - last_run)
    if age is None or age > 60:
        return {"ok": False, "code": "scheduler_stalled", "last_run_seconds_ago": age,
                "message": "Notetaker's scheduler is not running; booked bots would not start."}
    return {"ok": True, "last_run_seconds_ago": age}


def _check_transcription(active: list[dict]) -> dict:
    from faster_whisper import download_model

    from notetaker.transcribe import DEFAULT_MODEL

    name = os.environ.get("NOTETAKER_WHISPER_MODEL", DEFAULT_MODEL)
    try:
        download_model(name, local_files_only=True)
        on_disk = True
    except Exception:
        on_disk = False
    queue = sum(d.get("status") == jobs.WAITING for d in active)
    out = {"ok": True, "model": name, "model_on_disk": on_disk, "queue": queue,
           "in_progress": sum(d.get("status") in jobs.TRANSCRIBING for d in active)}
    if not on_disk:
        out["warning"] = "The speech model is still downloading; the first transcript will take longer."
    return out


def _check_notes() -> dict:
    configured = bool(os.environ.get("ANTHROPIC_API_KEY"))
    out = {"ok": True, "claude_key_configured": configured}
    if not configured:
        out["warning"] = "No Claude API key: transcripts work, notes are skipped."
    return out


def report(*, scheduler_last_run: Optional[float], at: Optional[datetime] = None, minutes: int = 60) -> dict:
    active = jobs.list_active_jobs()
    checks = {
        "bot_account": _check_bot_account(),
        "browser": _check_browser(),
        "capacity": _check_capacity(active, at, minutes),
        "disk": _check_disk(),
        "scheduler": _check_scheduler(scheduler_last_run),
        "transcription": _check_transcription(active),
        "notes": _check_notes(),
    }
    problems = [{"check": k, "code": v["code"], "message": v["message"]} for k, v in checks.items() if not v["ok"]]
    warnings = [{"check": k, "message": v["warning"]} for k, v in checks.items() if v.get("warning")]
    ready = not problems
    return {
        "ready": ready,
        "message": "A recording bot can join this meeting." if ready else problems[0]["message"],
        "checked_at": jobs.now_iso(),
        "problems": problems,
        "warnings": warnings,
        "checks": checks,
    }
