"""Local speech-to-text with Whisper (faster-whisper). Free, runs on this machine, audio never leaves it.

The Claude API does not accept audio, so transcription happens here and Claude only sees text.

Settings (environment variables):
    NOTETAKER_WHISPER_MODEL    default "large-v3-turbo"; "small" is ~2.5x faster, lower quality
    NOTETAKER_WHISPER_THREADS  default 8
    NOTETAKER_LANGUAGE         force a language code such as "de"; default detects per segment
"""

import contextlib
import fcntl
import logging
import os
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

_logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
LOCK_PATH = Path("generated") / ".transcribe.lock"
_models: dict = {}


class UnreadableAudio(ValueError):
    """The file is not audio, or uses a format that can't be decoded."""


def load_wav(path: Path) -> np.ndarray:
    """Read the bot's 16 kHz mono PCM16 WAV as float32 samples in [-1, 1]."""
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(
                f"{path}: expected 16 kHz mono 16-bit WAV, got {w.getframerate()} Hz, "
                f"{w.getnchannels()} channel(s), {8 * w.getsampwidth()}-bit"
            )
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def load_audio(path: Path) -> np.ndarray:
    """Any audio file (MP3, M4A/AAC, WAV of any rate, OGG, FLAC, the audio of a video, ...) as
    16 kHz mono float32 samples. The bot's own WAVs take the fast path above; everything else is
    decoded with PyAV (bundled FFmpeg; no separate install). Done here rather than by
    faster-whisper, whose own decoder breaks with PyAV 19."""
    try:
        return load_wav(path)
    except (wave.Error, ValueError, EOFError):
        pass
    import av

    try:
        chunks = []
        with av.open(str(path)) as container:
            streams = [s for s in container.streams if s.type == "audio"]
            if not streams:
                raise UnreadableAudio("the file contains no audio track")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(streams[0]):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    except UnreadableAudio:
        raise
    except Exception as err:
        raise UnreadableAudio("could not read the file as audio; send an audio file such as MP3, WAV or M4A") from err
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _get_model(name: str):
    if name not in _models:
        from faster_whisper import WhisperModel

        threads = int(os.environ.get("NOTETAKER_WHISPER_THREADS", "8"))
        _logger.info("Whisper: loading %s (first run downloads it, ~1.6 GB for turbo)", name)
        _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
    return _models[name]


@contextlib.contextmanager
def _machine_lock():
    """One transcription at a time on this machine; Whisper already uses all the cores it gets.

    Other workers wait here (their status stays "transcribing") instead of thrashing the CPU.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _logger.info("Whisper: another transcription is running, waiting for it")
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def transcribe_wav(path: Path, model_name: Optional[str] = None, language: Optional[str] = None) -> dict:
    """Transcribe a WAV. Returns {model, language, duration, seconds_taken, segments: [{start, end, text}]}."""
    model_name = model_name or os.environ.get("NOTETAKER_WHISPER_MODEL", DEFAULT_MODEL)
    language = language or os.environ.get("NOTETAKER_LANGUAGE") or None
    audio = load_audio(path)
    with _machine_lock():
        model = _get_model(model_name)
        t0 = time.time()
        segments, info = model.transcribe(
            audio,
            language=language,
            # Detect the language per segment, so a German remark in an English call is kept.
            # Without this Whisper locks onto one language and silently drops the rest.
            multilingual=language is None,
            vad_filter=True,  # skip silence; avoids hallucinated text in quiet stretches
            beam_size=5,
        )
        out = [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments
            if s.text.strip()
        ]
        taken = time.time() - t0
    _logger.info("Whisper: %d segments, %.0fs audio in %.0fs (%s)", len(out), info.duration, taken, model_name)
    return {
        "model": model_name,
        "language": info.language,
        "duration": round(info.duration, 2),
        "seconds_taken": round(taken, 1),
        "segments": out,
    }


def preload(model_name: Optional[str] = None) -> str:
    """Download (if needed) and load the model once, so the first real call isn't slowed by it."""
    name = model_name or os.environ.get("NOTETAKER_WHISPER_MODEL", DEFAULT_MODEL)
    _get_model(name)
    return name


def _main() -> int:
    """python -m notetaker.transcribe                       download/load the model
       python -m notetaker.transcribe --file X [--language de]
                                                           transcribe one file, print JSON on stdout"""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--file")
    parser.add_argument("--language")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    if not args.file:
        print(f"Whisper model ready: {preload()}")
        return 0
    try:
        result = transcribe_wav(Path(args.file), language=args.language)
    except UnreadableAudio as err:
        print(json.dumps({"error": str(err)}))
        return 2
    result["text"] = "\n".join(s["text"] for s in result["segments"])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
