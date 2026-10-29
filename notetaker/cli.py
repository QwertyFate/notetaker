"""Command line entrypoint.

    python -m notetaker.cli record https://meet.google.com/xxx-xxxx-xxx [--name Notetaker] [--max-minutes 120]
    python -m notetaker.cli process recordings/meet-<id>-<stamp>.wav [--no-notes]
"""

import argparse
import logging
import sys
import uuid
from pathlib import Path

from notetaker.backends import meet_meeto

from notetaker.jobs import run_job


def cmd_record(args: argparse.Namespace) -> int:
    try:
        meet_meeto.validate_url(args.url)
    except meet_meeto.UnsupportedMeetingUrl as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    from notetaker.bootstrap import ensure_browser
    ensure_browser()  # downloads the bot's browser on a fresh machine; instant afterwards

    print(f"A Chromium window will open and '{args.name}' will ask to join.", flush=True)
    print("Admit it from the Meet lobby. Recording stops when the meeting ends, "
          f"after {args.max_minutes} min, or on Ctrl-C.\n", flush=True)

    doc = run_job(uuid.uuid4().hex[:12], args.url, name=args.name, max_minutes=args.max_minutes,
                  transcribe=not args.no_transcribe, with_notes=not args.no_notes)
    if doc["status"] != "done" or not doc.get("wav"):
        print(f"error: {doc.get('error')}", file=sys.stderr)
        print("hint: look at generated/<id>/screenshots/ for what the bot saw.", file=sys.stderr)
        return 1
    note = " (stopped early with Ctrl-C)" if doc.get("stopped_early") else ""
    print(f"\nSaved {doc['wav']}  {doc['size_bytes'] / 1_000_000:.1f} MB  "
          f"{doc['duration_seconds'] / 60:.1f} min{note}")
    _print_outputs(doc)
    return 0


def _print_outputs(result: dict) -> None:
    for label, key, err in (("Transcript", "transcript_text", "transcript_error"),
                            ("Notes", "notes_markdown", "notes_error")):
        if result.get(key):
            print(f"{label}: {result[key]}")
        elif result.get(err):
            print(f"{label}: skipped, {result[err]}", file=sys.stderr)


def cmd_process(args: argparse.Namespace) -> int:
    from notetaker.postprocess import process_recording

    wav = Path(args.wav)
    if not wav.exists():
        print(f"error: {wav} not found", file=sys.stderr)
        return 1
    print(f"Transcribing {wav} with Whisper (first run downloads the model)...", flush=True)
    result = process_recording(wav, with_notes=not args.no_notes,
                               on_stage=lambda stage: print(f"... {stage}", flush=True))
    _print_outputs(result)
    return 1 if result.get("transcript_error") else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="notetaker", description="Send a recording bot to a meeting.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Join a Google Meet and save the audio as WAV.")
    rec.add_argument("url", help="Google Meet link, e.g. https://meet.google.com/abc-defg-hij")
    rec.add_argument("--name", default="Notetaker", help="Display name the bot uses in the meeting")
    rec.add_argument("--max-minutes", type=int, default=120, help="Stop recording after this many minutes")
    rec.add_argument("--no-transcribe", action="store_true", help="Only save the WAV")
    rec.add_argument("--no-notes", action="store_true", help="Transcribe, but skip the Claude notes")
    rec.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    rec.set_defaults(func=cmd_record)

    proc = sub.add_parser("process", help="Transcribe an existing WAV and write Claude notes.")
    proc.add_argument("wav", help="WAV file, e.g. recordings/meet-<id>-<stamp>.wav")
    proc.add_argument("--no-notes", action="store_true", help="Transcribe only")
    proc.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    proc.set_defaults(func=cmd_process)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
