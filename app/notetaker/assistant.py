"""An assistant in Uno Work sends this Notetaker to the person's calls ("Join my calls").

Uno Work keeps the switch: which assistant, the bot's name ("Ada (Uno notes)"),
which meetings ("mine": the person is in it, somebody else is invited, it has a
call link). This side asks for it through the local Uno App API (manifest
"calls": true):

  GET  /v1/assistant/link     — is it on, for which assistant, per-meeting choices
  GET  /v1/calendar/events    — the person's Google Calendar through Uno (calendars.py)
  POST /v1/assistant/events   — what happened: upcoming calls, a call's status,
                                the notes after it (Work turns them into a turn of the
                                assistant, an Inbox item and a Telegram message)

Events go through a small outbox in the state volume: Uno Work restarting, or
the computer waking up, never loses "Couldn't join" or the notes. Each event
has an id; Work ignores a repeated id.

Without Uno Work (or an older one: 404 on the link) nothing here is active and
the app works as before — its own calendars (iCal links), Inbox, Telegram.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from . import uno_app
from .config import STATE_DIR, app_api_config

log = logging.getLogger("notetaker.assistant")

LINK_REFRESH_S = 60
LOOP_S = 5
OUTBOX_MAX = 300
OUTBOX_TTL_S = 24 * 3600
UPCOMING_EVERY_S = 30

_lock = threading.RLock()
_link: dict = {"enabled": False, "checked": 0.0, "supported": None, "error": ""}
_wake = threading.Event()


# ---- the link ------------------------------------------------------------------------

def _client() -> uno_app.Client | None:
    cfg = app_api_config()
    return uno_app.Client(url=cfg["url"], token=cfg["token"], timeout=20.0) if cfg else None


def _link_path() -> str:
    return os.path.join(STATE_DIR, "assistant-link.json")


def _load_link() -> dict:
    try:
        with open(_link_path()) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def refresh_link(force: bool = False) -> dict:
    """Ask Uno Work (at most once a minute). Keeps the last answer when Work is
    unreachable for a moment — a restart of Work must not drop the bot from
    a call it is about to join."""
    with _lock:
        if not force and time.time() - _link["checked"] < LINK_REFRESH_S:
            return dict(_link)
        _link["checked"] = time.time()
    client = _client()
    if not client:
        with _lock:
            _link.update(enabled=False, supported=False, error="no Uno Work on this computer")
        return dict(_link)
    try:
        got = client.assistant_link()
    except uno_app.UnoAppError as exc:
        with _lock:
            if exc.status in (404,) or exc.code in ("not_found", "calls_not_allowed"):
                # An older Uno Work (no such route) or a manifest without "calls".
                _link.update(enabled=False, supported=False, error=exc.message)
            elif exc.code == "calls_off" or exc.status == 409:
                _link.update(enabled=False, supported=True, error="")
                _save_link_locked()
            else:
                stale = _load_link()
                _link.update({**stale, "checked": _link["checked"], "error": exc.message})
        return dict(_link)
    with _lock:
        _link.clear()
        _link.update(normalize_link(got), checked=time.time(), supported=True, error="")
        _save_link_locked()
    return dict(_link)


def _save_link_locked() -> None:
    try:
        _write_json(_link_path(), {k: v for k, v in _link.items() if k not in ("checked",)})
    except OSError as exc:
        log.warning("could not keep the assistant link: %s", exc)


def normalize_link(got: dict) -> dict:
    got = got if isinstance(got, dict) else {}
    a = got.get("assistant") if isinstance(got.get("assistant"), dict) else {}
    owner = got.get("owner") if isinstance(got.get("owner"), dict) else {}
    name = str(a.get("name") or "").strip()[:60]
    bot_name = str(got.get("botName") or "").strip()[:60] or (f"{name} (Uno notes)" if name else "Uno notes")
    choices = {}
    for k, v in (got.get("choices") or {}).items():
        if isinstance(v, dict) and v.get("choice") in ("join", "skip", "") and re.match(r"^[a-f0-9]{6,40}$", str(k)):
            choices[str(k)] = {"choice": v.get("choice") or "", "at": int(v.get("at") or 0)}
    return {
        "enabled": bool(got.get("enabled")),
        "assistant": {"projectId": str(a.get("projectId") or ""), "name": name},
        "owner": {"name": str(owner.get("name") or "").strip()[:60],
                  "email": str(owner.get("email") or "").strip().lower()[:200]},
        "bot_name": bot_name,
        "rule": got.get("rule") if got.get("rule") in ("mine", "accepted", "all") else "mine",
        "choices": choices,
    }


def link() -> dict:
    with _lock:
        return dict(_link)


def active() -> bool:
    """An assistant sends this Notetaker to the person's calls."""
    with _lock:
        return bool(_link.get("enabled"))


