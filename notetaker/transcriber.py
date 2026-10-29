"""The transcriber: one background process that turns finished recordings into transcripts and notes.

    python -m notetaker.transcriber      (the API starts it when a recording is waiting)

- Takes jobs in status waiting_for_transcription, oldest recording first, one at a time.
- Runs at low CPU priority, so bots that are still recording always get the CPU first.
- Runs only while there is work: it exits after IDLE_EXIT_S with an empty queue, freeing all
  its memory, and the API starts it again when the next recording arrives.
- If it is stopped or crashes mid-job, the job goes back into the queue and is redone on the
  next start; a job that breaks it MAX_ATTEMPTS times is finished with a transcript_error.
- Only one runs at a time (a lock file); a second copy exits straight away.
- It never sends CRM callbacks; the API does that once a job is done.
"""

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from notetaker import jobs
from notetaker.config import PROJECT_ROOT

_logger = logging.getLogger("notetaker.transcriber")

LOCK_PATH = jobs.OUTPUT_DIR / ".transcriber.lock"
PID_PATH = jobs.OUTPUT_DIR / ".transcriber.pid"
POLL_S = 5
IDLE_EXIT_S = 60
MAX_ATTEMPTS = 3
NICENESS = 10


def _alive(pid: Optional[int], marker: str = "notetaker") -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return marker in cmd


def recover() -> None:
    """Put jobs whose transcription was cut off (transcriber or CLI gone) back in the queue."""
    me = os.getpid()
    for doc in jobs.list_active_jobs():
        if doc.get("status") not in jobs.TRANSCRIBING:
            continue
        owner = doc.get("transcriber_pid") or doc.get("pid")
        if owner == me or _alive(owner):
            continue
        _logger.warning("requeueing %s: its transcription was interrupted", doc["id"])
        jobs.write_status(doc["id"], status=jobs.WAITING, transcriber_pid=None)


def claim_next() -> Optional[dict]:
    waiting = [d for d in jobs.list_active_jobs() if d.get("status") == jobs.WAITING]
    if not waiting:
        return None
    doc = min(waiting, key=lambda d: d.get("audio_ready_at") or d.get("created_at") or "")
    return jobs.write_status(doc["id"], status="transcribing", transcriber_pid=os.getpid(),
                             transcription_started_at=jobs.now_iso(),
                             transcribe_attempts=doc.get("transcribe_attempts", 0) + 1)


def process(doc: dict) -> dict:
    from notetaker.postprocess import process_recording

    job_id = doc["id"]
    if doc["transcribe_attempts"] > MAX_ATTEMPTS:
        return jobs.write_status(job_id, status="done", transcriber_pid=None,
                                 transcript_error=f"transcription failed {MAX_ATTEMPTS} times; see generated/transcriber.log")
    wav = Path(doc.get("wav") or "")
    if not wav.exists():
        return jobs.write_status(job_id, status="done", transcriber_pid=None,
                                 transcript_error=f"audio file missing: {wav}")
    _logger.info("transcribing %s (%s s of audio, attempt %d)", job_id, doc.get("duration_seconds"),
                 doc["transcribe_attempts"])
    try:
        result = process_recording(wav, job_id=job_id, with_notes=doc.get("with_notes", True),
                                   on_stage=lambda stage: jobs.write_status(job_id, status=stage))
    except KeyboardInterrupt:
        # Shutdown, not a failure: back into the queue without using up an attempt.
        jobs.write_status(job_id, status=jobs.WAITING, transcriber_pid=None,
                          transcribe_attempts=doc["transcribe_attempts"] - 1)
        raise
    done = jobs.write_status(job_id, status="done", transcriber_pid=None,
                             transcription_finished_at=jobs.now_iso(), **result)
    _logger.info("finished %s: transcript=%s notes=%s", job_id, bool(result.get("transcript")),
                 bool(result.get("notes")) or result.get("notes_error"))
    return done


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    os.chdir(PROJECT_ROOT)
    jobs.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _logger.info("another transcriber is already running; exiting")
        return 0
    os.nice(NICENESS)
    PID_PATH.write_text(str(os.getpid()))

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)

    _logger.info("transcriber running (pid %d, nice %d)", os.getpid(), os.getpriority(os.PRIO_PROCESS, 0))
    recover()
    idle_since = time.monotonic()
    try:
        while True:
            doc = claim_next()
            if doc:
                process(doc)
                idle_since = time.monotonic()
                continue
            if time.monotonic() - idle_since > IDLE_EXIT_S:
                _logger.info("queue empty for %d s; exiting (started again when a recording arrives)", IDLE_EXIT_S)
                return 0
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        _logger.info("transcriber stopping")
        return 0
    finally:
        PID_PATH.unlink(missing_ok=True)


def has_work() -> bool:
    """A recording is waiting, or a transcription was cut off and needs redoing."""
    return any(d.get("status") == jobs.WAITING or d.get("status") in jobs.TRANSCRIBING
               for d in jobs.list_active_jobs())


def ensure_running(state: dict) -> None:
    """Called by the API every few seconds: start the transcriber if there is work and none runs."""
    proc = state.get("proc")
    if proc is not None and proc.poll() is None:
        return
    try:
        if _alive(int(PID_PATH.read_text()), "notetaker.transcriber"):
            return  # one from an earlier API run is still going
    except (FileNotFoundError, ValueError):
        pass
    if not has_work():
        return
    if proc is not None and proc.returncode not in (0, None):
        _logger.warning("transcriber exited with code %s; starting it again", proc.returncode)
    log = open(jobs.OUTPUT_DIR / "transcriber.log", "ab")
    state["proc"] = subprocess.Popen([sys.executable, "-m", "notetaker.transcriber"], cwd=PROJECT_ROOT,
                                     stdout=log, stderr=subprocess.STDOUT)


if __name__ == "__main__":
    raise SystemExit(main())
