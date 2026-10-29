"""Send a finished recording (transcript + notes) back to the CRM that asked for it.

When a CRM job ends, the API POSTs one JSON document to the job's callback_url (the API is the
only process that sends callbacks; it also picks up undelivered ones after a restart):
    event "recording.completed"  the call was recorded; transcript/notes inline (or their errors)
    event "recording.failed"     no audio (bot not admitted, refused by Google, stopped before
                                 anything was captured, or a scheduled bot that was missed)
A cancelled scheduled bot sends nothing (the CRM cancelled it itself).

Headers on every delivery:
    X-Notetaker-Event       recording.completed | recording.failed
    X-Notetaker-Delivery    unique id per attempt
    X-Notetaker-Timestamp   unix seconds
    X-Notetaker-Signature   sha256=<hex HMAC of "<timestamp>.<body>"> when NOTETAKER_WEBHOOK_SECRET is set

Settings (.env):
    NOTETAKER_WEBHOOK_SECRET   shared secret for the signature (recommended)
    NOTETAKER_CALLBACK_HOSTS   hosts a callback_url may point at; default "localhost,127.0.0.1,::1"
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from notetaker import jobs
from notetaker.config import PROJECT_ROOT

_logger = logging.getLogger(__name__)

# Delays before each attempt: right away, then 5 s, 30 s, 2 min, 10 min (~13 min in total).
RETRY_DELAYS = (0, 5, 30, 120, 600)
TIMEOUT_SECONDS = 15


class CallbackNotAllowed(ValueError):
    pass


def allowed_hosts() -> set[str]:
    raw = os.environ.get("NOTETAKER_CALLBACK_HOSTS", "localhost,127.0.0.1,::1")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def check_callback_url(url: str) -> str:
    """Only http(s) URLs on allowed hosts: transcripts are customer conversations, and an open
    callback would let anyone who reaches the API send them anywhere."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CallbackNotAllowed(f"callback_url must be an http(s) URL, got {url!r}")
    if parsed.hostname.lower() not in allowed_hosts():
        raise CallbackNotAllowed(
            f"callback host {parsed.hostname!r} is not allowed; allowed: {sorted(allowed_hosts())}. "
            "Add it to NOTETAKER_CALLBACK_HOSTS in .env if it is your CRM."
        )
    return url


def _read_json(path: Optional[str]):
    if not path:
        return None
    p = PROJECT_ROOT / path
    return json.loads(p.read_text()) if p.exists() else None


def build_payload(doc: dict) -> dict:
    """The document the CRM receives (and what GET /crm/recordings/{id} returns)."""
    status = doc.get("status")
    transcript_doc = _read_json(doc.get("transcript"))
    notes_doc = _read_json(doc.get("notes"))
    notes_model = None
    if notes_doc is not None:
        notes_model = (notes_doc.pop("_claude", None) or {}).get("model")
    text_path = doc.get("transcript_text")
    base = doc.get("public_base_url", "").rstrip("/")
    if status == "done" and doc.get("wav"):
        event = "recording.completed"
    elif status == "cancelled":
        event = "recording.cancelled"
    elif status in jobs.TERMINAL:
        event = "recording.failed"
    elif status == "scheduled":
        event = "recording.scheduled"
    else:
        event = "recording.in_progress"
    return {
        "event": event,
        "id": doc["id"],
        "external_id": doc.get("external_id"),
        "status": status,
        "meeting_url": doc.get("url"),
        "join_at": doc.get("join_at"),
        "bot_leaves_at": doc.get("launch_at"),
        "bot_name": doc.get("name"),
        "metadata": doc.get("metadata") or {},
        "created_at": doc.get("created_at"),
        "ended_at": doc.get("ended_at"),
        "duration_seconds": doc.get("duration_seconds"),
        "language": doc.get("language"),
        "transcript": None if not transcript_doc else {
            "text": (PROJECT_ROOT / text_path).read_text() if text_path else None,
            "speakers_labelled": transcript_doc.get("speakers_labelled"),
            "turns": transcript_doc.get("turns"),
        },
        "notes": notes_doc,
        "notes_model": notes_model,
        "audio_url": f"{base}/crm/recordings/{doc['id']}/audio" if doc.get("wav") and base else None,
        "chat_notice": doc.get("chat_notice"),
        "errors": {
            "recording": doc.get("error"),
            "transcript": doc.get("transcript_error"),
            "notes": doc.get("notes_error"),
        },
    }


def sign(body: bytes, timestamp: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _post_once(url: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Notetaker-Webhook/1",
        "X-Notetaker-Event": payload["event"],
        "X-Notetaker-Delivery": uuid.uuid4().hex,
        "X-Notetaker-Timestamp": ts,
    }
    secret = os.environ.get("NOTETAKER_WEBHOOK_SECRET")
    if secret:
        headers["X-Notetaker-Signature"] = sign(body, ts, secret)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as err:
        return False, f"HTTP {err.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return False, f"{type(err).__name__}: {getattr(err, 'reason', err)}"


def deliver(job_id: str, delays: tuple = RETRY_DELAYS) -> bool:
    """POST the job's result to its callback_url, retrying with back-off. Records the outcome
    in status.json under "webhook". Returns True once the CRM answered 2xx."""
    doc = jobs.read_status(job_id) or {}
    url = doc.get("callback_url")
    if not url:
        return False
    payload = build_payload(doc)
    attempts, result = 0, ""
    for delay in delays:
        if delay:
            time.sleep(delay)
        attempts += 1
        ok, result = _post_once(url, payload)
        jobs.write_status(job_id, webhook={
            "url": url, "event": payload["event"], "attempts": attempts, "last_result": result,
            "delivered_at": datetime.now(timezone.utc).isoformat() if ok else None,
        })
        if ok:
            _logger.info("webhook for %s delivered to %s (%s, attempt %d)", job_id, url, result, attempts)
            return True
        _logger.warning("webhook for %s to %s failed (%s, attempt %d/%d)", job_id, url, result, attempts, len(delays))
    # Stop retrying on our own; POST /crm/recordings/{id}/redeliver can still send it.
    jobs.write_status(job_id, webhook={**(jobs.read_status(job_id) or {}).get("webhook", {}), "gave_up": True})
    return False


_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def deliver_async(job_id: str) -> None:
    """Deliver in a background thread, at most one delivery per job at a time."""
    with _in_flight_lock:
        if job_id in _in_flight:
            return
        _in_flight.add(job_id)

    def _run():
        try:
            deliver(job_id)
        except Exception:
            _logger.exception("webhook delivery for %s crashed", job_id)
        finally:
            with _in_flight_lock:
                _in_flight.discard(job_id)

    threading.Thread(target=_run, name=f"webhook-{job_id}", daemon=True).start()


def deliver_pending() -> None:
    """Send the callback for every finished CRM job that hasn't had one (run by the API loop)."""
    for doc in jobs.list_active_jobs():
        if doc.get("status") in ("done", "failed") and doc.get("callback_url"):
            deliver_async(doc["id"])