def bot_name() -> str:
    with _lock:
        return str(_link.get("bot_name") or "") if _link.get("enabled") else ""


def owner_name() -> str:
    with _lock:
        return str((_link.get("owner") or {}).get("name") or "") if _link.get("enabled") else ""


def owner_email() -> str:
    with _lock:
        return str((_link.get("owner") or {}).get("email") or "") if _link.get("enabled") else ""


def rule() -> str:
    with _lock:
        return str(_link.get("rule") or "mine")


def assistant_name() -> str:
    with _lock:
        return str((_link.get("assistant") or {}).get("name") or "")


def work_choices() -> dict:
    with _lock:
        return dict(_link.get("choices") or {}) if _link.get("enabled") else {}


# ---- the outbox ----------------------------------------------------------------------

def _outbox_path() -> str:
    return os.path.join(STATE_DIR, "assistant-outbox.json")


def _load_outbox() -> list[dict]:
    try:
        with open(_outbox_path()) as fh:
            d = json.load(fh)
            return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def emit(kind: str, payload: dict, event_id: str | None = None) -> dict | None:
    """Queue one event for the assistant (only while the link is on)."""
    if not active():
        return None
    ev = {"id": event_id or f"nt-{secrets.token_hex(8)}", "kind": kind, "at": int(time.time()), **payload}
    with _lock:
        box = [x for x in _load_outbox() if x.get("id") != ev["id"]]
        box.append(ev)
        _write_json(_outbox_path(), box[-OUTBOX_MAX:])
    _wake.set()
    return ev


def flush() -> int:
    """Send what is queued, oldest first. Stops at the first failure (Work
    down) and tries again on the next round. → how many were sent."""
    client = _client()
    with _lock:
        box = _load_outbox()
    if not box or not client:
        return 0
    sent, keep = 0, []
    now = time.time()
    failed = False
    for ev in box:
        if failed:
            keep.append(ev)
            continue
        if now - ev.get("at", now) > OUTBOX_TTL_S:
            log.warning("dropping an old assistant event %s (%s)", ev.get("id"), ev.get("kind"))
            continue
        try:
            client.assistant_event(ev)
            sent += 1
        except uno_app.UnoAppError as exc:
            if exc.status in (400, 403, 404, 413, 422):
                log.warning("assistant event %s refused: %s", ev.get("kind"), exc.message)  # never retried
                continue
            failed = True
            keep.append(ev)
    with _lock:
        current = _load_outbox()
        done = {ev["id"] for ev in box} - {ev["id"] for ev in keep}
        _write_json(_outbox_path(), [x for x in current if x.get("id") not in done])
    return sent


def pending() -> list[dict]:
    with _lock:
        return _load_outbox()


# ---- call statuses → events ---------------------------------------------------------------

REASONS = [
    ("waiting_room", re.compile(r"(?i)nobody let|let you in|no one responded|waiting room|lobby")),
    ("denied", re.compile(r"(?i)denied|declined|removed|не впустил|отклонил")),
    ("sign_in_required", re.compile(r"(?i)sign in|signed-in|войдите|log in")),
    ("meeting_not_found", re.compile(r"(?i)check your meeting code|can't find this meeting|invalid|expired|"
                                     r"такого звонка нет|not found|has ended|meeting id")),
    ("captcha", re.compile(r"(?i)captcha|robot")),
    ("bot_offline", re.compile(r"(?i)never picked this call up|bot container|stopped unexpectedly")),
    ("turned_off", re.compile(r"(?i)^cancelled")),
]


