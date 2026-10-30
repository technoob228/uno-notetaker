# Uno Notetaker

Meeting notes for your Uno computer — a small, self-hosted Granola / Otter.

- **A new look** (0.8.0): one calm library like Granola. Meetings by day on
  the left with a short summary line, the notes as a clean document (action
  items on top, a floating player, Notes / Transcript / Ask), one **New
  meeting** menu for every way to start, and Settings as a sheet with the rare
  options (AI, speech-to-text, defaults) under Advanced. Light and dark themes.
- **A bot joins your calls** — Google Meet, Zoom, Microsoft Teams, Yandex
  Telemost, Zoho Meeting — as a guest named "Uno Notetaker", by itself for the
  calls in your calendar or when you send it by link. It records the call and
  leaves when it ends; the notes come like for any recording. See **Calls** in
  the app and `docs/bot.md`.
- **Calendars from your Uno account** (0.7.0): Google Calendar and Outlook &
  Teams connected in Uno Work are used as they are — nothing to paste, no
  assistant needed (Settings → Calendars → "From your Uno account"; **Connect**
  opens Uno Work's own Connect). Any other calendar — Yandex, iCloud, Zoho,
  Outlook without Uno — by its secret iCal address, with step-by-step
  instructions per service in the app. The same meeting in two calendars gets
  one bot.
- **Record** a call in another browser tab (Google Meet, Zoom, Teams on the web):
  the tab's audio and your microphone are recorded as two tracks, so the
  transcript knows what *you* said. Or record a meeting in the room (mic only).
- **Upload** an audio or video file.
- **Screen recordings from Uno Work** (0.6.0): a recording made with «Show the
  agent» in Uno Work lands here too, next to the calls — the video (from Uno
  Drive), what was said *and what was on screen* line by line, notes and action
  items with **Ask Uno to do it**. See `docs/screen-recordings.md`.
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
app/notetaker/   FastAPI backend: config (keys, AI route), sso (Sign in with Uno), audio (ffmpeg), ai (STT/LLM),
                 pipeline (recording → transcript → notes), store (~/Meetings, search),
                 actions (action items from notes.md), deliver (Inbox, tasks), telegram (bot),
                 calendars (iCal links + the Uno account's Google / Outlook & Teams → calls,
                 one bot per call), assistant ("Join my calls" of a Uno Work assistant),
                 bot (the call bot's sessions, its API),
                 uno_app.py (vendored Uno App SDK)
bot/             the call bot: notetaker_bot/ (platforms.py — how to join each service,
                 session.py — one call, capture.py — PulseAudio + ffmpeg)
app/tests/       unit tests: python3 -m unittest discover -s app/tests
                 (test_uno_calendars.py — the Uno account's calendars against a fake App API)
app/static/      the UI (plain JS, no build)
testdata/        TTS test meetings (RU/EN) + smoke.py (end-to-end over the API)
                 + smoke_v2.py (action items, Inbox, tasks, search, widget, Telegram — against fakes)
```

## Security

- **Sign in with Uno** (0.4.2): with `UNO_OIDC_*` in the environment (the App
  Store registers each install as an OIDC client of the Uno console and passes
  them) the app goes straight to Uno — `/auth/uno/start` → the console →
  `/auth/uno/callback` (authorization code + PKCE S256, state, nonce; the
  `id_token` is checked: RS256 against the console's JWKS, iss, aud/azp, exp).
  The console lets in only the computer's owner and people they shared the app
  with. A good sign-in sets the same session cookie as the password. The
  password stays as the fallback: `/login?password=1` ("Use the owner
  password"); sign-in errors land there too. Without `UNO_OIDC_*` — password
  only, as before. Code: `app/notetaker/sso.py`, tests: `app/tests/test_sso.py`.
- `ADMIN_PASSWORD` is required (the container refuses to start without it); a
  signed session cookie, 10 wrong passwords per 5 minutes per IP.
- Over https the cookies are `Secure; HttpOnly; SameSite=None; Partitioned` —
  sign-in has to hold inside Uno Work's frame (another site). So the browser no
  longer stops cross-site forms: every POST/PUT/PATCH/DELETE carrying an
  `Origin` of another origin is refused (403). Over plain http — `SameSite=Lax`.
- Shared links are random 24-char tokens, revocable; the public page has a
  `default-src 'none'` CSP (notes are Markdown written by an LLM or the owner).
- The container drops root after fixing folder ownership and runs as the
  computer's user; the Uno Work state dir is mounted read-only.
- Voice fingerprints are computed on the computer and never stored or sent.
- Calendar links are secrets: kept in the state volume, shown masked. The Uno
  account's calendars come through the local App API with this app's own key —
  no OAuth tokens in the app; Uno Work keeps them. The bot
  container holds no keys — only a token that opens `/api/bot/worker/*`.

## The bot that joins calls

The `bot` service (`./bot`: Chromium on a virtual screen, PulseAudio, ffmpeg)
joins calls as a guest and streams their sound to the app; the app keeps the
calendars and the sessions and makes the notes. Details, what was tested on
each platform and the limits: `docs/bot.md`.

- **Calls** page: the bot's state, calls coming up (a switch per call), recent
  calls with what happened ("not let in", "what the bot saw" — a screenshot).
- Settings → **Calls and the bot**: join every call with a link / only accepted
  ones / only when sent; the bot's name; minutes early; how long to wait in the
  lobby; your email (to skip calls you declined); a hello in the call's chat.
- The host has to let a guest in (Meet, Teams, Zoom waiting room) — like with
  Otter or Fireflies. KakaoTalk group calls have no browser link: no bot there.
- Each call is a Chromium of ~0.5–1 GB: `BOT_MAX_CALLS` (2) at once. On a
  small computer run without it: `docker compose up -d notetaker`.

## Not yet

- Live transcript during the meeting (now: after Stop).
- Deleting audio after N days, per-meeting sharing of audio.
- Renaming speakers by hand (the AI names are right most of the time, not always).
