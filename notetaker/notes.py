"""Meeting notes from a transcript, written by Claude.

The Claude API takes text, not audio, so this runs on the Whisper transcript.

Credentials: ANTHROPIC_API_KEY in the environment, or in a .env file in the project folder.
Settings:   NOTETAKER_CLAUDE_MODEL (default claude-opus-5-5), NOTETAKER_CLAUDE_EFFORT (default medium).
"""

import logging
import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"


class NotesUnavailable(RuntimeError):
    """Claude could not produce notes (no key, refusal, truncated output, API error)."""


class ActionItem(BaseModel):
    task: str = Field(description="What has to be done, as one short imperative sentence.")
    owner: Optional[str] = Field(description="Who agreed to do it, as named in the transcript; null if nobody was named.")
    due: Optional[str] = Field(description="Deadline exactly as said in the meeting (e.g. 'Friday'); null if none.")


class MeetingNotes(BaseModel):
    title: str = Field(description="A short title for the meeting, max 8 words.")
    language: str = Field(description="ISO 639-1 code of the language the notes are written in.")
    summary: str = Field(description="3-6 sentence summary of what the meeting was about and its outcome.")
    key_points: list[str] = Field(description="The most important points discussed.")
    decisions: list[str] = Field(description="Decisions that were explicitly made. Empty if none.")
    action_items: list[ActionItem] = Field(description="Tasks someone committed to. Empty if none.")
    open_questions: list[str] = Field(description="Questions raised but not resolved. Empty if none.")


SYSTEM_PROMPT = """You write meeting notes that a sales team stores in their CRM next to the call.

You get a machine transcript of the call. Each line has a speaker name and what was said. \
Speaker names come from the meeting app's speaking indicator and can be wrong or "Unknown"; \
when the text itself makes clear who is speaking (someone addressed by name, someone saying "I will"), trust the text. \
The transcript comes from speech recognition, so expect misheard words, especially names, product terms and numbers; \
correct them only when the context makes the intended word obvious.

Write the notes in the language most of the meeting was held in. \
Only record decisions and action items that were actually agreed in the call, never ones you infer would be sensible. \
If the transcript is too short or empty to say anything meaningful, say so in the summary and leave the lists empty."""


def write_notes(transcript_text: str, *, meeting_label: str = "") -> tuple[MeetingNotes, dict]:
    """Return (notes, info) where info has model, stop_reason, token usage and request id."""
    if not transcript_text.strip():
        raise NotesUnavailable("transcript is empty, nothing to summarise")
    client = anthropic.Anthropic()

    model = os.environ.get("NOTETAKER_CLAUDE_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("NOTETAKER_CLAUDE_EFFORT", "medium")
    header = f"Meeting: {meeting_label}\n\n" if meeting_label else ""
    try:
        # Streaming keeps a long transcript + thinking clear of HTTP timeouts.
        # fallbacks="default": if a safety classifier declines, the API re-runs the request
        # on Anthropic's recommended fallback model instead of returning a refusal.
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            output_config={"effort": effort},
            output_format=MeetingNotes,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": f"{header}Transcript:\n\n{transcript_text}"}],
        ) as stream:
            message = stream.get_final_message()
            request_id = stream.request_id
    except TypeError as err:
        # The SDK only notices missing credentials when it builds the first request.
        if "authentication" in str(err).lower():
            raise NotesUnavailable("no Claude API key: put ANTHROPIC_API_KEY=sk-ant-... in .env") from err
        raise
    except anthropic.AuthenticationError as err:
        raise NotesUnavailable("Claude rejected the API key (401); check ANTHROPIC_API_KEY") from err
    except anthropic.PermissionDeniedError as err:
        raise NotesUnavailable(f"API key lacks access to {model} (403)") from err
    except anthropic.RateLimitError as err:
        raise NotesUnavailable("Claude rate limit hit (429) after retries; try again later") from err
    except anthropic.BadRequestError as err:
        raise NotesUnavailable(f"Claude rejected the request (400): {err.message}") from err
    except anthropic.APIStatusError as err:
        raise NotesUnavailable(f"Claude API error {err.status_code}: {err.message}") from err
    except anthropic.APIConnectionError as err:
        raise NotesUnavailable(f"could not reach the Claude API: {err}") from err

    info = {
        "model": message.model,
        "stop_reason": message.stop_reason,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "request_id": request_id,
    }
    if message.stop_reason == "refusal":
        category = getattr(message.stop_details, "category", None) if message.stop_details else None
        raise NotesUnavailable(f"Claude declined to write notes (refusal, category={category})")
    if message.stop_reason == "max_tokens":
        raise NotesUnavailable("Claude's output was cut off at max_tokens")
    notes = message.parsed_output
    if notes is None:
        raise NotesUnavailable(f"Claude returned no structured notes (stop_reason={message.stop_reason})")
    _logger.info("Claude notes: model=%s in=%s out=%s tokens", info["model"], info["input_tokens"], info["output_tokens"])
    return notes, info


def notes_to_markdown(notes: MeetingNotes) -> str:
    def bullets(items):
        return "\n".join(f"- {i}" for i in items) if items else "- (none)"

    actions = [
        a.task + (f" ({a.owner})" if a.owner else "") + (f", due {a.due}" if a.due else "")
        for a in notes.action_items
    ]
    return (
        f"# {notes.title}\n\n{notes.summary}\n\n"
        f"## Key points\n{bullets(notes.key_points)}\n\n"
        f"## Decisions\n{bullets(notes.decisions)}\n\n"
        f"## Action items\n{bullets(actions)}\n\n"
        f"## Open questions\n{bullets(notes.open_questions)}\n"
    )
