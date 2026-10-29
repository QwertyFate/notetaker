"""Google Meet backend built on meeto (Playwright, guest join, in-page audio capture)."""

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import meeto.meet.joiner as _joiner
import meeto.pipeline as _pipeline
import meeto.runtime as _runtime
from meeto.config.models import AudioConfig, JoinConfig, SttConfig, WorkerConfig
from meeto.runtime import run_meeting_worker

from notetaker.backends import meet_chat

_logger = logging.getLogger(__name__)

# meeto 0.3.3 looks for "Continue without ..." only inside the FIRST div[role=dialog] on the
# page. Current Meet stacks two: "Do you want people to see you in the meeting?" (button
# "Continue without camera") and a "Sign in with your Google account" tip (button "Got it").
# When the tip is first in the DOM, meeto never finds the Continue button and the join click
# is blocked. Walk every dialog and click whichever dismiss button it offers, then fall back
# to meeto's own handler.
_orig_dismiss_consent_popup = _joiner._dismiss_consent_popup
_DISMISS_BUTTONS = (
    'button:has-text("Continue without"), button:has-text("Got it"), '
    'button[aria-label*="close" i], button[aria-label*="dismiss" i]'
)


async def _dismiss_any_dialog(page, screenshot_dir, screenshot_storage=None) -> bool:
    clicked = False
    try:
        for _ in range(4):
            dialogs = page.locator('div[role="dialog"]')
            hit = False
            for i in range(await dialogs.count()):
                btn = dialogs.nth(i).locator(_DISMISS_BUTTONS).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                try:
                    label = (await btn.text_content(timeout=1000)
                             or await btn.get_attribute("aria-label", timeout=1000) or "").strip()
                except Exception:
                    label = "?"
                try:
                    await btn.click(timeout=3000)
                except Exception:
                    await btn.click(force=True, timeout=3000)
                _logger.info("Meet backend: dismissed dialog via %r", label)
                clicked = hit = True
                await asyncio.sleep(1)
                break
            if not hit:
                # A dialog with no known button (e.g. the camera nudge opened by the
                # "Camera problem" button) still closes on Escape.
                if await dialogs.count() > 0 and await dialogs.first.is_visible():
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                    if await dialogs.count() == 0:
                        _logger.info("Meet backend: dismissed dialog via Escape")
                        clicked = True
                break
    except Exception as err:  # never block the join on this; meeto's handler gets its turn
        _logger.debug("Meet backend: dialog dismiss attempt failed: %s", err)
    if clicked:
        return True
    return await _orig_dismiss_consent_popup(page, screenshot_dir, screenshot_storage)


_joiner._dismiss_consent_popup = _dismiss_any_dialog

# The dialogs appear right after the name is typed and block meeto's mic/camera toggle clicks
# for a full 30 s timeout each. Clear them as soon as the name is in.
_orig_fill_guest_name = _joiner._fill_guest_name


async def _fill_guest_name_then_dismiss(page, bot_name, screenshot_dir, screenshot_storage=None):
    await _orig_fill_guest_name(page, bot_name, screenshot_dir, screenshot_storage)
    await asyncio.sleep(1)
    await _dismiss_any_dialog(page, None)


_joiner._fill_guest_name = _fill_guest_name_then_dismiss

# meeto does not recognise Google's refusal page and would sit in "waiting for admission"
# for 15 minutes. Spot it and fail fast with the reason. (runtime imported the name directly,
# so the patch goes on meeto.runtime, not on the joiner module.)
_orig_wait_for_admission = _runtime.wait_for_admission
_REFUSAL_TEXT = "You can't join this video call"


class JoinRefused(RuntimeError):
    pass


async def _wait_for_admission_or_refusal(page, *args, **kwargs):
    for _ in range(6):  # the refusal page shows within a few seconds of the join click
        try:
            if await page.get_by_text(_REFUSAL_TEXT).count() > 0:
                raise JoinRefused(
                    "Google refused the guest: this meeting does not allow anonymous joiners. "
                    "Give the bot a Google account (see README: meeto-auth) or host the meeting "
                    "from a Workspace account that allows anonymous guests."
                )
        except JoinRefused:
            raise
        except Exception as err:
            _logger.debug("Meet backend: refusal check failed: %s", err)
        await asyncio.sleep(1)
    admitted = await _orig_wait_for_admission(page, *args, **kwargs)
    if admitted and _chat.text:
        # Announce the recording in the meeting chat, alongside the recording itself.
        _chat.task = asyncio.create_task(meet_chat.notice_task(page, _chat.text, _chat.on_result))
    return admitted


