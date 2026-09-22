# Uno Notetaker

Meeting notes for your Uno computer — a small, self-hosted Granola / Otter.

- **Record** a call in another browser tab (Google Meet, Zoom, Teams on the web):
  the tab's audio and your microphone are recorded as two tracks, so the
  transcript knows what *you* said. Or record a meeting in the room (mic only).
- **Upload** an audio or video file.
- **Transcript** with timestamps; speakers are told apart (two tracks, or the
  AI guesses from what was said for a single recording).
- **Notes**: summary, decisions, action items with owners, open questions.
  Kinds: general, client call, stand-up, interview. Rewrite any time.
- **Ask about this meeting** — chat over the transcript.
- **Share** a read-only link to the notes (optionally with the transcript).
- Every meeting is a folder in `~/Meetings` (audio, `transcript.md`,
  `notes.md`), so Files and the AI on the computer see it.

## AI

By default everything goes through **Uno AI** (the Uno LLM gateway) with the
computer's own key — nothing to set up on a Uno computer. Settings → AI provider
switches to any OpenAI-compatible endpoint + key. Speech-to-text can also run
**on the computer** (faster-whisper tiny/base/small, CPU) — free, slower.

| step | default | cost on Uno AI |
|---|---|---|
| speech-to-text | whisper-large-v3 via gateway, cut at pauses | ~$0.03 per hour of speech |
| notes, speakers, Ask | `deepseek/deepseek-v3.2` | ~$0.01 per hour of meeting |

## Run

```sh
ADMIN_PASSWORD=… docker compose up -d --build     # → :8430
```

On a Uno computer the compose file mounts `~/Meetings`, `~/.uno` (the app
registers itself in `~/.uno/apps/notetaker.json` and appears on the Uno Work
desktop) and, read-only, the Uno Work state directory to read the computer's
AI key. `UNO_LLM_API_KEY` in the environment (the App Store can mint a key just
for this app) takes precedence.

Elsewhere: set `UNO_HOME` to a folder for `Meetings`, and either
`UNO_LLM_API_KEY` or a provider in Settings.

## Layout

```
app/notetaker/   FastAPI backend: config (keys), audio (ffmpeg), ai (STT/LLM),
                 pipeline (recording → transcript → notes), store (~/Meetings)
app/static/      the UI (plain JS, no build)
testdata/        TTS test meetings (RU/EN) + smoke.py (end-to-end over the API)
```

## Not yet

- **A bot that joins the call** (Google Meet first). See `docs/bot-plan.md`.
- Live transcript during the meeting (now: after Stop).