def reason_of(status: str, detail: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    if status not in ("failed", "not_admitted", "cancelled"):
        return ""
    for code, rx in REASONS:
        if rx.search(detail or ""):
            return code
    return "waiting_room" if status == "not_admitted" else "error"


def status_text(x: dict, bot: str) -> str:
    """One plain sentence for the person (Inbox, the assistant's card)."""
    title = x.get("title") or "the call"
    status, why = x.get("status", ""), reason_of(x.get("status", ""), x.get("detail", ""), x.get("reason", ""))
    if status in ("queued", "joining"):
        return f"Joining {title} now"
    if status == "lobby":
        return f"{bot} is waiting to be let into {title}"
    if status == "recording":
        return f"{bot} is in {title}, taking notes"
    if status == "leaving":
        return f"{bot} is leaving {title}"
    if status == "done":
        return f"{bot} left {title} — notes are on the way"
    texts = {
        "waiting_room": f"Couldn't join {title}: waiting room — nobody let {bot} in",
        "denied": f"Couldn't join {title}: the host didn't let {bot} in",
        "sign_in_required": f"Couldn't join {title}: the call only lets signed-in people in",
        "meeting_not_found": f"Couldn't join {title}: the call link doesn't work (ended or wrong)",
        "captcha": f"Couldn't join {title}: the call asked for a captcha",
        "bot_offline": f"Couldn't join {title}: the notetaker on the computer isn't running",
        "moved": f"{title} moved — {bot} will come at the new time",
        "cancelled": f"{title} was cancelled — {bot} won't go",
        "declined": f"You declined {title} — {bot} won't go",
        "turned_off": f"{bot} won't go to {title}",
    }
    return texts.get(why, f"Couldn't join {title}: {x.get('detail') or 'something went wrong'}")


def _sent_path() -> str:
    return os.path.join(STATE_DIR, "assistant-sent.json")


def _load_sent() -> dict:
    try:
        with open(_sent_path()) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def sync_sessions(sessions: list[dict]) -> int:
    """A call.status event for every session whose status changed since the
    last one sent. → how many queued."""
    if not active():
        return 0
    sent = _load_sent()
    known = sent.get("sessions") or {}
    n = 0
    name = bot_name() or "The notetaker"
    for x in sessions:
        status = x.get("status", "")
        if status == "queued" or known.get(x["id"]) == status:
            continue
        reason = reason_of(status, x.get("detail", ""), x.get("reason", ""))
        emit("call.status", {
            "key": x.get("event_key", ""), "session": x["id"], "title": x.get("title", ""),
            "start": x.get("start", ""), "end": x.get("end", ""), "platform": x.get("platform", ""),
            "status": status, "reason": reason, "detail": x.get("detail", ""),
            "meeting_id": x.get("meeting_id", ""), "text": status_text(x, name),
        }, event_id=f"st-{x['id']}-{status}")
        known[x["id"]] = status
        n += 1
    if n:
        ids = {x["id"] for x in sessions}
        sent["sessions"] = {k: v for k, v in known.items() if k in ids}
        _write_json(_sent_path(), sent)
    return n


def sync_upcoming(calls: list[dict], force: bool = False) -> bool:
    """The calls coming up (with the bot's decision) when they changed."""
    if not active():
        return False
    slim = [{k: c.get(k) for k in ("key", "title", "start", "end", "platform", "platform_name", "join", "why",
                                   "choice")} | {"session": c.get("session")} for c in calls[:40]]
    digest = hashlib.sha1(json.dumps(slim, sort_keys=True).encode()).hexdigest()
    sent = _load_sent()
    if not force and sent.get("upcoming") == digest and time.time() - sent.get("upcoming_at", 0) < 6 * 3600:
        return False
    emit("calls.upcoming", {"calls": slim}, event_id=f"up-{digest[:16]}-{int(time.time()) // 60}")
    sent["upcoming"], sent["upcoming_at"] = digest, time.time()
    _write_json(_sent_path(), sent)
    return True


# ---- notes → the assistant ------------------------------------------------------------

SECTION_RE = re.compile(r"^#{1,4}\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.*)$")


def sections(notes: str) -> dict[str, list[str]]:
    """notes.md → {"summary": [...], "decisions": [...], "questions": [...]} (bullets)."""
    names = {"summary": r"summary|итоги|кратко|резюме", "decisions": r"decisions|решения",
             "questions": r"open questions|questions|вопросы"}
    out: dict[str, list[str]] = {k: [] for k in names}
    current = ""
    for line in notes.splitlines():
        h = SECTION_RE.match(line)
        if h:
            current = next((k for k, rx in names.items() if re.search(rf"(?i){rx}", h.group(1))), "")
            continue
        m = BULLET_RE.match(line)
        if current and m:
            text = re.sub(r"\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*$", "", m.group(1).replace("**", "")).strip()
            if text:
                out[current].append(text[:400])
    return {k: v[:12] for k, v in out.items()}


def meeting_notes_event(m: dict, notes: str, items: list[dict]) -> dict:
    call = m.get("call") or {}
    started = call.get("start") or m.get("created_at") or ""
    folder = f"~/Meetings/{m.get('folder', '')}".rstrip("/")
    s = sections(notes)
    duration = 0
    try:
        duration = int(float(m.get("duration") or m.get("duration_s") or 0))
    except (TypeError, ValueError):
        pass
    return {
        "meeting_id": m.get("id", ""), "key": (call.get("event_key") or ""), "session": call.get("session", ""),
        "title": m.get("title") or "Meeting", "start": started, "end": call.get("end", ""),
        "duration_s": duration, "platform": call.get("platform", ""),
        "attendees": [str(a) for a in (m.get("attendees") or [])][:40],
        "summary": s["summary"], "decisions": s["decisions"], "questions": s["questions"],
        "action_items": [{"id": it["id"], "owner": it.get("owner", ""), "text": it["text"],
                          "due": it.get("due", ""), "done": bool(it.get("done"))} for it in items][:30],
        "notes_path": f"{folder}/notes.md", "transcript_path": f"{folder}/transcript.md",
        "open": {"app": True, "path": f"/m/{m.get('id', '')}"},
    }


# ---- the loop ---------------------------------------------------------------------------

def tick() -> None:
    """One round: the link, Work's per-meeting choices, statuses, upcoming, outbox."""
    from . import bot, calendars  # late: they import this module
    was = active()
    refresh_link()
    now_on = active()
    if was != now_on:
        log.info("assistant link: %s (%s)", "on" if now_on else "off", bot_name())
        calendars.poke()
    if now_on:
        calendars.apply_work_choices(work_choices())
        sync_sessions(bot.sessions())
        state = _load_sent()
        if time.time() - state.get("upcoming_tick", 0) >= UPCOMING_EVERY_S:
            state["upcoming_tick"] = time.time()
            _write_json(_sent_path(), state)
            sync_upcoming(bot.overview()["upcoming"])
    flush()


def start() -> None:
    def loop():
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("assistant loop")
            _wake.wait(LOOP_S)
            _wake.clear()

    threading.Thread(target=loop, daemon=True, name="notetaker-assistant").start()


def status() -> dict:
    """For the app's own pages: "Sent to calls by Ada (Uno Work)"."""
    with _lock:
        d = dict(_link)
    return {"active": bool(d.get("enabled")), "supported": d.get("supported"), "assistant": assistant_name(),
            "bot_name": bot_name(), "rule": d.get("rule") or "", "error": d.get("error", ""),
            "pending": len(pending())}


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()
