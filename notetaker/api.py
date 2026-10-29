"""HTTP API for requesting a notetaker. Listens on localhost only (see start.sh).

    ./start.sh

CRM route (records, transcribes, writes notes, then POSTs everything to the CRM's callback_url):
    POST   /crm/recordings                    {"meeting_url", "callback_url", "join_at", "external_id", "metadata", ...}
    GET    /crm/health                        can a bot join now (or ?at=...)? 200 ready / 503 not
    POST   /crm/transcribe                    upload an audio file (MP3, WAV, M4A, ...) -> its transcript
    GET    /crm/recordings                    CRM jobs (?status=scheduled, ?external_id=...)
    GET    /crm/recordings/{id}               current state, same shape as the callback
    GET    /crm/recordings/{id}/audio         the WAV
    DELETE /crm/recordings/{id}               cancel a scheduled bot, or pull one out of the call
    POST   /crm/recordings/{id}/redeliver     send the callback again
  Requires "Authorization: Bearer <NOTETAKER_CRM_TOKEN>" when that is set in .env.

Testing routes:
    GET  /record/abc-defg-hij                 start a bot for that Meet code (or a full Meet link)
    POST /record  {"url": "...", "name": "Notetaker", "max_minutes": 120}
    GET  /recordings                          list jobs
    GET  /recordings/{id}                     status: scheduled, queued, joining, waiting_for_admission,
                                              recording, waiting_for_transcription, transcribing,
                                              summarizing, done, failed, cancelled
    GET  /recordings/{id}/audio               the WAV, from status transcribing onwards
    GET  /recordings/{id}/transcript          Whisper transcript JSON (?format=text for plain text)
    GET  /recordings/{id}/notes               Claude meeting notes JSON (?format=markdown)
    DELETE /recordings/{id}                   pull the bot out of the call early
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from notetaker import bootstrap, health, jobs, scheduler, transcriber, webhooks
from notetaker.backends import meet_meeto


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # uvicorn only configures its own loggers; show ours in the console and logs/api.log too.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    # First start on a new machine: fetch what pip can't install. Later starts: ~0.5 s.
    bootstrap.ensure_browser()
    bootstrap.download_whisper_model_in_background()
    active = jobs.rebuild_active_index()
    logging.getLogger("notetaker.api").info("%d job(s) need attention", active)
    health.start_periodic_live_check()
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()


async def _housekeeping():
    """Every few seconds: launch scheduled bots that are due, keep the transcriber running,
    salvage jobs whose worker died, and send CRM callbacks for finished jobs."""
    while True:
        try:
            await asyncio.to_thread(scheduler.run_due_jobs, _launch)
            await asyncio.to_thread(transcriber.ensure_running, _transcriber)
            await asyncio.to_thread(scheduler.reap_dead_workers, _procs)
            await asyncio.to_thread(webhooks.deliver_pending)
            _state["housekeeping_last_run"] = time.time()
        except Exception:
            logging.getLogger("notetaker.api").exception("housekeeping failed")
        await asyncio.sleep(scheduler.INTERVAL_SECONDS)


app = FastAPI(title="Notetaker", version="0.1", lifespan=lifespan)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_procs: dict[str, subprocess.Popen] = {}
_transcriber: dict = {}  # {"proc": Popen} of the transcriber this API started
_state: dict = {"housekeeping_last_run": None}
_MEET_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")


class RecordRequest(BaseModel):
    url: str
    name: str = "Notetaker"
    max_minutes: int = 120


def _to_meet_url(link: str) -> str:
    link = link.strip()
    if _MEET_CODE.match(link):
        return f"https://meet.google.com/{link}"
    # Browsers and proxies may collapse "https://" to "https:/" inside a path.
    link = re.sub(r"^https?:/+", "https://", link)
    return link


def _validated_meet_url(link: str) -> str:
    try:
        return meet_meeto.validate_url(_to_meet_url(link))
    except meet_meeto.UnsupportedMeetingUrl as err:
        raise HTTPException(status_code=400, detail=str(err))


def _new_job(url: str, name: str, max_minutes: int, request: Request, *, status: str = "queued",
             with_notes: bool = True, extra: Optional[dict] = None) -> str:
    """Create the job's status.json. Returns the job id."""
    job_id = uuid.uuid4().hex[:12]
    jobs.write_status(job_id, status=status, url=url, name=name, max_minutes=max_minutes,
                      with_notes=with_notes, created_at=datetime.now(timezone.utc).isoformat(),
                      public_base_url=str(request.base_url).rstrip("/"), **(extra or {}))
    return job_id


