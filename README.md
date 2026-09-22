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

## AI

By default everything goes through **Uno AI** (the Uno LLM gateway) with the
computer's own key — nothing to set up on a Uno computer. Settings → AI provider
switches to any OpenAI-compatible endpoint + key. Speech-to-text can also run
**on the computer** (faster-whisper tiny/base/small, CPU) — free, slower.

| step | default | cost on Uno AI (measured 23.09) |
|---|---|---|
| speech-to-text | whisper-large-v3 via gateway, utterance chunks, silence never sent | ~$0.03 per hour of speech |
| notes + naming speakers | `deepseek/deepseek-v3.2` | ~$0.015 per hour of meeting |
| Ask about the meeting | same model, transcript in context | ~$0.007 per question for a 1-hour meeting |

If the gateway refuses the computer's key for speech-to-text (Uno gateways
before fishcode `bfcd2d6` accept `unollm_` keys only on chat), the app
transcribes on the computer with faster-whisper instead and says so — it keeps
working, at ~20 s per minute of audio on 2 vCPU (base model, ~400 MB RAM peak).

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

## Security

- `ADMIN_PASSWORD` is required (the container refuses to start without it); a
  signed session cookie, 10 wrong passwords per 5 minutes per IP.
- Shared links are random 24-char tokens, revocable; the public page has a
  `default-src 'none'` CSP (notes are Markdown written by an LLM or the owner).
- The container drops root after fixing folder ownership and runs as the
  computer's user; the Uno Work state dir is mounted read-only.
- Voice fingerprints are computed on the computer and never stored or sent.

## Not yet

- **A bot that joins the call** (Google Meet first). See `docs/bot-plan.md`.
- Live transcript during the meeting (now: after Stop).
- Deleting audio after N days, per-meeting sharing of audio.
- Renaming speakers by hand (the AI names are right most of the time, not always).
