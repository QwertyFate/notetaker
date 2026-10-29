"""Subprocess entrypoint used by the API, one per meeting:

    python -m notetaker.worker <job_id> <url> [--name N] [--max-minutes M] [--no-notes]

Joins and records the meeting, writes the WAV, and hands it to the transcriber (status
waiting_for_transcription). It then exits, so its memory is free while the transcript is made.
"""

import argparse
import logging

from notetaker import jobs
from notetaker.jobs import run_job

_logger = logging.getLogger("notetaker.worker")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("job_id")
    p.add_argument("url")
    p.add_argument("--name", default="Notetaker")
    p.add_argument("--max-minutes", type=int, default=120)
    p.add_argument("--no-notes", action="store_true")  # kept for compatibility; with_notes lives in status.json
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    # SIGTERM from the API cancels the bot task inside run_job: meeto closes the browser
    # and whatever audio was captured is still converted and queued.
    try:
        doc = run_job(args.job_id, args.url, name=args.name, max_minutes=args.max_minutes, queue=True)
    except BaseException as err:  # even a crash must keep captured audio or end the job
        _logger.exception("worker crashed")
        doc = jobs.read_status(args.job_id) or {}
        if doc.get("status") in jobs.IN_CALL:
            doc = jobs.salvage_or_fail(args.job_id, f"worker crashed: {type(err).__name__}: {err}")
    return 0 if doc.get("status") in (jobs.WAITING, "done") else 1


if __name__ == "__main__":
    raise SystemExit(main())
