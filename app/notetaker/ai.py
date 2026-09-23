"""Talking to the AI: speech-to-text and the notes model.

Both go to one OpenAI-compatible provider (config.ai_provider): the AI of
this computer (Uno Work App API, via the vendored uno_app SDK), a Uno gateway
key, or whatever endpoint the person set in Settings. Local Whisper
(faster-whisper, on this computer's CPU) is an opt-in alternative for
speech-to-text.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from . import uno_app
from .config import ROUTE_APP, USER_AGENT, Provider, Settings, notes_model

log = logging.getLogger("notetaker.ai")
HTTP = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), headers={"User-Agent": USER_AGENT})


class AIError(RuntimeError):
    """A message a person can act on (shown in the UI as is)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


RETRYABLE = (429, 500, 502, 503, 504)


def explain(status: int, code: str, msg: str, what: str) -> AIError:
    """A person-readable error for a failed AI call (App API or gateway)."""
    if code == "app_limit_reached":
        return AIError(f"{what}: this app used its AI limit — raise it in Uno Work → Settings → Apps, "
                       "then press Retry.", status)
    if code == "key_limit_reached":
        return AIError(f"{what}: this app's Uno AI key used its spending limit — raise it on the app's "
                       "card in Uno Work (Apps), then press Retry.", status)
    if code == "ai_not_connected":
        return AIError(f"{what}: this computer is not connected to Uno AI yet — link it in Uno Work, "
                       "or choose another provider in Settings.", status)
    if code == "invalid_app_token":
        return AIError(f"{what}: Uno Work did not accept this app's AI key (it may have been revoked) — "
                       "check Uno Work → Settings → Apps.", status)
    if code == "ai_not_allowed":
        return AIError(f"{what}: this app is not allowed to use the AI of this computer — "
                       "check Uno Work → Settings → Apps.", status)
    if status in (401, 403):
        return AIError(f"{what}: the AI key was not accepted ({msg}). Check Settings → AI provider.", status)
    if status == 402:
        return AIError(f"{what}: not enough AI credits on your Uno account. Top up and press Retry.", 402)
    if status == 0:
        return AIError(f"{what} failed: {msg}", 0)
    return AIError(f"{what} failed (HTTP {status}): {msg}", status)


def _explain(resp: httpx.Response, what: str) -> AIError:
    code = ""
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            msg = err.get("message") or resp.text
            # OpenAI puts the machine code in "code"; the Uno gateway in "type".
            code = next((c for c in (err.get("code"), err.get("type")) if isinstance(c, str) and c), "")
        else:
            msg = err or resp.text
    except (ValueError, AttributeError):
        msg = resp.text[:200]
    return explain(resp.status_code, code, str(msg), what)


def _app_error(exc: uno_app.UnoAppError, what: str) -> AIError:
    return explain(exc.status, exc.code, exc.message, what)


def _app_client(p: Provider) -> uno_app.Client:
    return uno_app.Client(url=p.base_url, token=p.api_key, timeout=180.0)


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
    if p.route == ROUTE_APP:
        return _transcribe_app(p, data, path)
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
        return _segments(resp.json())
    raise AIError("Transcription failed after retries")


def _segments(body: dict) -> dict:
    segs = body.get("segments")
    return {"text": body.get("text", ""),
            "segments": [{"start": float(x.get("start", 0)), "end": float(x.get("end", 0)),
                          "text": x.get("text", "")} for x in segs] if isinstance(segs, list) else None}


def _transcribe_app(p: Provider, data: dict, path: str) -> dict:
    """Same as the gateway path, through the Uno Work App API."""
    client = _app_client(p)
    for attempt in range(5):
        try:
            body = client.transcribe_json(path, filename="chunk.ogg", model=data["model"],
                                          language=data.get("language"),
                                          response_format=data["response_format"])
        except uno_app.UnoAppError as exc:
            log.debug("stt(app) %s attempt=%d → %d %s", os.path.basename(path), attempt, exc.status, exc.code)
            if exc.status == 400 and data["response_format"] == "verbose_json" \
                    and "response_format" in exc.message:
                data["response_format"] = "json"
                _no_verbose[p.base_url] = time.time()
                continue
            transient = exc.status in RETRYABLE or exc.code == "unreachable"
            if transient and exc.code != "ai_not_connected" and attempt < 3:
                time.sleep(2 + attempt * 3)
                continue
            raise _app_error(exc, "Transcription") from None
        return _segments(body)
    raise AIError("Transcription failed after retries")


_no_verbose: dict[str, float] = {}  # provider → when it said verbose_json isn't supported
_local_lock = threading.Lock()
_local_model = None
_local_name = ""


def _local(s: Settings):
    global _local_model, _local_name
    try:
        from faster_whisper import WhisperModel  # heavy import only when used
    except ImportError as exc:  # pragma: no cover
        raise AIError(f"Local Whisper is not available in this build: {exc}") from exc
    name = s.local_model if s.local_model in ("tiny", "base", "small") else "base"
    if _local_model is None or _local_name != name:
        _local_model = WhisperModel(name, device="cpu", compute_type="int8",
                                    download_root="/state/whisper-models")
        _local_name = name
    return _local_model


def transcribe_local(s: Settings, path: str) -> str:
    """faster-whisper on this computer's CPU, one utterance-sized chunk.

    Same chunks as the gateway path (cut at pauses), so a line is one person's
    utterance whichever engine transcribed it. One chunk at a time: the model
    holds 0.2–0.9 GB of RAM while it works.
    """
    with _local_lock:
        model = _local(s)
        segments, _info = model.transcribe(
            path, language=s.language if s.language in ("ru", "en") else None,
            vad_filter=False, beam_size=1, condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments)


# ---- the notes model -------------------------------------------------------

def chat(p: Provider, s: Settings, messages: list[dict], max_tokens: int = 6000,
         temperature: float = 0.2) -> tuple[str, dict]:
    if not p.ready:
        raise AIError("No AI key. On a Uno computer it is set up automatically; "
                      "elsewhere choose a provider in Settings.")
    model = notes_model(s, p)
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature}
    for attempt in range(3):
        if p.route == ROUTE_APP:
            try:
                data = _app_client(p).chat(body)
            except uno_app.UnoAppError as exc:
                transient = exc.status in RETRYABLE or exc.code == "unreachable"
                if transient and exc.code != "ai_not_connected" and attempt < 2:
                    time.sleep(2 + attempt * 3)
                    continue
                raise _app_error(exc, "AI notes") from None
        else:
            resp = HTTP.post(f"{p.base_url}/chat/completions",
                             headers={"Authorization": f"Bearer {p.api_key}"}, json=body)
            if resp.status_code in RETRYABLE and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            if resp.status_code != 200:
                raise _explain(resp, "AI notes")
            data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        used = data.get("model") or model
        if not text.strip():
            raise AIError(f"{used} returned no text (thinking models can spend the whole budget "
                          "on reasoning). Pick another notes model in Settings and press Retry.")
        usage = dict(data.get("usage") or {})
        usage["model"] = used
        return text.strip(), usage
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
