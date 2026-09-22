"""Talking to the AI: speech-to-text and the notes model.

Both go to one OpenAI-compatible provider — the Uno Gateway by default, or
whatever endpoint the person set in Settings. Local Whisper (faster-whisper,
on this computer's CPU) is an opt-in alternative for speech-to-text.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from .config import USER_AGENT, Provider, Settings

log = logging.getLogger("notetaker.ai")
HTTP = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), headers={"User-Agent": USER_AGENT})


class AIError(RuntimeError):
    """A message a person can act on (shown in the UI as is)."""


def _explain(resp: httpx.Response, what: str) -> AIError:
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or resp.text)
    except ValueError:
        msg = resp.text[:200]
    if resp.status_code == 401:
        return AIError(f"{what}: the AI key was not accepted ({msg}). Check Settings → AI provider.")
    if resp.status_code == 402:
        return AIError(f"{what}: not enough AI credits on your Uno account. Top up and press Retry.")
    return AIError(f"{what} failed (HTTP {resp.status_code}): {msg}")


# ---- speech-to-text --------------------------------------------------------

# Whisper invents these on near-silence (YouTube subtitle credits it was trained on).
_HALLUCINATIONS = re.compile(
    r"^(продолжение следует|субтитры (сделал|создавал|делал)|редактор субтитров|"
    r"спасибо за просмотр|thanks? for watching|thank you\.?$|you$|\.+$|subtitles by)",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text or _HALLUCINATIONS.match(text):
        return ""
    return text


def transcribe_remote(p: Provider, s: Settings, path: str) -> dict:
    """→ {"text": str, "segments": [{start,end,text}] | None} for one chunk."""
    if not p.ready:
        raise AIError("No AI key. On a Uno computer it is set up automatically; "
                      "elsewhere choose a provider in Settings.")
    fmt = "json" if time.time() - _no_verbose.get(p.base_url, 0) < 3600 else "verbose_json"
    data = {"model": s.stt_model or "whisper-1", "response_format": fmt}
    if s.language in ("ru", "en"):
        data["language"] = s.language
    for attempt in range(5):
        with open(path, "rb") as fh:
            resp = HTTP.post(f"{p.base_url}/audio/transcriptions",
                             headers={"Authorization": f"Bearer {p.api_key}"},
                             data=data, files={"file": ("chunk.ogg", fh, "audio/ogg")})
        log.debug("stt %s attempt=%d fmt=%s → %d %s", os.path.basename(path), attempt,
                 data.get("response_format"), resp.status_code, resp.text[:120].replace("\n", " "))
        if resp.status_code == 400 and data.get("response_format") == "verbose_json" \
                and "response_format" in resp.text:
            data["response_format"] = "json"  # gateway without timestamps: chunk start is enough
            _no_verbose[p.base_url] = time.time()
            continue
        # a request the edge mangled in transit: worth one more try
        transient = resp.status_code == 400 and "multipart" in resp.text
        if (transient or resp.status_code in (429, 500, 502, 503, 504)) and attempt < 3:
            time.sleep(2 + attempt * 3)
            continue
        if resp.status_code != 200:
            raise _explain(resp, "Transcription")
        body = resp.json()
        segs = body.get("segments")
        return {"text": body.get("text", ""),
                "segments": [{"start": float(x.get("start", 0)), "end": float(x.get("end", 0)),
                              "text": x.get("text", "")} for x in segs] if isinstance(segs, list) else None}
    raise AIError("Transcription failed after retries")


_no_verbose: dict[str, float] = {}  # provider → when it said verbose_json isn't supported
_local_lock = threading.Lock()
_local_model = None
_local_name = ""


def transcribe_local(s: Settings, wav_path: str) -> list[dict]:
    """faster-whisper on this computer's CPU → segments with timestamps.

    The whole file at once (it does its own voice detection); one job at a
    time — the model holds 0.3–1 GB of RAM while it works.
    """
    global _local_model, _local_name
    try:
        from faster_whisper import WhisperModel  # heavy import only when used
    except ImportError as exc:  # pragma: no cover
        raise AIError("Local Whisper is not installed in this build") from exc
    name = s.local_model if s.local_model in ("tiny", "base", "small") else "base"
    with _local_lock:
        if _local_model is None or _local_name != name:
            _local_model = WhisperModel(name, device="cpu", compute_type="int8",
                                        download_root="/state/whisper-models")
            _local_name = name
        segments, _info = _local_model.transcribe(
            wav_path, language=s.language if s.language in ("ru", "en") else None,
            vad_filter=True, beam_size=1)
        return [{"start": seg.start, "end": seg.end, "text": seg.text} for seg in segments]


# ---- the notes model -------------------------------------------------------

def chat(p: Provider, s: Settings, messages: list[dict], max_tokens: int = 3000,
         temperature: float = 0.2) -> tuple[str, dict]:
    if not p.ready:
        raise AIError("No AI key. On a Uno computer it is set up automatically; "
                      "elsewhere choose a provider in Settings.")
    body = {"model": s.model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature}
    for attempt in range(3):
        resp = HTTP.post(f"{p.base_url}/chat/completions",
                         headers={"Authorization": f"Bearer {p.api_key}"}, json=body)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 + attempt * 3)
            continue
        if resp.status_code != 200:
            raise _explain(resp, "AI notes")
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return text.strip(), data.get("usage") or {}
    raise AIError("AI notes failed after retries")


TEMPLATES = {
    "general": {
        "name": "General meeting",
        "extra": "",
    },
    "client": {
        "name": "Client call",
        "extra": ("Add sections: 'Client needs & pains', 'Objections / risks', "
                  "'Commercial terms mentioned' (prices, discounts, dates), 'Next steps with the client'."),
    },
    "standup": {
        "name": "Stand-up",
        "extra": ("Add a section 'By person' with, for each participant: done / doing next / blockers. "
                  "Keep it terse."),
    },
    "interview": {
        "name": "Interview",
        "extra": ("Add sections: 'Candidate background', 'Strengths', 'Concerns', "
                  "'Notable answers' (quote briefly), 'Recommendation' (hire / no hire / next round, with why — "
                  "state it is the AI's reading, the decision is the interviewer's)."),
    },
}

LANG_NAMES = {"ru": "Russian", "en": "English"}


def notes_prompt(template: str, notes_language: str, two_track: bool = False) -> str:
    t = TEMPLATES.get(template, TEMPLATES["general"])
    lang = (f"Write the notes in {LANG_NAMES[notes_language]}."
            if notes_language in LANG_NAMES else
            "Write the notes in the language most of the meeting was spoken in.")
    return f"""You are a meeting notetaker. You get a transcript with [mm:ss] timestamps (and speaker names when known).
Write concise, factual meeting notes in Markdown. {lang}

Format exactly:
# <short meeting title, max 8 words>

## Summary
3–6 bullet points: what the meeting was about and what came out of it.

## Decisions
- each decision that was actually agreed (omit the section content with "—" if none)

## Action items
- [ ] **<owner or "—">**: <task> (due: <date if said>) [mm:ss]

## Open questions
- questions raised but not resolved

{t['extra']}

{"Lines marked 'Me' were said by the person these notes are for; call them 'You' (in the notes language). Lines marked 'Others' are the other participants: use their names when the conversation gives them." if two_track else ""}
Rules: only facts present in the transcript; never invent names, numbers, or dates; keep numbers exact;
refer to people by name when the transcript gives one, otherwise by their role or speaker label;
add the [mm:ss] timestamp where a decision or task was stated."""


def ask_prompt(title: str, transcript_md: str, notes_md: str) -> str:
    return f"""You answer questions about one meeting, "{title}". Use only the transcript and notes below.
Quote timestamps [mm:ss] for claims. If the answer is not in the meeting, say so plainly.
Answer in the language of the question.

=== NOTES ===
{notes_md or '(not generated yet)'}

=== TRANSCRIPT ===
{transcript_md}"""