def _launch(job_id: str) -> None:
    """Start the worker process for a job, using the settings stored in its status.json."""
    doc = jobs.read_status(job_id)
    cmd = [sys.executable, "-m", "notetaker.worker", job_id, doc["url"], "--name", doc["name"],
           "--max-minutes", str(doc["max_minutes"])] + ([] if doc.get("with_notes", True) else ["--no-notes"])
    log = (jobs.OUTPUT_DIR / job_id / "worker.log").open("ab")
    _procs[job_id] = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT)


def _start(url: str, name: str, max_minutes: int, request: Request) -> dict:
    url = _validated_meet_url(url)
    job_id = _new_job(url, name, max_minutes, request)
    _launch(job_id)
    base = str(request.base_url).rstrip("/")
    return {"id": job_id, "status": "queued", "url": url,
            "status_url": f"{base}/recordings/{job_id}",
            "audio_url": f"{base}/recordings/{job_id}/audio",
            "transcript_url": f"{base}/recordings/{job_id}/transcript",
            "notes_url": f"{base}/recordings/{job_id}/notes"}


@app.get("/record/{link:path}", status_code=202)
def record_get(link: str, request: Request, name: str = "Notetaker", max_minutes: int = 120):
    return _start(_to_meet_url(link), name, max_minutes, request)


@app.post("/record", status_code=202)
def record_post(body: RecordRequest, request: Request):
    return _start(_to_meet_url(body.url), body.name, body.max_minutes, request)


def _status(job_id: str) -> dict:
    doc = jobs.read_status(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="unknown recording id")
    proc = _procs.get(job_id)
    if doc.get("status") in scheduler.ACTIVE and proc is not None and proc.poll() is not None:
        # The worker is gone but the job never finished; close it (and tell the CRM) now.
        scheduler.finish_dead_job(job_id, f"worker exited with code {proc.returncode}; see worker.log")
        doc = jobs.read_status(job_id)
    return doc


@app.get("/recordings")
def list_recordings():
    return [_status(d["id"]) for d in jobs.list_jobs()]


@app.get("/recordings/{job_id}")
def get_recording(job_id: str):
    return _status(job_id)


def _artifact(doc: dict, field: str, error_field: str, what: str) -> Path:
    """Path of a finished artifact, or the HTTP error that explains why it isn't there."""
    if doc.get(field):
        return PROJECT_ROOT / doc[field]
    if doc.get(error_field):
        raise HTTPException(status_code=424, detail=doc[error_field])
    if doc.get("status") in ("done", "failed"):
        raise HTTPException(status_code=404, detail=f"no {what} for this recording ({doc.get('error') or 'skipped'})")
    raise HTTPException(status_code=409, detail=f"{what} not ready, status is {doc.get('status')}")


@app.get("/recordings/{job_id}/audio")
def get_audio(job_id: str):
    doc = _status(job_id)
    wav = _artifact(doc, "wav", "error", "audio")
    return FileResponse(wav, media_type="audio/wav", filename=wav.name)


@app.get("/recordings/{job_id}/transcript")
def get_transcript(job_id: str, format: str = "json"):
    doc = _status(job_id)
    if format == "text":
        return PlainTextResponse(_artifact(doc, "transcript_text", "transcript_error", "transcript").read_text())
    return FileResponse(_artifact(doc, "transcript", "transcript_error", "transcript"), media_type="application/json")


