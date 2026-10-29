"""Recording notice in the Google Meet chat.

Right after the bot is admitted it posts a notice in the meeting chat and pins it. Meet's
chat history is off by default, so people who join later only see earlier messages that are
pinned. If pinning isn't possible (Google may not allow it for a participant, or the host turned
chat off), the notice is posted again whenever new people join, at most once a minute.

Text: NOTETAKER_CHAT_MESSAGE in .env (empty = no notice), or per CRM request.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from meeto.meet.end_detector import _get_participant_count

_logger = logging.getLogger(__name__)

DEFAULT_MESSAGE = (
    "Hallo, ich bin Notetaker. Dieses Meeting wird gemäß unserer Richtlinie zur Aufzeichnung von "
    "Meetings aufgezeichnet und transkribiert, um Notizen zu erstellen. Wenn Sie keine Aufzeichnung "
    "wünschen, kann der Host mich aus dem Meeting entfernen."
)
MAX_LENGTH = 500
REPOST_MIN_INTERVAL_S = 60
WATCH_INTERVAL_S = 20

_BOX = 'textarea[aria-label="Send a message"]'
_CHAT_TOGGLE = 'button[aria-label="Chat with everyone"]'


def default_message() -> str:
    """NOTETAKER_CHAT_MESSAGE if set (an empty value turns the notice off), else the default."""
    return os.environ.get("NOTETAKER_CHAT_MESSAGE", DEFAULT_MESSAGE).strip()


async def _open_chat(page) -> bool:
    box = page.locator(_BOX)
    if not await box.is_visible():
        await page.locator(_CHAT_TOGGLE).first.click(timeout=5000)
        await box.wait_for(state="visible", timeout=5000)
    return not await box.is_disabled()


async def _close_chat(page) -> None:
    try:
        if await page.locator(_BOX).is_visible():
            await page.locator(_CHAT_TOGGLE).first.click(timeout=3000)
    except Exception:
        pass


async def _pin(page, bubble) -> Optional[str]:
    """Pin the message just sent. Returns None on success, else the reason it failed."""
    try:
        await bubble.hover()
        await page.locator('button[aria-label="Pin message"]:visible').first.click(timeout=3000)
        await page.get_by_role("button", name="Pin this message").click(timeout=3000)
        await page.locator('button[aria-label="Unpin message"]').first.wait_for(state="attached", timeout=3000)
        return None
    except Exception as err:
        try:
            await page.keyboard.press("Escape")  # close a half-open pin dialog
        except Exception:
            pass
        return f"{type(err).__name__}: {str(err).splitlines()[0][:150]}"


async def post_notice(page, text: str, *, pin: bool = True) -> dict:
    """Send `text` in the meeting chat (and pin it). Never raises; the outcome is in the dict."""
    result = {"text": text, "sent": False, "pinned": False, "sent_at": None, "error": None}
    try:
        if not await _open_chat(page):
            result["error"] = "chat is turned off for participants in this meeting"
            return result
        box = page.locator(_BOX)
        await box.fill(text)
        await box.press("Enter")
        bubble = page.get_by_text(text[:120]).last
        await bubble.wait_for(state="visible", timeout=5000)
        result.update(sent=True, sent_at=datetime.now(timezone.utc).isoformat())
        if pin:
            reason = await _pin(page, bubble)
            result["pinned"] = reason is None
            if reason:
                result["pin_error"] = reason
    except Exception as err:
        result["error"] = f"{type(err).__name__}: {str(err).splitlines()[0][:150]}"
    finally:
        await _close_chat(page)
    return result


async def notice_task(page, text: str, on_result: Optional[Callable[[dict], None]] = None) -> None:
    """Post (and pin) the notice; if it couldn't be pinned, repeat it when new people join."""
    report = on_result or (lambda r: None)
    await asyncio.sleep(2)  # let meeto start the audio capture first
    result = await post_notice(page, text)
    result["posts"] = 1 if result["sent"] else 0
    report(result)
    _logger.info("Chat notice: sent=%s pinned=%s %s", result["sent"], result["pinned"],
                 result.get("error") or result.get("pin_error") or "")
    if result["pinned"] or result["error"]:
        return  # pinned: late joiners see it. Chat off: nothing more we can do.

    seen = await _get_participant_count(page)
    last_post = time.monotonic()
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        count = await _get_participant_count(page)
        if count > seen and time.monotonic() - last_post >= REPOST_MIN_INTERVAL_S:
            again = await post_notice(page, text, pin=False)
            if again["sent"]:
                result["posts"] += 1
                result["last_sent_at"] = again["sent_at"]
                report(result)
                last_post = time.monotonic()
                _logger.info("Chat notice re-posted for new participants (%d -> %d)", seen, count)
        seen = max(seen, count)
