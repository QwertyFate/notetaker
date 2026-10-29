"""Scheduled bots and housekeeping. The API runs both every few seconds.

Scheduled job:  status "scheduled" with join_at (meeting start) and launch_at (when the bot
                leaves, NOTETAKER_JOIN_LEAD_SECONDS earlier, default 60). At launch_at the job
                becomes "queued" and its worker starts. If Notetaker wasn't running then and the
                meeting started more than NOTETAKER_MISSED_AFTER_MINUTES ago (default 30), the
                job fails as "missed" and the CRM is told.

Housekeeping:   a job whose recording worker is gone while it is still in the call (Terminal
                closed, crash, reboot) keeps whatever audio it captured: that goes to the
                transcriber as a partial recording. With no audio the job fails. The API's loop
                then sends the CRM callback. Without this such jobs stay "recording" forever.
"""

import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from notetaker import jobs

_logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 10
ACTIVE = jobs.IN_CALL  # states owned by a recording worker; the transcriber owns its own
lock = threading.Lock()  # guards status changes made by the API process (launch, cancel, reap)


def now() -> datetime:
    return datetime.now(timezone.utc)


def lead() -> timedelta:
    return timedelta(seconds=int(os.environ.get("NOTETAKER_JOIN_LEAD_SECONDS", "60")))


def missed_after() -> timedelta:
    return timedelta(minutes=int(os.environ.get("NOTETAKER_MISSED_AFTER_MINUTES", "30")))


def run_due_jobs(launch: Callable[[str], None]) -> None:
    t = now()
    for doc in jobs.list_active_jobs():
        if doc.get("status") != "scheduled" or datetime.fromisoformat(doc["launch_at"]) > t:
            continue
        with lock:
            doc = jobs.read_status(doc["id"]) or {}
            if doc.get("status") != "scheduled":
                continue
            if t - datetime.fromisoformat(doc["join_at"]) > missed_after():
                jobs.write_status(doc["id"], status="failed",
                                  error=f"missed: Notetaker was not running when the meeting started ({doc['join_at']})")
                _logger.warning("scheduled job %s missed (meeting started %s)", doc["id"], doc["join_at"])
                continue
            jobs.write_status(doc["id"], status="queued", launched_at=t.isoformat())
            launch(doc["id"])
            _logger.info("scheduled job %s launched for %s (meeting starts %s)", doc["id"], doc["url"], doc["join_at"])


def _worker_alive(pid: int, job_id: str) -> bool:
    """The pid exists and is still a Notetaker process for this job (pids get reused)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return f"notetaker.worker {job_id}" in cmd or "notetaker.cli" in cmd


def finish_dead_job(job_id: str, reason: str) -> None:
    """A job's recording worker is gone: keep its audio for the transcriber, or fail the job."""
    with lock:
        doc = jobs.read_status(job_id) or {}
        if doc.get("status") not in ACTIVE:
            return  # finished meanwhile
        doc = jobs.salvage_or_fail(job_id, reason)
    _logger.warning("job %s: %s -> %s", job_id, reason, doc.get("status"))


def reap_dead_workers(procs: dict) -> None:
    t = now()
    for doc in jobs.list_active_jobs():
        if doc.get("status") not in ACTIVE:
            continue
        job_id, proc, pid = doc["id"], procs.get(doc["id"]), doc.get("pid")
        if proc is not None and proc.poll() is None:
            continue
        if proc is not None:
            finish_dead_job(job_id, f"worker exited with code {proc.returncode}; see worker.log")
            continue
        if pid:
            if not _worker_alive(pid, job_id):
                finish_dead_job(job_id, "worker stopped unexpectedly (Notetaker restarted or Terminal closed)")
            continue
        # Never got as far as writing its pid; give a slow start two minutes.
        started = doc.get("launched_at") or doc.get("created_at")
        if started and t - datetime.fromisoformat(started) > timedelta(minutes=2):
            finish_dead_job(job_id, "worker never started")
