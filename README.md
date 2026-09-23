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
                 pipeline (recording → transcript → notes), store (~/Meetings),
                 uno_app.py (vendored Uno App SDK)
app/tests/       unit tests: python3 -m unittest discover -s app/tests
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
