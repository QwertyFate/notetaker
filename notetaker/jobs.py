"""A recording job: join -> record -> WAV -> Whisper transcript -> Claude notes.

Status lives in generated/<id>/status.json and moves through
    [scheduled,] queued, joining, waiting_for_admission, recording,
    waiting_for_transcription, transcribing, summarizing, done
or ends in failed (or cancelled, for a scheduled bot called off). "done" means processing
finished; a step that could not run leaves transcript_error or notes_error next to whatever it
did produce.

Who does what:
    worker process (one per meeting)  joins, records, writes the WAV, hands off as
                                      waiting_for_transcription, exits
    transcriber process (one)         transcribes + writes notes, oldest recording first
    API process                       schedules bots, restarts the transcriber, sends CRM callbacks
The CLI does all of it in one process.

generated/.active/ holds one empty file per job that still needs attention (not finished, or a
CRM callback not yet delivered), so the background loops don't re-read every job ever made.
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from meeto.state_store.base import MeetingLifecycleStore

from notetaker.audio import pcm16_to_wav
from notetaker.backends import meet_meeto

_logger = logging.getLogger(__name__)

OUTPUT_DIR = meet_meeto.OUTPUT_DIR
ACTIVE_DIR = OUTPUT_DIR / ".active"
RECORDINGS_DIR = Path("recordings")
WAITING = "waiting_for_transcription"
TERMINAL = {"done", "failed", "cancelled"}
IN_CALL = {"queued", "joining", "waiting_for_admission", "recording"}
TRANSCRIBING = {"transcribing", "summarizing"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def status_path(job_id: str) -> Path:
    return OUTPUT_DIR / job_id / "status.json"


def read_status(job_id: str) -> Optional[dict]:
    p = status_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def needs_attention(doc: dict) -> bool:
    """Still running, or finished but its CRM callback hasn't been delivered (or given up on)."""
    status = doc.get("status")
    if status not in TERMINAL:
        return True
    if status == "cancelled" or not doc.get("callback_url"):
        return False
    webhook = doc.get("webhook") or {}
    return not webhook.get("delivered_at") and not webhook.get("gave_up")


def _update_active_marker(doc: dict) -> None:
    marker = ACTIVE_DIR / doc["id"]
    if needs_attention(doc):
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)


def write_status(job_id: str, **fields) -> dict:
    """Merge fields into the job's status.json and return the new document."""
    p = status_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = read_status(job_id) or {"id": job_id}
    doc.update(fields)
    doc["updated_at"] = now_iso()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, p)
    _update_active_marker(doc)
    return doc


def list_jobs() -> list[dict]:
    """Every job ever made, newest first. Reads every status file: use for listings only."""
    docs = [read_status(d.name) for d in OUTPUT_DIR.glob("*") if d.is_dir() and not d.name.startswith(".")]
    docs = [d for d in docs if d]
    return sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)


def list_active_jobs() -> list[dict]:
    """Jobs that still need attention, newest first. Cheap: reads only those."""
    if not ACTIVE_DIR.exists():
        return []
    docs = []
    for marker in ACTIVE_DIR.iterdir():
        doc = read_status(marker.name)
        if doc is None or not needs_attention(doc):
            marker.unlink(missing_ok=True)  # stale marker
            continue
        docs.append(doc)
    return sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)


def rebuild_active_index() -> int:
    """Recreate generated/.active from the status files (at API start). Returns the count."""
    count = 0
    for doc in list_jobs():
        _update_active_marker(doc)
        count += needs_attention(doc)
    return count


class JsonStatusStore(MeetingLifecycleStore):
    """meeto lifecycle store that mirrors status into status.json."""

    def update_status(self, meeting_id, *, status, ended_at=None, **_ignored):
        fields = {"status": status}
        if status == "recording":
            # Lines Whisper's timestamps up with meeto's speaker events later.
            fields["recording_started_epoch"] = time.time()
        if ended_at:
            fields["ended_at"] = datetime.fromtimestamp(ended_at, timezone.utc).isoformat()
        write_status(meeting_id, **fields)

    def heartbeat(self, meeting_id):
        write_status(meeting_id, last_heartbeat=now_iso())


