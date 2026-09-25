# Uno Notetaker

Meeting notes for your Uno computer — a small, self-hosted Granola / Otter.

- **Record** a call in another browser tab (Google Meet, Zoom, Teams on the web):
  the tab's audio and your microphone are recorded as two tracks, so the
  transcript knows what *you* said. Or record a meeting in the room (mic only).
- **Upload** an audio or video file.
- **Transcript** with timestamps, one line per utterance (audio is cut at
  pauses). **Speakers** are told apart by voice: a speaker-embedding model
  (NeMo TitaNet-small via sherpa-onnx, 40 MB, CPU) groups the lines, then the
  notes model names each group from the conversation ("Oleg", "Sarah", or
  "Speaker 2"). A call recorded in the browser also knows which lines are *yours*.
- **Notes**: summary, decisions, action items with owners, open questions.
  Kinds: general, client call, stand-up, interview. Rewrite any time.
- **Ask about this meeting** — chat over the transcript.
- **Share** a read-only link to the notes (optionally with the transcript).
- Every meeting is a folder in `~/Meetings` (audio, `transcript.md`,
  `notes.md`), so Files and the AI on the computer see it.
- **Action items** are checkboxes: tick them off in the meeting or in the
  **Action items** view across all meetings (it is the `[x]` in `notes.md`,
  so editing the file works too). **Ask Uno to do it** hands one item to the
  AI of the computer — a Uno Work chat (App API `/v1/tasks`, tools `ask`:
  it asks before changing anything) that drafts the email or document.
- **When notes are ready** the app tells you: the **Uno Work Inbox** (the
  bell; App API `/v1/notify`, "Notes ready · 3 action items", Open → the
  meeting) and **Telegram**.
- **Telegram**: your own bot (token from @BotFather, Settings → Telegram,
  paired by a one-time link, then it talks to that chat only). Send it a voice
  message or a recording (≤ 20 MB — the Bot API limit) and the notes come back
  as a reply; `/todo`, `/last`, `/find words`. Long polling: nothing on the
  computer has to be reachable from the internet.
- **Search across meetings**: every word must match; hits show the line of
  the notes or the transcript (with its time — click to jump there).
- **Speakers**: rename one ("Speaker 1" → "Mike") everywhere — transcript and
  notes; talk time per person.
- **Home widget** in Uno Work: "Last meeting · 3 action items" + the first
  three (manifest `widget`, a read-only key in the widget URL because the
  frame is third-party and gets no login cookie).
- **Phone**: one screen at a time, Record/Upload at the bottom, recording on
  iPhone (Safari records mp4, not webm) with the screen kept awake; installable
  (Add to Home Screen) and, on Android, a **Share target** for recordings.

## AI

By default everything goes through **Uno AI** — nothing to set up on a Uno
computer. Which way it goes is shown in Settings ("Now in use: …"):

1. **AI of this computer (Uno Work)** — the app's manifest asks for AI
   (`"ai": {"chat": true, "tasks": false, "limitUsd": 10}`), Uno Work gives the
   app its own key and the app calls the local **Uno App API** through the
   vendored SDK (`app/notetaker/uno_app.py`, from uno-work `sdk/python` — keep
   in sync). Spending is metered per app; the limit ($10 unless the person
   raises it) is in Uno Work → Settings → Apps. With no notes model chosen,
   the computer's choice for apps is used (`model: "default"`).
2. **Uno gateway key** — fallback for computers whose Uno Work has no App API
   yet: `UNO_LLM_API_KEY` (an App Store key just for this app) or the
   computer's own key from the Uno Work settings file.
3. **Custom** — Settings → AI provider: any OpenAI-compatible endpoint + key.

Speech-to-text can also run **on the computer** (faster-whisper
tiny/base/small, CPU) — free, slower.

| step | default | cost on Uno AI (measured 23.09) |
|---|---|---|
| speech-to-text | whisper-large-v3 via gateway, utterance chunks, silence never sent | ~$0.03 per hour of speech |
| notes + naming speakers | `deepseek/deepseek-v3.2` | ~$0.015 per hour of meeting |
| Ask about the meeting | same model, transcript in context | ~$0.007 per question for a 1-hour meeting |

If Uno AI refuses the key for speech-to-text (401/403 — the App API, or Uno
gateways before fishcode `bfcd2d6` that accept `unollm_` keys only on chat),
the app transcribes on the computer with faster-whisper instead and says so —
it keeps working, at ~20 s per minute of audio on 2 vCPU (base model, ~400 MB
RAM peak). When the app used its AI limit, the error says to raise it in
Uno Work → Settings → Apps.

## Run

```sh
ADMIN_PASSWORD=… docker compose up -d --build     # → :8430
```

On a Uno computer the compose file mounts:

- `~/Meetings` → `/meetings`;
- `~/.uno/apps` → `/uno-dot/apps` — the app writes its manifest
  `notetaker.json` there (it appears on the Uno Work desktop and asks for AI);
- `~/.uno/app-keys/notetaker` → `/run/uno-app:ro` — this app's own key
  (`token` + `api.json`), which the SDK reads; the App API is reached at
  `host.docker.internal` (`extra_hosts: ["host.docker.internal:host-gateway"]`);
- read-only, the Uno Work state directory — only for the gateway-key fallback.

**Do not mount the whole `~/.uno`**: it contains the AI keys of every other
app on the computer. A one-shot `uno-dirs` step creates the two `~/.uno`
folders owned by the computer's user first — otherwise docker creates a
missing bind folder as root and Uno Work cannot write the key into it.

Elsewhere: set `UNO_HOME` to a folder for `Meetings`, and either
`UNO_LLM_API_KEY` or a provider in Settings.

## Layout

```
app/notetaker/   FastAPI backend: config (keys, AI route), audio (ffmpeg), ai (STT/LLM),
                 pipeline (recording → transcript → notes), store (~/Meetings, search),
                 actions (action items from notes.md), deliver (Inbox, tasks), telegram (bot),
                 uno_app.py (vendored Uno App SDK)
app/tests/       unit tests: python3 -m unittest discover -s app/tests
app/static/      the UI (plain JS, no build)
testdata/        TTS test meetings (RU/EN) + smoke.py (end-to-end over the API)
                 + smoke_v2.py (action items, Inbox, tasks, search, widget, Telegram — against fakes)
```

## Security

- `ADMIN_PASSWORD` is required (the container refuses to start without it); a
  signed session cookie, 10 wrong passwords per 5 minutes per IP.
- Shared links are random 24-char tokens, revocable; the public page has a
  `default-src 'none'` CSP (notes are Markdown written by an LLM or the owner).
- The container drops root after fixing folder ownership and runs as the
  computer's user; the Uno Work state dir is mounted read-only.
- Voice fingerprints are computed on the computer and never stored or sent.

## Not yet

- **A bot that joins the call** (Google Meet first). See `docs/bot-plan.md`:
  Meet/Teams make the host admit a guest bot, Zoom needs an approved
  Marketplace app (since 03.2026). Record the call's tab instead.
- Live transcript during the meeting (now: after Stop).
- Deleting audio after N days, per-meeting sharing of audio.
- Renaming speakers by hand (the AI names are right most of the time, not always).
