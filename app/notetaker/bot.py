"""The bot that joins calls — this app's side.

The bot itself runs in its own container (../bot: Chromium + PulseAudio,
one browser per call). It holds no state and knows nothing but this app's
address and a token: it long-polls /api/bot/worker/work for calls to join,
reports what happens (joining → waiting in the lobby → recording → done),
streams the call's audio here in pieces and says when it left. This side
keeps the sessions (state/bot.json), creates the meeting once the bot is in
the call, and from then on the meeting is like any other recording: the
pipeline transcribes it, tells voices apart and writes the notes.

The token is HMAC(cookie secret) — written where the bot container reads it
(a volume only the two containers share). It allows /api/bot/worker/* and
nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from . import calendars, pipeline, store
from .config import STATE_DIR, cookie_secret, load_settings

log = logging.getLogger("notetaker.bot")
KEY_FILE = os.environ.get("BOT_KEY_FILE", "/bot-key/token")

ACTIVE = ("queued", "joining", "lobby", "recording", "leaving")
FINAL = ("done", "failed", "not_admitted", "cancelled")
WORKER_GONE_S = 90        # no poll for this long: the bot container isn't running
SESSION_SILENT_S = 180    # a claimed session with no report for this long: the bot died with it
KEEP_SESSIONS = 200

_lock = threading.RLock()
_changed = threading.Condition(_lock)
_worker: dict = {"seen": 0.0, "version": "", "capacity": 0, "running": 0}


# ---- token -----------------------------------------------------------------------

def token() -> str:
    return hmac.new(cookie_secret(), b"notetaker-bot-v1", hashlib.sha256).hexdigest()


def token_ok(header: str) -> bool:
    got = header[7:] if header.lower().startswith("bearer ") else ""
    return bool(got) and hmac.compare_digest(got, token())


def write_token() -> None:
    """Where the bot container reads it. Silently skipped without the volume
    (no bot container on this computer)."""
    d = os.path.dirname(KEY_FILE)
    if not os.path.isdir(d):
        return
    try:
        tmp = KEY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(token())
        os.chmod(tmp, 0o644)  # the bot runs as another user; only the two containers see this volume
        os.replace(tmp, KEY_FILE)
    except OSError as exc:
        log.warning("could not write the bot token: %s", exc)


# ---- sessions (state/bot.json) -------------------------------------------------

def _path() -> str:
    return os.path.join(STATE_DIR, "bot.json")


def _load() -> list[dict]:
    try:
        with open(_path()) as fh:
            d = json.load(fh)
            return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(sessions: list[dict]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sessions[-KEEP_SESSIONS:], fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def sessions() -> list[dict]:
    with _lock:
        return _load()


def get(sid: str) -> dict | None:
    return next((x for x in sessions() if x["id"] == sid), None)


def _update(sid: str, **changes) -> dict | None:
    with _lock:
        all_ = _load()
        for x in all_:
            if x["id"] == sid:
                x.update(changes)
                x["updated_at"] = int(time.time())
                _save(all_)
                _changed.notify_all()
                return x
    return None


def create(url: str, title: str = "", event: dict | None = None) -> dict:
    """A call for the bot to join (now). Refuses a second bot in the same call."""
    platform = calendars.platform_of(url)
    with _lock:
        all_ = _load()
        for x in all_:
            if x["status"] in ACTIVE and x["url"] == url:
                return x
            if event and x.get("event_key") == event["key"]:
                return x  # once per occurrence, whatever happened (the person can send it again by hand)
        now = int(time.time())
        x = {
            "id": secrets.token_hex(5), "url": url, "platform": platform,
            "platform_name": calendars.PLATFORM_NAMES.get(platform, "Call"),
            "title": (title or (event or {}).get("title") or "").strip()[:120],
            "event_key": (event or {}).get("key", ""), "start": (event or {}).get("start", ""),
            "end": (event or {}).get("end", ""),
            "attendees": [a["name"] or a["email"].split("@")[0] for a in (event or {}).get("attendees") or []
                          if a.get("name") or a.get("email")][:40],
            "status": "queued", "detail": "Waiting for the bot…", "meeting_id": "",
            "created_at": now, "updated_at": now, "stop": False, "log": [],
        }
        all_.append(x)
        _save(all_)
        _changed.notify_all()
    log.info("bot session %s → %s %s", x["id"], platform, url)
    return x


def from_calendar(ev: dict) -> None:
    create(ev["url"], ev.get("title", ""), ev)


def stop(sid: str) -> dict | None:
    x = get(sid)
    if not x:
        return None
    if x["status"] == "queued":
        return _update(sid, status="cancelled", detail="Cancelled before the bot joined")
    if x["status"] in ACTIVE:
        return _update(sid, stop=True, detail="Leaving the call…")
    return x


def worker_status() -> dict:
    seen = _worker["seen"]
    return {"online": time.time() - seen < WORKER_GONE_S, "seen": int(seen) or None,
            "version": _worker["version"], "capacity": _worker["capacity"], "running": _worker["running"]}


# ---- what the bot container calls ----------------------------------------------

def _job(x: dict) -> dict:
    s = load_settings()
    end = x.get("end") or ""
    return {"id": x["id"], "url": x["url"], "platform": x["platform"], "title": x["title"],
            "name": s.bot_name.strip()[:60] or "Uno Notetaker",
            "chat_message": (f"Hi! I'm {s.bot_name or 'Uno Notetaker'}: I record and transcribe this call "
                             "to take notes for the person who invited me. Remove me from the call to stop "
                             "the recording.") if s.bot_chat_message else "",
            "lobby_wait_s": max(60, min(60 * 60, s.bot_lobby_wait_min * 60)),
            "end": end,
            # how long at most: the scheduled end + 1 h, a call without a schedule 4 h
            "max_s": int(max(30 * 60, (datetime.fromisoformat(end) - datetime.now(timezone.utc)).total_seconds()
                              + 3600)) if end else 4 * 3600}


def claim(capacity: int, running: list[str], version: str = "") -> dict:
    """One poll of the bot container: calls to start (up to its free slots)
    and the ids of running calls it should leave."""
    now = time.time()
    _worker.update(seen=now, version=version[:40], capacity=capacity, running=len(running))
    with _lock:
        all_ = _load()
        start, stop_ids = [], []
        free = max(0, capacity - len(running))
        for x in all_:
            if x["status"] == "queued" and free > 0:
                x.update(status="joining", detail="Opening the call…", updated_at=int(now), claimed_at=int(now))
                start.append(_job(x))
                free -= 1
            elif x["status"] in ACTIVE and x["status"] != "queued" and x["id"] not in running \
                    and now - x.get("claimed_at", now) > 30:
                # The container restarted with this call: it is gone.
                _fail_lost(x)
            if x.get("stop") and x["id"] in running:
                stop_ids.append(x["id"])
        if start:
            _save(all_)
        elif any(x.get("_dirty") for x in all_):
            for x in all_:
                x.pop("_dirty", None)
            _save(all_)
        return {"start": start, "stop": stop_ids}


def wait_for_work(timeout: float) -> None:
    with _lock:
        if any(x["status"] == "queued" or x.get("stop") for x in _load() if x["status"] in ACTIVE):
            return
        _changed.wait(timeout)


def _fail_lost(x: dict) -> None:
    """The bot went away mid-call (container restart, crash): keep what was
    recorded and make notes from it."""
    x["_dirty"] = True
    mid = x.get("meeting_id")
    if x["status"] == "joining" and not mid and not x.get("requeued"):
        # Handed out just as the bot container stopped: give it to the next one.
        x.update(status="queued", detail="Waiting for the bot…", requeued=True, updated_at=int(time.time()))
        return
    if mid and (store.get(mid) or {}).get("status") == "recording":
        x.update(status="done", detail="The bot stopped unexpectedly — notes from what was recorded")
        _finish_meeting(mid)
    else:
        x.update(status="failed", detail="The bot stopped unexpectedly")
    x["updated_at"] = int(time.time())


STATUS_TEXT = {
    "joining": "Opening the call…",
    "lobby": "Waiting for the host to let the bot in…",
    "recording": "In the call, recording",
    "leaving": "Leaving the call…",
}


def report(sid: str, status: str, detail: str = "") -> dict:
    """The bot says where it is. → {"stop": bool, "meeting_id": str}."""
    x = get(sid)
    if not x:
        return {"stop": True, "meeting_id": "", "error": "no such session"}
    if x["status"] in FINAL:
        return {"stop": True, "meeting_id": x.get("meeting_id", "")}
    detail = (detail or STATUS_TEXT.get(status, "")).strip()[:300]
    changes: dict = {"detail": detail}
    if status in ("joining", "lobby", "recording", "leaving"):
        changes["status"] = status
    elif status in ("failed", "not_admitted"):
        changes["status"] = status
    if status == "recording" and not x.get("meeting_id"):
        changes["meeting_id"] = _new_meeting(x)
        changes["joined_at"] = int(time.time())
    log_ = (x.get("log") or [])[-30:]
    if not log_ or log_[-1].get("status") != status or log_[-1].get("detail") != detail:
        log_.append({"at": int(time.time()), "status": status, "detail": detail})
    changes["log"] = log_
    x = _update(sid, **changes) or x
    return {"stop": bool(x.get("stop")), "meeting_id": x.get("meeting_id", "")}


def _new_meeting(x: dict) -> str:
    s = load_settings()
    tz = s.tz_offset_min if -900 <= s.tz_offset_min <= 900 else None
    m = store.create(x.get("title") or "", "bot", s.template, s.language, tz)
    store.update(m["id"], call={"platform": x["platform"], "url": x["url"], "session": x["id"],
                                "start": x.get("start", ""), "end": x.get("end", "")},
                 attendees=x.get("attendees") or [], progress="The bot is in the call, recording…")
    return m["id"]


def audio(sid: str, data: bytes, offset: int, ext: str = "ogg") -> int:
    x = get(sid)
    if not x or not x.get("meeting_id"):
        raise ValueError("not recording")
    m = store.get(x["meeting_id"])
    if not m or m["status"] != "recording":
        raise ValueError("This meeting is no longer recording")
    return store.append_track(x["meeting_id"], "bot", ext, data, offset)


def _shot_path(sid: str) -> str:
    return os.path.join(STATE_DIR, "bot-shots", f"{sid}.png")


def save_screenshot(sid: str, png: bytes, why: str) -> None:
    """What the bot saw when it could not get in — for the person (and for
    fixing platforms/*.py when a service changes its page)."""
    if not get(sid) or not png.startswith(b"\x89PNG"):
        raise ValueError("not a screenshot of a known call")
    os.makedirs(os.path.dirname(_shot_path(sid)), exist_ok=True)
    with open(_shot_path(sid), "wb") as fh:
        fh.write(png)
    _update(sid, screenshot=why[:40] or "seen")
    # keep the last 30
    d = os.path.dirname(_shot_path(sid))
    shots = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
    for old in shots[:-30]:
        os.remove(old)


def screenshot_path(sid: str) -> str | None:
    if not re.match(r"^[a-f0-9]{10}$", sid or ""):
        return None
    p = _shot_path(sid)
    return p if os.path.exists(p) else None


def finish(sid: str, outcome: str, detail: str = "") -> dict:
    """The bot left. outcome: done | failed | not_admitted."""
    x = get(sid)
    if not x:
        return {}
    if outcome not in ("done", "failed", "not_admitted"):
        outcome = "done"
    mid = x.get("meeting_id")
    if mid and (store.get(mid) or {}).get("status") == "recording":
        recorded = sum(t.get("bytes", 0) for t in ((store.get(mid) or {}).get("tracks") or {}).values())
        if recorded:
            outcome = "done"
            _finish_meeting(mid)
        else:
            store.update(mid, status="error", error="The bot was in the call but recorded nothing.", progress="")
            outcome = "failed"
    defaults = {"done": "Left the call — writing notes", "failed": "Could not join",
                "not_admitted": "Nobody let the bot in"}
    return _update(sid, status=outcome, detail=(detail or defaults[outcome])[:300], stop=False,
                   left_at=int(time.time())) or {}


def _finish_meeting(mid: str) -> None:
    store.update(mid, status="queued", progress="Waiting in line…")
    pipeline.enqueue(mid, "full")


# ---- watchdog ---------------------------------------------------------------------

def start_watchdog() -> None:
    def loop():
        while True:
            time.sleep(30)
            try:
                _sweep()
            except Exception:  # noqa: BLE001
                log.exception("bot watchdog")

    threading.Thread(target=loop, daemon=True, name="notetaker-bot-watchdog").start()


def _sweep() -> None:
    now = time.time()
    with _lock:
        all_ = _load()
        dirty = False
        for x in all_:
            if x["status"] == "queued" and now - x["created_at"] > 30 * 60:
                x.update(status="failed", detail="The bot never picked this call up — is the bot container running?",
                         updated_at=int(now))
                dirty = True
            elif x["status"] in ACTIVE and x["status"] != "queued" and now - x["updated_at"] > SESSION_SILENT_S:
                _fail_lost(x)
                dirty = True
        if dirty:
            for x in all_:
                x.pop("_dirty", None)
            _save(all_)
            _changed.notify_all()


def overview() -> dict:
    """For the Calls page: the bot, running and recent sessions, upcoming calls."""
    all_ = sessions()
    by_event = {x["event_key"]: x for x in all_ if x.get("event_key")}
    upcoming = []
    for ev in calendars.upcoming():
        x = by_event.get(ev["key"])
        upcoming.append({k: ev[k] for k in ("key", "title", "start", "end", "platform", "platform_name", "url",
                                             "join", "why", "choice", "status")}
                        | {"attendees": len(ev.get("attendees") or []),
                           "session": {k: x.get(k) for k in ("id", "status", "detail", "meeting_id")} if x else None})
    active = [x for x in all_ if x["status"] in ACTIVE]
    recent = [x for x in reversed(all_) if x["status"] in FINAL][:15]
    strip = ("log",)
    return {"worker": worker_status(), "calendars": calendars.public_list(),
            "active": [{k: v for k, v in x.items() if k not in strip} for x in active],
            "recent": [{k: v for k, v in x.items() if k not in strip} for x in recent],
            "upcoming": upcoming}


def due_from_calendar(ev: dict) -> None:
    try:
        from_calendar(ev)
    except Exception:  # noqa: BLE001
        log.exception("could not send the bot to %s", ev.get("title"))

