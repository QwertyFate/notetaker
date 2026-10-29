# Notetaker

A Fathom-style meeting bot: it joins a Google Meet, records the audio, transcribes it
locally with Whisper, and has Claude write meeting notes. No Docker. Recording and
transcription are free and stay on your machine; only the notes step calls the Claude API.

Built on [meeto](https://github.com/ResearchifyLabs/meeto) (MIT), a Playwright-driven
Google Meet bot that captures audio inside the browser page.

## Install (Mac mini or any Mac)

Needs Python 3.12 or newer (from [python.org](https://www.python.org/downloads/) or
`brew install python@3.13`).

1. Copy this folder to the Mac, without `venv/`, `generated/` and `recordings/`.
2. Also copy `.env` (holds the Claude API key) and `storage_state.json` (the bot's Google
   login) from the Mac where they already exist.
3. Install and start:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
./start.sh
```

That is the whole install. On its first start the API downloads the bot's browser (about
200 MB, before it accepts requests) and the Whisper model (about 1.5 GB, in the background).
Every package in `requirements.txt` is pinned to the exact version tested here.

If you have no `.env` yet, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`.
If you have no `storage_state.json` yet, sign the bot in with
`venv/bin/python -m notetaker.login` (needs Google Chrome).

For a Mac mini that runs unattended, in System Settings turn on "Prevent automatic sleeping
when the display is off" (Energy). `./start.sh` runs in its terminal window, so it has to be
started again after a restart of the Mac.

### What it uses

Measured on a MacBook M4 with 16 GB, the same chip and memory as a base Mac mini M4.

| | |
|---|---|
| Disk, install | about 2.5 GB: Whisper model 1.5 GB, Python packages 0.4 GB, Chromium 0.6 GB |
| Disk, recordings | 115 MB per hour of WAV, plus small transcript and notes files |
| RAM, Whisper | 2.0 to 2.1 GB peak, only while transcribing, growing with call length |
| CPU, Whisper | about 7 to 8 of 10 cores while transcribing; idle otherwise |
| Speed, Whisper | about 2.3x real time, so roughly 25 minutes per hour of call |

### How the work is split

| Process | Does | Runs |
|---|---|---|
| API (`./start.sh`) | takes requests, launches scheduled bots, sends CRM callbacks | always |
| Bot worker, one per meeting | joins, records, saves the WAV, then exits | during the call |
| Transcriber, at most one | Whisper transcript, then Claude notes, oldest recording first | only while recordings are waiting |

- The transcriber runs at low CPU priority, so bots that are still in calls always come first.
- It starts within about 10 seconds of a recording arriving and exits a minute after the queue
  is empty, so it uses no memory between meetings. It loads Whisper in about 3 seconds.
- If it crashes or is stopped mid-transcription, the job goes back into the queue and is redone
  when it next starts. A recording that breaks it three times is finished with a transcript error.
- If a bot dies mid-call (Terminal closed, crash), the audio captured so far is kept and
  transcribed as a partial recording.
- Only the API sends CRM callbacks, so each result is sent once, and callbacks that were due
  while the API was down are sent when it starts again.

## Usage

```bash
source venv/bin/activate
python -m notetaker.cli record https://meet.google.com/abc-defg-hij --name Notetaker
```

Run commands from this folder, with the venv activated as above.

1. A Chromium window opens and "Notetaker" asks to join.
2. Admit it from the Meet lobby (within 15 minutes).
3. Recording runs until the meeting ends, `--max-minutes` elapses (default 120), or Ctrl-C.
4. The WAV is written to `recordings/meet-<id>-<timestamp>.wav` (16 kHz, mono).
5. Whisper transcribes it, then Claude writes notes. Both land next to the WAV.

## Recording notice in the meeting chat

As soon as the bot is admitted, it posts this in the meeting chat and pins it:

> Hallo, ich bin Notetaker. Dieses Meeting wird gemäß unserer Richtlinie zur Aufzeichnung von
> Meetings aufgezeichnet und transkribiert, um Notizen zu erstellen. Wenn Sie keine Aufzeichnung
> wünschen, kann der Host mich aus dem Meeting entfernen.

- **Pinned** so that people who join later see it too. Meet's chat history is off by default,
  so an unpinned message is invisible to late joiners.
- **If pinning isn't possible,** the bot posts the notice again whenever new people join, at
  most once a minute.
- **If the host has turned chat off for participants,** no notice can be posted; the result says so.
- **Change the text** with `NOTETAKER_CHAT_MESSAGE` in `.env` (for example to add a link to your
  policy, or a German version), or per meeting with `chat_message` in the CRM request.
  An empty value turns the notice off.
- The outcome (sent, pinned, how often) is in the job status and in the CRM callback as `chat_notice`.

A chat notice informs participants; whether that is enough consent for recording under your
local law (in Germany, section 201 StGB and the GDPR) is a question for your legal advisor.

## Transcripts and notes

The Claude API does not accept audio, so speech-to-text runs locally with Whisper
(faster-whisper, `large-v3-turbo`), and Claude gets the text transcript.

| File | Contents |
|---|---|
| `meet-<id>-<ts>.transcript.txt` | `Anna Schmidt: ...` one speaker turn per line |
| `meet-<id>-<ts>.transcript.json` | segments with times and speakers, merged turns, language |
| `meet-<id>-<ts>.notes.json` | title, summary, key points, decisions, action items (task, owner, due), open questions |
| `meet-<id>-<ts>.notes.md` | the same notes as Markdown |

- **Speaker names** come from the Meet speaking indicator that meeto records during the call.
- **Mixed languages** are kept: Whisper detects the language per segment, and Claude writes
  the notes in the meeting's main language.
- **Speed** on an M4: about 2.3x real time, so a one-hour call takes about 25 minutes. Set
  `NOTETAKER_WHISPER_MODEL=small` in `.env` for about 4.7x real time at lower quality
  (0.5 GB model, 1.2 to 1.4 GB RAM).
- **Claude key**: copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Without it you
  still get the transcript, and the status shows why notes were skipped.
- **Cost**: the notes use Claude Opus 5.5. A one-hour call is roughly 15k input tokens plus a
  few thousand output tokens, about 10 to 20 cents per call.
- **Privacy**: the transcript text is sent to Anthropic for the notes. Audio never leaves the machine.

Re-run both steps on any existing recording:

```bash
python -m notetaker.cli process recordings/meet-<id>-<timestamp>.wav            # transcript + notes
python -m notetaker.cli process recordings/meet-<id>-<timestamp>.wav --no-notes # transcript only
```

`record` also takes `--no-transcribe` and `--no-notes`.

Raw working files (PCM, screenshots) are kept under `generated/<id>/`.

## CRM route

One request from the CRM's backend books the bot, either for now or for the meeting's
scheduled time. The bot joins one minute before the start. When the meeting is over and the
transcript and notes are done, Notetaker POSTs everything to the CRM's `callback_url`.
The CRM doesn't have to poll.

### 0. Check that a bot is available (before offering it to the user)

```http
GET http://localhost:8333/crm/health                                    can a bot join now?
GET http://localhost:8333/crm/health?at=2026-10-21T14:00:00%2B02:00&minutes=60   at that time?
GET http://localhost:8333/crm/health?refresh=true                       re-check the Google login live (~2 s)
```

Answers **200** when a bot can join and **503** when not; the JSON body is the same either way:

```json
{
  "ready": false,
  "message": "All 5 recording bots are already booked at that time.",
  "problems": [{"check": "capacity", "code": "capacity_full", "message": "All 5 recording bots are already booked at that time."}],
  "warnings": [{"check": "notes", "message": "No Claude API key: transcripts work, notes are skipped."}],
  "checks": {
    "bot_account":   {"ok": true, "expires_at": "2027-11-23T...", "live_check": {"ok": true, "account": "notetaker.alphaiota@gmail.com", "checked_at": "..."}},
    "browser":       {"ok": true},
    "capacity":      {"ok": false, "at": "...", "bots_booked_in_slot": 5, "max_bots": 5},
    "disk":          {"ok": true, "free_gb": 290.1},
    "scheduler":     {"ok": true, "last_run_seconds_ago": 6},
    "transcription": {"ok": true, "model_on_disk": true, "queue": 0, "in_progress": 0},
    "notes":         {"ok": true, "claude_key_configured": false}
  }
}
```

Show `message` to the user, or map `problems[].code` to your own text:

| Code | Meaning | Fix |
|---|---|---|
| `bot_login_missing` | no saved Google login for the bot | `venv/bin/python -m notetaker.login` |
| `bot_login_expired` | the saved login has expired | same |
| `bot_login_rejected` | Google no longer accepts the login (checked live hourly) | same |
| `browser_missing` | the bot's browser isn't installed | restart `./start.sh` |
| `capacity_full` | `NOTETAKER_MAX_BOTS` bots are in calls now, or booked in that slot | wait, or pick another time |
| `memory_low` | less than 15% memory free right now | wait for calls to end |
| `disk_low` | less than `NOTETAKER_MIN_FREE_GB` free | delete old recordings |
| `scheduler_stalled` | the background loop isn't running, booked bots wouldn't start | restart `./start.sh` |

With `?at=`, capacity counts bookings whose time overlaps the slot (each booking counts for its
`max_minutes`). Memory is only judged for "now". Warnings never make `ready` false. The check is
advisory: a booking request is still accepted when the health check says full.

### 1. Request a bot

```http
POST http://localhost:8333/crm/recordings
Authorization: Bearer <NOTETAKER_CRM_TOKEN>        (only when set in .env)
Content-Type: application/json

{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "callback_url": "http://localhost:3000/api/notetaker/webhook",
  "join_at": "2026-10-21T14:00:00+02:00",
  "external_id": "crm-meeting-5531",
  "metadata": {"deal_id": "D-1042", "user_id": "anna"},
  "bot_name": "Notetaker",
  "max_minutes": 120,
  "include_notes": true,
  "chat_message": "Hallo, ich bin Notetaker. Dieses Meeting wird ..."
}
```

Only `meeting_url` and `callback_url` are required.

| Field | Meaning |
|---|---|
| `join_at` | Meeting start, ISO 8601 **with timezone** (`+02:00` or `Z`). Omit to send the bot now. Up to 30 minutes in the past is accepted (the bot joins right away). |
| `external_id` | The CRM's own meeting id. Send the same request again when the meeting changes (new time, new link): the scheduled bot is updated, not duplicated. |
| `metadata` | Any JSON object, returned unchanged in the callback, so the CRM knows which deal the result belongs to. |
| `max_minutes` | Recording stops after this long (1 to 240). |
| `include_notes` | `false` for transcript only. |
| `chat_message` | The recording notice the bot posts in the meeting chat for this meeting. Omit for the default, `""` for none. |

Responses:

- **202**: booked. The body has the job `id` and `status`: `scheduled` (with `join_at` and
  `bot_leaves_at`) or `queued` (bot on its way now). Store the `id` with the meeting.
- **200 with `"updated": true`**: an `external_id` you sent before; its scheduled bot now has the
  new time, link and metadata.
- **200 with `"duplicate": true`**: a bot is already booked for or in that meeting at about that
  time; you get that job instead of a second bot.
- **400**: not a Google Meet link, `join_at` without a timezone or too far in the past, or a
  `callback_url` on a host that is not allowed.
- **401**: wrong or missing token.

Times in responses and callbacks are in UTC (`...+00:00`).

**Scheduling needs Notetaker running at meeting time.** The scheduler lives in the API, so
`./start.sh` has to be running and the Mac awake when a bot is due. If Notetaker was off and
comes back within 30 minutes of the start, the bot still joins; later than that, the job fails
as `missed` and the CRM gets a `recording.failed` callback. Booked meetings survive restarts.

### 2. Receive the result

When the job ends, Notetaker POSTs to `callback_url`:

```json
{
  "event": "recording.completed",
  "id": "818b8fe97480",
  "status": "done",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "metadata": {"deal_id": "D-1042", "user_id": "anna"},
  "duration_seconds": 2712.4,
  "language": "en",
  "transcript": {
    "text": "Anna Schmidt: Hallo zusammen, ...\nTom Becker: Ich würde beim Preis bleiben ...",
    "speakers_labelled": true,
    "turns": [{"speaker": "Anna Schmidt", "start": 0.0, "end": 6.8, "text": "Hi everyone, ..."}]
  },
  "notes": {
    "title": "...", "language": "en", "summary": "...",
    "key_points": ["..."], "decisions": ["..."],
    "action_items": [{"task": "...", "owner": "Tom Becker", "due": "Friday"}],
    "open_questions": ["..."]
  },
  "notes_model": "claude-opus-5-5",
  "audio_url": "http://localhost:8333/crm/recordings/818b8fe97480/audio",
  "chat_notice": {"text": "Hallo, ich bin Notetaker. ...", "sent": true, "pinned": true, "posts": 1,
                  "sent_at": "2026-10-21T12:01:12+00:00", "error": null},
  "errors": {"recording": null, "transcript": null, "notes": null}
}
```

- `event` is `recording.completed` when the call was recorded, `recording.failed` when there
  is no audio (bot not admitted, refused by Google, stopped early). `errors` says why.
- A completed recording can still lack `notes` (no Claude key, refusal) or `transcript`;
  the matching `errors` field explains it.
- Answer with any 2xx. Anything else, or no answer within 15 s, is retried after 5 s, 30 s,
  2 min and 10 min.
- The audio isn't inline (115 MB per hour); fetch `audio_url` with the same token if needed.

### 3. Verify the callback (recommended)

With `NOTETAKER_WEBHOOK_SECRET` set in `.env`, each callback carries
`X-Notetaker-Timestamp` and `X-Notetaker-Signature: sha256=<HMAC-SHA256 of "<timestamp>.<raw body>">`.
Node/Express example; it needs the raw body, not the parsed JSON:

```js
import crypto from "node:crypto";
import express from "express";

app.post("/api/notetaker/webhook", express.raw({ type: "application/json" }), (req, res) => {
  const ts = req.get("X-Notetaker-Timestamp") ?? "";
  const sig = req.get("X-Notetaker-Signature") ?? "";
  const expected = "sha256=" + crypto
    .createHmac("sha256", process.env.NOTETAKER_WEBHOOK_SECRET)
    .update(`${ts}.${req.body}`)
    .digest("hex");
  const valid = sig.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
  if (!valid) return res.sendStatus(401);

  const result = JSON.parse(req.body);
  // result.metadata.deal_id, result.notes, result.transcript.text, result.errors ...
  res.sendStatus(200);
});
```

### Transcribe an audio file

```bash
curl -F "file=@call.mp3" http://localhost:8333/crm/transcribe
curl -F "file=@call.mp3" "http://localhost:8333/crm/transcribe?format=text"     # plain text
curl -F "file=@call.mp3" "http://localhost:8333/crm/transcribe?language=de"     # force a language
```

Send any common audio file (MP3, WAV at any sample rate, M4A/AAC, OGG, FLAC, or a video's
audio) as the multipart field `file`. The answer is the transcript:

```json
{
  "filename": "call.mp3",
  "language": "en",
  "duration_seconds": 42.35,
  "seconds_taken": 17.2,
  "model": "large-v3-turbo",
  "text": "Hi everyone, thanks for joining. ...\nI think we should keep the price at 49 euros ...",
  "segments": [{"start": 0.0, "end": 6.8, "text": "Hi everyone, thanks for joining. ..."}]
}
```

- Whisper runs locally in its own low-priority process; nothing leaves the machine and the upload
  is deleted when the transcript is done.
- The request stays open until the transcript is ready: about 1 minute per 2.3 minutes of
  audio on an M4, longer while a meeting is being transcribed (one transcription runs at a time).
  A one-hour file takes about 25 minutes, so give your HTTP client a long enough timeout.
- No speaker names: those come from Meet during a call, which an uploaded file doesn't have.
- **400** empty file, **413** larger than `NOTETAKER_MAX_UPLOAD_MB` (default 500), **422** not
  audio. Protected by `NOTETAKER_CRM_TOKEN` like the other `/crm` routes.

### Other CRM endpoints

```
GET    /crm/health                      can a bot join now / at ?at=... (see step 0)
POST   /crm/transcribe                  upload an audio file, get its transcript (see above)
GET    /crm/recordings                  CRM jobs, newest first; ?status=scheduled, ?external_id=...
GET    /crm/recordings/{id}             current state, same shape as the callback
GET    /crm/recordings/{id}/audio       the WAV
DELETE /crm/recordings/{id}             scheduled: cancel it (no callback). In the call: pull the bot out;
                                        what was recorded is still processed and sent.
POST   /crm/recordings/{id}/redeliver   send the callback again (e.g. after the CRM was down)
```

`status` over a job's life: `scheduled` → `queued` → `joining` → `waiting_for_admission` →
`recording` → `waiting_for_transcription` → `transcribing` → `summarizing` → `done`;
or `failed`, or `cancelled`. `waiting_for_transcription` means the call is over and the
recording is in the transcription queue.
In `GET` responses `event` is `recording.scheduled`, `recording.in_progress`,
`recording.completed`, `recording.failed` or `recording.cancelled`; callbacks are only sent
for `completed` and `failed`.

If a bot's process dies (Terminal closed, crash, reboot), Notetaker notices within seconds of its
next start. Audio captured up to that point is transcribed as a partial recording: the callback is
`recording.completed` with `errors.recording` starting with "partial recording". Without any
audio the job ends as `recording.failed`. The CRM gets the callback either way.

### Settings (.env)

| Setting | Effect |
|---|---|
| `NOTETAKER_CRM_TOKEN` | when set, every `/crm/*` call needs `Authorization: Bearer <token>` |
| `NOTETAKER_WEBHOOK_SECRET` | when set, callbacks are signed |
| `NOTETAKER_CALLBACK_HOSTS` | hosts a `callback_url` may use; default `localhost,127.0.0.1,::1` |
| `NOTETAKER_JOIN_LEAD_SECONDS` | how long before `join_at` the bot leaves; default 60 |
| `NOTETAKER_MISSED_AFTER_MINUTES` | how late a scheduled bot may still join after a restart; default 30 |
| `NOTETAKER_MAX_BOTS` | bots allowed in calls at once, used by `/crm/health`; default 5 |
| `NOTETAKER_MIN_FREE_GB` | free disk below which `/crm/health` reports `disk_low`; default 2 |

## Testing routes

No auth yet, so local testing only.

```bash
./start.sh
```

It listens on `127.0.0.1:8333`, so only this Mac can reach it. Set `NOTETAKER_HOST=0.0.0.0`
in `.env` to accept requests from other machines on the network, but only once the API has
authentication: anyone who can reach the port can send the bot into meetings.

Start a notetaker for a call, by Meet code or full link:

```
GET  http://localhost:8333/record/abc-defg-hij
GET  http://localhost:8333/record/https://meet.google.com/abc-defg-hij?name=Notetaker&max_minutes=120
POST http://localhost:8333/record   {"url": "https://meet.google.com/abc-defg-hij", "name": "Notetaker", "max_minutes": 120}
```

Response (202):

```json
{"id": "3eba46c2eb15", "status": "queued",
 "status_url": "http://localhost:8333/recordings/3eba46c2eb15",
 "audio_url":  "http://localhost:8333/recordings/3eba46c2eb15/audio"}
```

Then:

```
GET    /recordings/{id}             status (see below)
GET    /recordings/{id}/audio       the WAV, as soon as the call has ended
GET    /recordings/{id}/transcript  transcript JSON; ?format=text for plain text
GET    /recordings/{id}/notes       Claude notes JSON; ?format=markdown for Markdown
DELETE /recordings/{id}             pull the bot out of the call; audio captured so far is still processed
GET    /recordings                  all jobs
```

Status moves through `queued`, `joining`, `waiting_for_admission`, `recording`,
`waiting_for_transcription`, `transcribing`, `summarizing`, then `done`, or ends in `failed` if
the bot never recorded.
`done` means processing finished. If a step could not run, the status carries
`transcript_error` or `notes_error`, and that endpoint answers 424 with the reason.
Endpoints answer 409 while their file is still being made.

Each request runs one bot in its own process, so several calls can be recorded at once.
Per-job logs and screenshots are under `generated/<id>/`.

## If Google says "You can't join this video call"

Meetings hosted by a personal Google account (and some Workspace setups) refuse anonymous
guests, so the bot needs its own Google login. Create a free Google account for the bot,
then save its login once:

```bash
venv/bin/python -m notetaker.login   # Chrome opens: sign in as the bot; it saves and closes itself
```

While `storage_state.json` exists in the project folder, every request joins signed in as
that account (Meet shows the account's name, so name the account "Notetaker"). It still waits
in the lobby for the host to admit it, and runs without a visible window. Set
`NOTETAKER_HEADLESS=0` to see the window, or `NOTETAKER_STORAGE_STATE=/path/to/file.json`
to keep the login elsewhere. The status JSON reports `"mode": "guest"` or `"authenticated"`.

## Known limits

- Google Meet only. Zoom and Teams are next.
- A host must admit the bot from the lobby in both guest and signed-in mode.
- Guest mode needs a visible browser window, because Google blocks headless browsers.
- Current Meet shows two popups before joining (camera nudge and a sign-in tip). The
  backend closes them; if Google changes them again, check `generated/<id>/screenshots/`.
- Google can change the Meet UI and break the join flow until meeto is updated.

## Layout

```
notetaker/api.py                 FastAPI app (CRM route and testing routes)
notetaker/webhooks.py            builds, signs and delivers the CRM callback
notetaker/scheduler.py           launches scheduled bots on time, closes jobs whose worker died
notetaker/health.py              readiness checks behind GET /crm/health
notetaker/worker.py              one-bot subprocess started by the API: records, hands the WAV off
notetaker/transcriber.py         the transcription queue: Whisper + notes, one recording at a time
notetaker/jobs.py                job states in generated/<id>/status.json, WAV conversion, salvage
notetaker/postprocess.py         WAV -> transcript -> notes, writes the files next to the WAV
notetaker/transcribe.py          Whisper speech-to-text (local)
notetaker/speakers.py            labels transcript segments with Meet speaker names
notetaker/notes.py               Claude meeting notes (structured output)
notetaker/login.py               saves the bot's Google login (storage_state.json)
notetaker/config.py              loads .env for every entry point
notetaker/bootstrap.py           first start: installs the bot's browser, fetches the Whisper model
start.sh                         starts the API (host and port from .env)
notetaker/cli.py                 command line entrypoint (same job code, in-process)
notetaker/audio.py               PCM16 -> WAV
notetaker/backends/meet_meeto.py Google Meet backend (meeto)
notetaker/backends/meet_chat.py  recording notice in the meeting chat (post, pin, re-post)
```

New platforms go in `notetaker/backends/` behind the same `record()` signature.