# meeto attaches its audio capture to each Meet <audio> element only once. If Meet first gives an
# element a placeholder stream and later swaps in the participant's real one, meeto would keep
# listening to the silent placeholder. Re-attach whenever an element's stream changes.
_orig_audio_capture_script = _pipeline._audio_capture_script
_ATTACH_ONCE = "if (sources.has(el)) return;"
_ATTACH_ON_CHANGE = ("if (sources.has(el) && sources.get(el).__stream === el.srcObject) return;\n"
                     "    if (sources.has(el)) { try { sources.get(el).disconnect(); } catch (_e) {} sources.delete(el); }")
_REMEMBER = "sources.set(el, source);"


def _audio_capture_script_reattach(*args, **kwargs) -> str:
    script = _orig_audio_capture_script(*args, **kwargs)
    if _ATTACH_ONCE not in script or _REMEMBER not in script:
        _logger.warning("Meet backend: meeto's audio script changed; stream re-attach patch not applied")
        return script
    return (script.replace(_ATTACH_ONCE, _ATTACH_ON_CHANGE)
                  .replace(_REMEMBER, "source.__stream = el.srcObject; " + _REMEMBER))


_pipeline._audio_capture_script = _audio_capture_script_reattach

# Current Google Meet plays participants' audio without any <audio> elements, so meeto's capture
# (which only listens to <audio> elements) hears nothing. Collect every incoming audio track where
# it enters the page, from the WebRTC connection, before Meet's own code runs; the capture script
# then listens to those tracks as well.
_RTC_TRACK_HOOK = """
(() => {
  if (window.__ntRtcHooked || !window.RTCPeerConnection) return;
  window.__ntRtcHooked = true;
  window.__ntRemoteAudio = [];
  const Orig = window.RTCPeerConnection;
  function Hooked(...args) {
    const pc = new Orig(...args);
    pc.addEventListener('track', (e) => {
      if (e.track && e.track.kind === 'audio') window.__ntRemoteAudio.push(e.track);
    });
    return pc;
  }
  Hooked.prototype = Orig.prototype;
  Object.setPrototypeOf(Hooked, Orig);
  window.RTCPeerConnection = Hooked;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Hooked;
})();
(() => {
  // Meet decodes call audio itself (encoded streams + AudioWorklets) and plays it through WebAudio,
  // so the WebRTC tracks stay silent. Copy everything the page connects to the speakers into a
  // stream per AudioContext; the capture script records those streams: what a person would hear.
  if (window.__ntPlaybackHooked || !window.AudioNode) return;
  window.__ntPlaybackHooked = true;
  window.__ntPlaybackStreams = [];
  const connect = AudioNode.prototype.connect;
  const taps = new WeakMap();
  AudioNode.prototype.connect = function (destination, ...rest) {
    const result = connect.call(this, destination, ...rest);
    try {
      const ctx = this.context;
      if (destination instanceof AudioDestinationNode && !ctx.__ntCapture && ctx.createMediaStreamDestination) {
        let tap = taps.get(ctx);
        if (!tap) {
          tap = ctx.createMediaStreamDestination();
          taps.set(ctx, tap);
          window.__ntPlaybackStreams.push(tap.stream);
          (window.__ntPlaybackContexts = window.__ntPlaybackContexts || []).push(ctx);
        }
        connect.call(this, tap);
      }
    } catch (err) {}
    return result;
  };
})();
"""
_SCAN_AUDIO_ELEMENTS = 'document.querySelectorAll("audio").forEach(attachAudio);'
_SCAN_RTC_TRACKS = _SCAN_AUDIO_ELEMENTS + """
    const playback = window.__ntPlaybackStreams || [];
    playback.forEach((stream) => {
      if (sources.has(stream)) return;
      try {
        const source = audioCtx.createMediaStreamSource(stream);
        source.connect(processor);
        sources.set(stream, source);
      } catch (err) {
        console.debug("Notetaker: attaching the page's playback failed", err);
      }
    });
    // The page's own playback is the complete mix: when there is any, drop the call tracks so
    // nothing is recorded twice; otherwise fall back to the call tracks.
    (window.__ntRemoteAudio || []).forEach((track) => {
      if (playback.length) {
        if (!sources.has(track)) return;
        try { sources.get(track).disconnect(); } catch (_e) {}
        sources.delete(track);
        return;
      }
      if (sources.has(track) || track.readyState === "ended") return;
      try {
        const stream = new MediaStream([track]);
        // Chrome only delivers a call track's audio to WebAudio while a media element plays it;
        // a muted element keeps it flowing without playing anything twice.
        const keepAlive = new Audio();
        keepAlive.muted = true;
        keepAlive.srcObject = stream;
        keepAlive.play().catch(() => {});
        (window.__ntKeepAlive = window.__ntKeepAlive || []).push(keepAlive);
        const source = audioCtx.createMediaStreamSource(stream);
        source.connect(processor);
        sources.set(track, source);
      } catch (err) {
        console.debug("Notetaker: attaching a call audio track failed", err);
      }
    });"""


