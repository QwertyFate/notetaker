"""After a call: WAV -> Whisper transcript (with speaker names) -> Claude meeting notes.

Writes next to the WAV in recordings/:
    meet-<id>-<stamp>.transcript.json   segments, speaker turns, metadata
    meet-<id>-<stamp>.transcript.txt    "Anna Schmidt: ..." one speaker turn per line
    meet-<id>-<stamp>.notes.json        structured notes for the CRM
    meet-<id>-<stamp>.notes.md          the same notes for humans
"""

import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from notetaker import speakers
from notetaker.notes import NotesUnavailable, notes_to_markdown, write_notes
from notetaker.transcribe import transcribe_wav

_logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("generated")
_JOB_ID_IN_NAME = re.compile(r"^meet-([0-9a-f]{12})-")


def job_id_from_wav(wav: Path) -> Optional[str]:
    m = _JOB_ID_IN_NAME.match(Path(wav).name)
    return m.group(1) if m else None


def _audio_start_epoch(job_id: str, duration: float) -> Optional[float]:
    """When the first audio sample was captured, to line Whisper's times up with speaker events."""
    status = OUTPUT_DIR / job_id / "status.json"
    if status.exists():
        epoch = json.loads(status.read_text()).get("recording_started_epoch")
        if epoch:
            return float(epoch)
    pcms = sorted((OUTPUT_DIR / job_id / "audio").glob("*.pcm"))
    if pcms:  # audio streams continuously, so the last write minus the length is the start
        return pcms[-1].stat().st_mtime - duration
    return None


def _label_speakers(job_id: Optional[str], segments: list[dict], duration: float) -> tuple[list[dict], bool]:
    if not job_id:
        return segments, False
    events = sorted((OUTPUT_DIR / job_id / "speaker_events").glob("*_speakers.jsonl"))
    start = _audio_start_epoch(job_id, duration)
    if not events or start is None:
        return segments, False
    intervals = speakers.load_intervals(events[-1], end_epoch=start + duration)
    if not intervals:
        return segments, False
    return speakers.attribute(segments, intervals, start), True


def process_recording(
    wav: Path,
    *,
    job_id: Optional[str] = None,
    with_notes: bool = True,
    on_stage: Callable[[str], None] = lambda stage: None,
) -> dict:
    """Run transcription and notes. Never raises for a failed step; errors are returned as fields."""
    wav = Path(wav)
    job_id = job_id or job_id_from_wav(wav)
    base = wav.with_suffix("")
    result: dict = {}

    on_stage("transcribing")
    try:
        tr = transcribe_wav(wav)
    except Exception as err:
        _logger.exception("transcription failed for %s", wav)
        result["transcript_error"] = f"transcription failed: {err}"
        return result

    segments, labelled = _label_speakers(job_id, tr["segments"], tr["duration"])
    if not labelled:
        segments = [{**s, "speaker": speakers.UNKNOWN} for s in segments]
    turns = speakers.to_turns(segments)
    text = speakers.format_transcript(turns)

    transcript_json = base.with_name(base.name + ".transcript.json")
    transcript_txt = base.with_name(base.name + ".transcript.txt")
    transcript_json.write_text(json.dumps(
        {**{k: v for k, v in tr.items() if k != "segments"}, "speakers_labelled": labelled,
         "turns": turns, "segments": segments},
        indent=2, ensure_ascii=False,
    ))
    transcript_txt.write_text(text + "\n")
    result.update(transcript=str(transcript_json), transcript_text=str(transcript_txt),
                  language=tr["language"], speakers_labelled=labelled)

    if not with_notes:
        return result

    on_stage("summarizing")
    try:
        notes, info = write_notes(text, meeting_label=wav.name)
    except NotesUnavailable as err:
        _logger.warning("notes skipped for %s: %s", wav, err)
        result["notes_error"] = str(err)
        return result
    except Exception as err:  # the transcript is already saved; never lose it over the notes step
        _logger.exception("notes failed for %s", wav)
        result["notes_error"] = f"notes failed: {err}"
        return result
    notes_json = base.with_name(base.name + ".notes.json")
    notes_md = base.with_name(base.name + ".notes.md")
    notes_json.write_text(json.dumps({**notes.model_dump(), "_claude": info}, indent=2, ensure_ascii=False))
    notes_md.write_text(notes_to_markdown(notes))
    result.update(notes=str(notes_json), notes_markdown=str(notes_md))
    return result
