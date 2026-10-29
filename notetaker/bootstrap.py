"""First-run setup that pip can't do: the bot's browser and the Whisper model.

With these, `pip install -r requirements.txt` is the whole install. Both steps are no-ops
once done (the browser check takes about half a second).
"""

import logging
import os
import subprocess
import sys
import threading

_logger = logging.getLogger(__name__)


def ensure_browser() -> None:
    """Download Playwright's Chromium (about 200 MB) if this machine doesn't have it yet."""
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"could not install the bot's browser: {result.stderr.strip()[-500:]}")
    if result.stdout.strip():
        _logger.info("Installed the bot's browser (Playwright Chromium)")


def download_whisper_model_in_background() -> threading.Thread:
    """Fetch the Whisper model files (about 1.5 GB) without loading them, so the first
    transcription doesn't wait for the download. Failures only log; transcription retries."""
    def _download():
        from faster_whisper import download_model

        from notetaker.transcribe import DEFAULT_MODEL

        name = os.environ.get("NOTETAKER_WHISPER_MODEL", DEFAULT_MODEL)
        try:
            download_model(name)
            _logger.info("Whisper model %s is on disk", name)
        except Exception as err:
            _logger.warning("Whisper model download failed (%s); it will be retried on first use", err)

    t = threading.Thread(target=_download, name="whisper-download", daemon=True)
    t.start()
    return t