@app.get("/recordings/{job_id}/notes")
def get_notes(job_id: str, format: str = "json"):
    doc = _status(job_id)
    if format == "markdown":
        return PlainTextResponse(_artifact(doc, "notes_markdown", "notes_error", "notes").read_text(),
                                 media_type="text/markdown")
    return FileResponse(_artifact(doc, "notes", "notes_error", "notes"), media_type="application/json")


@app.delete("/recordings/{job_id}")
def stop_recording(job_id: str):
    return _stop(job_id)


def _stop(job_id: str) -> dict:
    doc = _status(job_id)
    proc = _procs.get(job_id)
    # Only a bot that is still in (or on its way into) the call can be pulled out; once it is
    # transcribing, the call is over and stopping would only throw the transcript away.
    if doc.get("status") not in jobs.IN_CALL or proc is None or proc.poll() is not None:
        return {"id": job_id, "status": doc.get("status"), "stopped": False}
    os.kill(proc.pid, signal.SIGTERM)
    threading.Thread(target=_force_kill_after, args=(job_id, proc, STOP_GRACE_SECONDS), daemon=True).start()
    return {"id": job_id, "status": "stopping", "stopped": True}


STOP_GRACE_SECONDS = 30


def _force_kill_after(job_id: str, proc: subprocess.Popen, grace: float) -> None:
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        scheduler.finish_dead_job(job_id, "stop requested; worker did not exit in time and was killed")


# ---------------------------------------------------------------------------------------------
# CRM route: request a bot now or for a scheduled meeting; transcript + notes are POSTed back to
# callback_url when done.
# ---------------------------------------------------------------------------------------------

def _crm_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """When NOTETAKER_CRM_TOKEN is set, only callers presenting it may use /crm/*."""
    token = os.environ.get("NOTETAKER_CRM_TOKEN")
    if not token:
        return
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="missing or wrong Authorization: Bearer token")


crm = APIRouter(prefix="/crm", tags=["crm"], dependencies=[Depends(_crm_auth)])
DUPLICATE_WINDOW = timedelta(minutes=30)


class CrmRecordRequest(BaseModel):
    meeting_url: str = Field(description="Google Meet link or code, e.g. https://meet.google.com/abc-defg-hij")
    callback_url: str = Field(description="Where the result is POSTed when the job ends")
    join_at: Optional[datetime] = Field(
        default=None, description="Meeting start with timezone, e.g. 2026-10-21T14:00:00+02:00. Omit to join now.")
    external_id: Optional[str] = Field(
        default=None, max_length=200,
        description="The CRM's own meeting id. Sending it again updates that meeting's scheduled bot.")
    metadata: dict[str, Any] = Field(default_factory=dict,
                                     description="Your own ids (deal, contact, user); echoed back unchanged")
    bot_name: str = "Notetaker"
    max_minutes: int = Field(default=120, ge=1, le=240)
    include_notes: bool = Field(default=True, description="false: transcript only, no Claude notes")
    chat_message: Optional[str] = Field(
        default=None, max_length=500,
        description="Recording notice the bot posts and pins in the meeting chat. Omit for the default "
                    "(NOTETAKER_CHAT_MESSAGE), \"\" for no notice.")


def _crm_doc(job_id: str) -> dict:
    doc = _status(job_id)
    if not doc.get("callback_url"):
        raise HTTPException(status_code=404, detail="unknown CRM recording id")
    return doc