def _audio_capture_script_rtc(*args, **kwargs) -> str:
    script = _audio_capture_script_reattach(*args, **kwargs)
    if _SCAN_AUDIO_ELEMENTS not in script:
        _logger.warning("Meet backend: meeto's audio script changed; call-track capture not applied")
        return script
    script = script.replace(_SCAN_AUDIO_ELEMENTS, _SCAN_RTC_TRACKS, 1)
    script = script.replace("const audioCtx = new (window.AudioContext || window.webkitAudioContext)();",
                            "const audioCtx = new (window.AudioContext || window.webkitAudioContext)();\n"
                            "  audioCtx.__ntCapture = true;", 1)
    # Debug output: also report each captured call track ("muted" = nobody speaking on it, "live").
    return script.replace('event: "scan",', 'event: "scan", playback_taps: (window.__ntPlaybackStreams || []).length, '
                          'playback_states: (window.__ntPlaybackContexts || []).map((c) => c.state), '
                          'call_tracks: (window.__ntRemoteAudio || [])'
                          '.map((t) => t.readyState === "ended" ? "ended" : (t.muted ? "muted" : "live")),', 1)


_pipeline._audio_capture_script = _audio_capture_script_rtc

# The hook has to be in place before Meet creates its connections, so install it on every browser
# context this process opens (meeto creates the context; this is the earliest point we can reach).
from playwright.async_api import Browser as _PWBrowser  # noqa: E402

_orig_new_context = _PWBrowser.new_context


async def _new_context_with_rtc_hook(self, *args, **kwargs):
    context = await _orig_new_context(self, *args, **kwargs)
    await context.add_init_script(_RTC_TRACK_HOOK)
    return context


_PWBrowser.new_context = _new_context_with_rtc_hook


# Per-process chat-notice settings (one worker process records one meeting).
_chat = SimpleNamespace(text=None, on_result=None, task=None)


_runtime.wait_for_admission = _wait_for_admission_or_refusal

OUTPUT_DIR = Path("generated")
# Authenticated mode: a Google login saved by `meeto-auth --output storage_state.json`.
# Needed when Google refuses anonymous guests ("You can't join this video call"), which is
# the case for meetings hosted by personal Google accounts.
STORAGE_STATE = Path(os.environ.get("NOTETAKER_STORAGE_STATE", "storage_state.json"))
_MEET_URL = re.compile(r"^https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(\?.*)?$")


class UnsupportedMeetingUrl(ValueError):
    pass


def validate_url(url: str) -> str:
    url = url.strip()
    if not _MEET_URL.match(url):
        raise UnsupportedMeetingUrl(
            f"Not a Google Meet link: {url!r}. "
            "Expected https://meet.google.com/xxx-xxxx-xxx (only Google Meet is supported for now)."
        )
    return url


