"""Label transcript segments with who was speaking, using meeto's active-speaker events.

meeto watches the Meet page and logs JSON lines like
    {"type": "speaker_event", "speaker": "Anna Schmidt", "timestamp": 1760900000.12, "is_speaking": true}
Each Whisper segment is matched to the speaker whose speaking time overlaps it most.
"""

import json
from pathlib import Path
from typing import Optional

UNKNOWN = "Unknown"


def load_intervals(jsonl_path: Path, end_epoch: float) -> list[tuple[str, float, float]]:
    """Turn start/stop events into (speaker, start_epoch, end_epoch) intervals."""
    intervals = []
    open_since: dict[str, float] = {}
    if not jsonl_path or not Path(jsonl_path).exists():
        return intervals
    for line in Path(jsonl_path).read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        name, ts = ev.get("speaker"), ev.get("timestamp")
        if not name or ts is None:
            continue
        if ev.get("is_speaking"):
            open_since.setdefault(name, ts)
        elif name in open_since:
            intervals.append((name, open_since.pop(name), ts))
    for name, start in open_since.items():  # still speaking when the recording stopped
        intervals.append((name, start, end_epoch))
    return intervals


def attribute(segments: list[dict], intervals: list[tuple[str, float, float]], audio_start_epoch: float) -> list[dict]:
    """Return copies of segments with a "speaker" field."""
    labelled = []
    for seg in segments:
        a, b = audio_start_epoch + seg["start"], audio_start_epoch + seg["end"]
        overlap: dict[str, float] = {}
        for name, s, e in intervals:
            o = min(b, e) - max(a, s)
            if o > 0:
                overlap[name] = overlap.get(name, 0.0) + o
        speaker = max(overlap, key=overlap.get) if overlap else _nearest_before(intervals, a)
        labelled.append({**seg, "speaker": speaker or UNKNOWN})
    return labelled


def _nearest_before(intervals, t: float, window: float = 2.0) -> Optional[str]:
    """Speaker indicators lag the audio a little; accept someone who stopped just before."""
    best, best_gap = None, window
    for name, _s, e in intervals:
        gap = t - e
        if 0 <= gap <= best_gap:
            best, best_gap = name, gap
    return best


def to_turns(segments: list[dict]) -> list[dict]:
    """Merge consecutive segments by the same speaker into turns."""
    turns: list[dict] = []
    for seg in segments:
        speaker = seg.get("speaker", UNKNOWN)
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["end"] = seg["end"]
            turns[-1]["text"] += " " + seg["text"]
        else:
            turns.append({"speaker": speaker, "start": seg["start"], "end": seg["end"], "text": seg["text"]})
    return turns


def format_transcript(turns: list[dict]) -> str:
    """Plain-text transcript, one speaker turn per line:

        Anna Schmidt: Hallo zusammen, danke fürs Kommen.
        Tom Becker: Ich würde beim Preis bleiben.

    Times are left out here; they are in the turns ("start", "end" in seconds)."""
    return "\n".join(f"{t['speaker']}: {t['text']}" for t in turns)
