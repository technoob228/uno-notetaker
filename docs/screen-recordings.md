# Screen recordings from Uno Work (Loom Work) — 0.6.0

A recording made in Uno Work («Show the agent» / the record button in a chat)
lands in the same library as calls: Uno Work watches it (speech + what was on
screen, second by second) and writes a ready folder into `~/Meetings`. The app
notices it within ~10 s, writes the notes (action items with **Ask Uno to do
it** included), sends «Notes ready» to the Inbox and Telegram like for a call.

Uno Work writes the folder only when `~/Meetings` exists (the app is
installed); the video itself stays in Uno Drive (`Recordings/`).

## The folder (written by Uno Work, meeting.json last)

`~/Meetings/<YYYY-MM-DD HH-MM> Screen recording/` — renamed to the AI's title
after the notes, like «Meeting».

`transcript.json` — one line per moment of the recording:

```json
[{"start": 4, "end": 9, "speaker": "Me", "text": "Вот тут криво.",
  "screen": "the Save button sits below the form", "did": "clicked Save"}]
```

`speaker` is `"Me"` when something was said, `""` for a silent moment;
`screen` / `did` may be empty.

`transcript.md` — the same for people and Files (`_On screen: …_`, `_Did: …_`).

`meeting.json`:

```json
{"id": "<10 × [a-z0-9]>", "title": "", "created_at": "<ISO with offset>",
 "source": "screen", "template": "screen", "language": "auto",
 "status": "summarizing", "progress": "Writing notes…", "error": "",
 "duration": 74, "tracks": {}, "share": null, "chat": [], "usage": {},
 "folder": "<folder name>",
 "screen": {"drive_key": "Recordings/<name>.webm", "share_url": "<Uno Drive link>",
            "share_expires_at": "<ISO>", "width": 1280, "height": 800, "from": "uno-work"}}
```

## In the app

- The list marks it **Screen**; the meeting page plays the video from the Uno
  Drive link (WebM, the MP4 copy for Safari/iPhone) and follows the transcript
  as it plays. After the link expires (7 days) the page says where the file is.
- The transcript shows «On screen: …» / «Did: …» under each line; the notes
  model gets them as `[screen: …] [did: …]` and the «Screen recording» kind
  (a «What was shown» section; action items are what the person wants fixed).
- «Screen recording» is not offered for new meetings (it comes from Uno Work).