def find_pcm(meeting_id: str) -> Optional[Path]:
    """Locate the PCM dump meeto wrote for this meeting, newest first."""
    audio_dir = OUTPUT_DIR / meeting_id / "audio"
    candidates = sorted(audio_dir.glob("*.pcm"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def storage_state_path() -> Optional[str]:
    return str(STORAGE_STATE) if STORAGE_STATE.exists() else None


def build_config(url: str, name: str, max_minutes: int, meeting_id: str) -> WorkerConfig:
    state = storage_state_path()
    if state:
        # Signed-in bot: no guest name (Meet shows the account's name), headless is fine.
        # Signed-in contexts get no mic/camera permission, so the toggle buttons are really
        # "Microphone problem" / "Camera problem" and open popups that block "Ask to join".
        # The bot only listens, so skip both clicks.
        join = JoinConfig(headless=os.environ.get("NOTETAKER_HEADLESS", "1") != "0",
                          storage_state_path=state, bot_name=name,
                          disable_mic=False, disable_camera=False)
    else:
        # Guest join must be headed: Google blocks headless browsers in the lobby.
        join = JoinConfig(headless=False, bot_name=name, disable_camera=False)
    # disable_camera=False skips meeto's camera-toggle click. The bot never gets camera
    # permission, so the camera is off anyway, and that button is really "Camera problem.
    # Show more info", which opens the popup that blocks "Ask to join".
    return WorkerConfig(
        meeting_id=meeting_id,
        meet_url=url,
        duration_seconds=max_minutes * 60,
        output_dir=str(OUTPUT_DIR),
        # NOTETAKER_AUDIO_DEBUG=1 logs Meet's audio elements and the captured level every few seconds.
        audio=AudioConfig(dump_enabled=True, debug=os.environ.get("NOTETAKER_AUDIO_DEBUG") == "1"),
        stt=SttConfig(provider=None),
        join=join,
    )


async def record_async(
    url: str,
    name: str = "Notetaker",
    max_minutes: int = 120,
    meeting_id: Optional[str] = None,
    state_store=None,
    chat_message: Optional[str] = None,
    on_chat_result: Optional[Callable[[dict], None]] = None,
) -> tuple[str, Optional[Path]]:
    """Join the meeting as a guest, record until it ends, return (meeting_id, pcm_path).

    pcm_path is None if the bot was never admitted or nothing was captured.
    state_store is an optional meeto MeetingLifecycleStore that receives status updates
    (joining, waiting_for_admission, recording, completed, failed).
    chat_message is posted in the meeting chat once admitted: None uses NOTETAKER_CHAT_MESSAGE
    or the default notice, "" posts nothing. on_chat_result gets the outcome dict.
    Cancelling the task (asyncio.CancelledError) lets meeto close the browser; the
    CancelledError is re-raised afterwards so callers can convert any captured audio.
    """
    url = validate_url(url)
    meeting_id = meeting_id or uuid.uuid4().hex[:12]
    config = build_config(url, name, max_minutes, meeting_id)
    _logger.info("Meet backend: meeting_id=%s url=%s bot=%r mode=%s", meeting_id, url, name,
                 "authenticated" if config.join.storage_state_path else "guest")
    text = meet_chat.default_message() if chat_message is None else chat_message.strip()
    _chat.text, _chat.on_result, _chat.task = text[:meet_chat.MAX_LENGTH] or None, on_chat_result, None
    try:
        await run_meeting_worker(config, state_store=state_store)
    finally:
        if _chat.task and not _chat.task.done():
            _chat.task.cancel()
        _logger.info("Meet backend: pcm=%s", find_pcm(meeting_id))
    return meeting_id, find_pcm(meeting_id)


def record(url: str, name: str = "Notetaker", max_minutes: int = 120,
           meeting_id: Optional[str] = None, state_store=None) -> tuple[str, Optional[Path]]:
    """Blocking wrapper around record_async for simple scripts."""
    return asyncio.run(record_async(url, name=name, max_minutes=max_minutes,
                                    meeting_id=meeting_id, state_store=state_store))