def _audio_to_wav(job_id: str, pcm: Path) -> dict:
    """Convert the raw capture to recordings/meet-<id>-<stamp>.wav; return the status fields."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    wav = RECORDINGS_DIR / f"meet-{job_id}-{stamp}.wav"
    seconds = pcm16_to_wav(pcm, wav)
    if wav.exists() and wav.stat().st_size > 44:
        pcm.unlink()  # the WAV holds the same samples; keeping both doubles disk use (115 MB/hour)
    return {"wav": str(wav), "duration_seconds": round(seconds, 1), "size_bytes": wav.stat().st_size,
            "audio_ready_at": now_iso()}


def salvage_or_fail(job_id: str, reason: str) -> dict:
    """A job's worker died. Keep whatever audio it captured (queued for transcription, marked
    partial), or fail the job if there is none. The API sends the CRM callback either way."""
    doc = read_status(job_id) or {}
    if doc.get("wav") and Path(doc["wav"]).exists():
        return write_status(job_id, status=WAITING, audio_ready_at=doc.get("audio_ready_at") or now_iso())
    pcm = meet_meeto.find_pcm(job_id)
    if pcm is not None and pcm.stat().st_size >= 32000:  # at least one second of audio
        _logger.warning("job %s: worker died (%s); keeping %d bytes of partial audio", job_id, reason, pcm.stat().st_size)
        return write_status(job_id, status=WAITING, partial=True, error=f"partial recording: {reason}",
                            **_audio_to_wav(job_id, pcm))
    return write_status(job_id, status="failed", error=reason)


async def run_job_async(job_id: str, url: str, name: str = "Notetaker", max_minutes: int = 120,
                        queue: bool = False) -> dict:
    """Record one meeting and convert the audio to WAV.

    queue=True hands the WAV to the transcriber (status waiting_for_transcription);
    queue=False leaves it for the caller to transcribe in-process (status transcribing).
    If the task is cancelled (the API's DELETE), meeto closes the browser and whatever
    audio was captured is still converted.
    """
    write_status(job_id, url=url, name=name, max_minutes=max_minutes, pid=os.getpid(),
                 mode="authenticated" if meet_meeto.storage_state_path() else "guest",
                 started_at=now_iso())
    interrupted = False
    pcm = None
    try:
        job = read_status(job_id) or {}
        _, pcm = await meet_meeto.record_async(
            url, name=name, max_minutes=max_minutes, meeting_id=job_id, state_store=JsonStatusStore(),
            chat_message=job["chat_message"] if "chat_message" in job else None,
            on_chat_result=lambda result: write_status(job_id, chat_notice=result))
    except (asyncio.CancelledError, KeyboardInterrupt):
        interrupted = True
        pcm = meet_meeto.find_pcm(job_id)
    except Exception as err:
        first_line = str(err).splitlines()[0] if str(err) else type(err).__name__
        _logger.exception("job %s failed", job_id)
        return write_status(job_id, status="failed", error=f"could not join the meeting ({first_line})")

    if pcm is None or pcm.stat().st_size == 0:
        error = ("stopped before any audio was captured" if interrupted
                 else "no audio captured (bot was not admitted, or the meeting ended first)")
        return write_status(job_id, status="failed", error=error, stopped_early=interrupted)

    return write_status(job_id, status=WAITING if queue else "transcribing", stopped_early=interrupted,
                        **_audio_to_wav(job_id, pcm))


def postprocess_job(job_id: str, with_notes: bool = True) -> dict:
    """Transcribe the job's WAV and write notes in this process (CLI path)."""
    from notetaker.postprocess import process_recording  # loads Whisper/Claude only when needed

    doc = read_status(job_id) or {}
    if not doc.get("wav"):
        return doc
    try:
        result = process_recording(Path(doc["wav"]), job_id=job_id, with_notes=with_notes,
                                   on_stage=lambda stage: write_status(job_id, status=stage))
    except KeyboardInterrupt:
        write_status(job_id, status="done", transcript_error="interrupted before transcription finished")
        raise
    return write_status(job_id, status="done", **result)


def run_job(job_id: str, url: str, name: str = "Notetaker", max_minutes: int = 120,
            transcribe: bool = True, with_notes: bool = True, queue: bool = False) -> dict:
    """Blocking: record (Ctrl-C/SIGTERM leaves the call and keeps captured audio), then either
    hand the WAV to the transcriber (queue=True, the API's workers) or transcribe it here (CLI)."""
    async def _main():
        task = asyncio.ensure_future(run_job_async(job_id, url, name=name, max_minutes=max_minutes, queue=queue))
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        return await task

    doc = asyncio.run(_main())
    if queue or doc.get("status") != "transcribing":
        return doc
    if not transcribe:
        return write_status(job_id, status="done")
    return postprocess_job(job_id, with_notes=with_notes)