def _meet_code(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def _when(doc: dict) -> datetime:
    """When this job's bot is (or was) meant to be in the meeting."""
    return datetime.fromisoformat(doc.get("join_at") or doc.get("launched_at") or doc["created_at"])


def _find_existing(body: CrmRecordRequest, url: str, join_at: datetime) -> Optional[dict]:
    """A job this request is a repeat of: same CRM meeting id, or same Meet link at about the same time."""
    for doc in jobs.list_active_jobs():
        if not doc.get("callback_url") or doc.get("status") not in ({"scheduled"} | jobs.IN_CALL):
            continue
        if body.external_id:
            if doc.get("external_id") == body.external_id:
                return doc
            continue
        if _meet_code(doc.get("url")) != _meet_code(url):
            continue
        if doc["status"] in jobs.IN_CALL and join_at <= scheduler.now() + DUPLICATE_WINDOW:
            return doc  # a bot is already in (or joining) that call
        if doc["status"] == "scheduled" and abs(_when(doc) - join_at) <= DUPLICATE_WINDOW:
            return doc  # same meeting already scheduled; a recurring link at another time is not a repeat
    return None


@crm.post("/recordings", status_code=202)
def crm_create(body: CrmRecordRequest, request: Request, response: Response):
    url = _validated_meet_url(body.meeting_url)
    try:
        webhooks.check_callback_url(body.callback_url)
    except webhooks.CallbackNotAllowed as err:
        raise HTTPException(status_code=400, detail=str(err))
    now = scheduler.now()
    if body.join_at is None:
        join_at = now
    elif body.join_at.tzinfo is None:
        raise HTTPException(status_code=400, detail="join_at needs a timezone, e.g. 2026-10-21T14:00:00+02:00 or ...Z")
    else:
        join_at = body.join_at.astimezone(timezone.utc)
        if now - join_at > scheduler.missed_after():
            raise HTTPException(status_code=400, detail=f"join_at {body.join_at.isoformat()} is more than "
                                f"{int(scheduler.missed_after().total_seconds() // 60)} minutes in the past")
    launch_at = join_at - scheduler.lead()
    fields = {"url": url, "name": body.bot_name, "max_minutes": body.max_minutes, "with_notes": body.include_notes,
              "callback_url": body.callback_url, "metadata": body.metadata, "external_id": body.external_id,
              "join_at": join_at.isoformat() if body.join_at else None,
              "launch_at": launch_at.isoformat() if body.join_at else None}
    if body.chat_message is not None:
        fields["chat_message"] = body.chat_message

    with scheduler.lock:
        existing = _find_existing(body, url, join_at)
        if existing and existing["status"] == "scheduled" and body.external_id:
            # The CRM re-sent its meeting (moved, new link, new metadata): update the scheduled bot.
            jobs.write_status(existing["id"], **fields)
            if launch_at <= now:
                jobs.write_status(existing["id"], status="queued", launched_at=now.isoformat())
                _launch(existing["id"])
            response.status_code = 200
            return {**webhooks.build_payload(jobs.read_status(existing["id"])), "duplicate": False, "updated": True}
        if existing:
            # Two clicks for the same meeting must not send two bots.
            response.status_code = 200
            return {**webhooks.build_payload(existing), "duplicate": True, "updated": False}

        later = body.join_at is not None and launch_at > now
        job_id = _new_job(url, body.bot_name, body.max_minutes, request, status="scheduled" if later else "queued",
                          with_notes=body.include_notes,
                          extra={"source": "crm", **{k: v for k, v in fields.items()
                                                     if k not in ("url", "name", "max_minutes", "with_notes")}})
        if not later:
            jobs.write_status(job_id, launched_at=now.isoformat())
            _launch(job_id)
    return {**webhooks.build_payload(jobs.read_status(job_id)), "duplicate": False, "updated": False}


@crm.get("/health")
def crm_health(at: Optional[datetime] = None, minutes: int = 60, refresh: bool = False):
    """Can a recording bot join now (or at ?at=<ISO time with timezone>, for ?minutes=)?
    200 when ready, 503 when not; the body says why either way. ?refresh=true re-checks the
    bot's Google login live (about 2 s) instead of using the hourly result."""
    if at is not None and at.tzinfo is None:
        raise HTTPException(status_code=400, detail="at needs a timezone, e.g. 2026-10-21T14:00:00+02:00")
    if refresh:
        health.refresh_live_check()
    body = health.report(scheduler_last_run=_state["housekeeping_last_run"],
                         at=at.astimezone(timezone.utc) if at else None, minutes=minutes)
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


UPLOAD_DIR = jobs.OUTPUT_DIR / "uploads"


@crm.post("/transcribe")
async def crm_transcribe(file: UploadFile = File(...), language: Optional[str] = None, format: str = "json"):
    """Transcribe an uploaded audio file and answer with the transcript. Any common format works
    (MP3, WAV, M4A/AAC, OGG, FLAC, even a video's audio). Runs Whisper locally in its own
    low-priority process; the upload is deleted afterwards. Takes about 1 minute per 2.3 minutes
    of audio on an M4, longer while a meeting is being transcribed. ?language=de forces a
    language; ?format=text answers with plain text."""
    max_bytes = int(os.environ.get("NOTETAKER_MAX_UPLOAD_MB", "500")) * 1024 * 1024
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}{Path(file.filename or '').suffix[:10]}"
    size = 0
    try:
        with open(path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"file is larger than {max_bytes // (1024 * 1024)} MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="the uploaded file is empty")
        cmd = [sys.executable, "-m", "notetaker.transcribe", "--file", str(path)]
        if language:
            cmd += ["--language", language]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=PROJECT_ROOT, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, preexec_fn=lambda: os.nice(10))
        stdout, stderr = await proc.communicate()
    finally:
        path.unlink(missing_ok=True)
    try:
        result = json.loads(stdout.decode().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        logging.getLogger("notetaker.api").error("transcription failed: %s", stderr.decode()[-2000:])
        raise HTTPException(status_code=500, detail="transcription failed; see the server log")
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    if format == "text":
        return PlainTextResponse(result["text"])
    return {
        "filename": file.filename,
        "language": result["language"],
        "duration_seconds": result["duration"],
        "seconds_taken": result["seconds_taken"],
        "model": result["model"],
        "text": result["text"],
        "segments": result["segments"],
    }


@crm.get("/recordings")
def crm_list(status: Optional[str] = None, external_id: Optional[str] = None):
    """CRM jobs, newest first. Filter with ?status=scheduled or ?external_id=..."""
    out = []
    for doc in jobs.list_jobs():
        if not doc.get("callback_url"):
            continue
        if status and doc.get("status") != status or external_id and doc.get("external_id") != external_id:
            continue
        out.append(webhooks.build_payload(doc))
    return out


@crm.get("/recordings/{job_id}")
def crm_get(job_id: str):
    return webhooks.build_payload(_crm_doc(job_id))


@crm.get("/recordings/{job_id}/audio")
def crm_audio(job_id: str):
    wav = _artifact(_crm_doc(job_id), "wav", "error", "audio")
    return FileResponse(wav, media_type="audio/wav", filename=wav.name)


@crm.delete("/recordings/{job_id}")
def crm_stop(job_id: str):
    """Cancel a scheduled bot, or pull a bot that is in the call out of it."""
    _crm_doc(job_id)
    with scheduler.lock:
        doc = jobs.read_status(job_id)
        if doc.get("status") == "scheduled":
            jobs.write_status(job_id, status="cancelled", cancelled_at=scheduler.now().isoformat())
            return {"id": job_id, "status": "cancelled", "cancelled": True, "stopped": False}
    return {**_stop(job_id), "cancelled": False}


@crm.post("/recordings/{job_id}/redeliver")
def crm_redeliver(job_id: str):
    doc = _crm_doc(job_id)
    if doc.get("status") not in jobs.TERMINAL:
        raise HTTPException(status_code=409, detail=f"not finished yet, status is {doc.get('status')}")
    with webhooks._in_flight_lock:
        if job_id in webhooks._in_flight:
            raise HTTPException(status_code=409, detail="a delivery for this recording is already in progress")
        webhooks._in_flight.add(job_id)
    try:
        jobs.write_status(job_id, webhook={**(doc.get("webhook") or {}), "gave_up": False})
        delivered = webhooks.deliver(job_id, delays=(0,))
    finally:
        with webhooks._in_flight_lock:
            webhooks._in_flight.discard(job_id)
    return {"id": job_id, "delivered": delivered, "webhook": (jobs.read_status(job_id) or {}).get("webhook")}


app.include_router(crm)
