"""Audio helpers: convert meeto's raw PCM16 dump into a playable WAV."""

import wave
from pathlib import Path

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # PCM16


def pcm16_to_wav(pcm_path: Path, wav_path: Path, sample_rate: int = SAMPLE_RATE) -> float:
    """Wrap a raw PCM16 mono file in a WAV header. Returns the duration in seconds."""
    data = Path(pcm_path).read_bytes()
    wav_path = Path(wav_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(data)
    frames = len(data) // (SAMPLE_WIDTH_BYTES * CHANNELS)
    return frames / sample_rate
