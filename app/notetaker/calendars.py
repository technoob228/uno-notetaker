"""Calendars → upcoming calls the bot joins.

A calendar is connected by its secret iCal address — Google Calendar
("Secret address in iCal format"), Outlook / Microsoft 365 ("Publish a
calendar" → ICS), Yandex Calendar ("Export" → link), Zoho Calendar, iCloud
(public calendar). No OAuth app, no Google Cloud project: the link is
read-only and the person can reset it in their calendar at any time. The
link is a secret (anyone with it reads the calendar), so it is kept in the
state volume and shown masked.

Every few minutes the calendars are fetched, recurring events expanded for
the next days, and each event searched for a call link (Meet, Zoom, Teams,
Telemost, Zoho). Whether the bot goes is decided per occurrence:

  * a per-event choice the person made in the app wins ("join" / "skip");
  * auto-join "off" → no; cancelled or declined by the person → no;
  * auto-join "accepted" → only meetings the person accepted or organizes
    (or with no guests at all);
  * auto-join "all" → every call with a link;
  * "mine" (what an assistant uses, see assistant.py) → the person is the
    organizer or an attendee who hasn't declined, and somebody else is
    invited (a solo block with an auto-added Meet link is not a call).

A calendar can also come from Uno Work: when an assistant has "Join my
calls" on, the person's Google Calendar (connected once in Uno) arrives as
JSON through the App API — a virtual calendar "uno", nothing to paste. Google
gives each occurrence a stable id, so a meeting moved to another time keeps
its key: the bot follows it instead of going twice.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import STATE_DIR, USER_AGENT, load_settings, save_settings

log = logging.getLogger("notetaker.calendar")

REFRESH_S = 5 * 60
REFRESH_UNO_S = 3 * 60
UNO_CAL = "uno"
HORIZON = timedelta(days=7)
MAX_ICS = 15 * 1024 * 1024
MAX_CALENDARS = 10

# Call links, most specific first. Trailing punctuation and markup are cut off
# afterwards (descriptions are often HTML or "<https://…>").
LINK_PATTERNS = [
    ("meet", re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}(?:\?[^\s\"'<>]*)?", re.I)),
    ("zoom", re.compile(r"https://(?:[\w-]+\.)?zoom(?:gov)?\.(?:us|com)/(?:j|w|s|my|wc/join)/[^\s\"'<>]+", re.I)),
    ("teams", re.compile(r"https://teams\.(?:microsoft|live)\.com/(?:l/meetup-join|meet|v2/\?meetingjoin)[^\s\"'<>]+", re.I)),
    ("telemost", re.compile(r"https://telemost\.(?:360\.)?yandex\.(?:ru|com|com\.tr|kz|by|uz)/j/[^\s\"'<>]+", re.I)),
    ("zoho", re.compile(r"https://(?:meeting|meet|www)\.zoho\.(?:com|eu|in|com\.au|jp|com\.cn|sa|ca|uk)/"
                        r"(?:meeting/)?[^\s\"'<>]*(?:join|meeting|video)[^\s\"'<>]*", re.I)),
    ("jitsi", re.compile(r"https://meet\.jit\.si/[^\s\"'<>]+", re.I)),
]
PLATFORM_NAMES = {"meet": "Google Meet", "zoom": "Zoom", "teams": "Microsoft Teams", "telemost": "Yandex Telemost",
                  "zoho": "Zoho Meeting", "jitsi": "Jitsi Meet", "other": "Call"}

_lock = threading.RLock()
_cache: dict[str, dict] = {}   # calendar id → {"at", "events", "error", "name"}
_wake = threading.Event()
HTTP = httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True,
                    headers={"User-Agent": USER_AGENT})


# ---- links -------------------------------------------------------------------

def _clean_link(url: str) -> str:
    url = html.unescape(url)
    url = re.split(r"[\s<>\"'\\]", url)[0]
    return url.rstrip(".,;:!?)]}*_|")


def find_link(*texts: str) -> tuple[str, str]:
    """(platform, url) of the first call link in the texts, else ("", "")."""
    for text in texts:
        if not text:
            continue
        text = html.unescape(str(text))
        for platform, rx in LINK_PATTERNS:
            m = rx.search(text)
            if m:
                return platform, _clean_link(m.group(0))
    return "", ""


def platform_of(url: str) -> str:
    platform, _ = find_link(url)
    return platform or "other"


# ---- calendar list (state) ----------------------------------------------------

def _overrides_path() -> str:
    return os.path.join(STATE_DIR, "calendar.json")


def _load_overrides() -> dict:
    try:
        with open(_overrides_path()) as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_overrides(d: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _overrides_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, _overrides_path())


def _mask(url: str) -> str:
    m = re.match(r"(?:https?|webcal)://([^/]+)", url)
    return f"{m.group(1) if m else 'calendar'} · …{url[-6:]}"


def _ical_calendars() -> list[dict]:
    return [c for c in load_settings().calendars if isinstance(c, dict) and c.get("url")]


def calendars() -> list[dict]:
    out = _ical_calendars()
    from . import assistant  # late: assistant imports nothing of ours at import time
    if assistant.active():
        out.append({"id": UNO_CAL, "kind": "uno", "url": "", "name": "Google Calendar (through Uno)"})
    return out


def public_list() -> list[dict]:
    out = []
    for c in calendars():
        info = _cache.get(c["id"]) or {}
        out.append({"id": c["id"], "name": c.get("name") or info.get("name") or "",
                    "where": "Uno Work · Google" if c.get("kind") == "uno" else _mask(c["url"]),
                    "kind": c.get("kind", "ical"),
                    "fetched_at": info.get("at"), "error": info.get("error", ""),
                    "events": len(info.get("events") or [])})
    return out


def add(url: str) -> dict:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not re.match(r"^https?://[^\s]+$", url):
        raise ValueError("Paste the calendar's iCal address — it starts with https:// or webcal://")
    cals = _ical_calendars()
    if any(c["url"] == url for c in cals):
        raise ValueError("This calendar is already connected")
    if len(cals) >= MAX_CALENDARS:
        raise ValueError(f"At most {MAX_CALENDARS} calendars")
    cal = {"id": secrets.token_hex(4), "url": url, "name": ""}
    events, name = _fetch(cal)  # raises ValueError with a readable reason
    cal["name"] = name
    with _lock:
        _cache[cal["id"]] = {"at": int(time.time()), "events": events, "error": "", "name": name}
    save_settings({"calendars": cals + [cal]}, internal=True)
    _wake.set()
    return {"id": cal["id"], "name": name, "events": len(events)}


def remove(cid: str) -> bool:
    cals = _ical_calendars()
    left = [c for c in cals if c["id"] != cid]
    if len(left) == len(cals):
        return False
    save_settings({"calendars": left}, internal=True)
    with _lock:
        _cache.pop(cid, None)
    return True


# ---- fetching and parsing -----------------------------------------------------

def _fetch(cal: dict) -> tuple[list[dict], str]:
    if cal.get("kind") == "uno":
        return _fetch_uno()
    try:
        with HTTP.stream("GET", cal["url"]) as resp:
            if resp.status_code in (401, 403, 404):
                raise ValueError(f"The calendar refused the link (HTTP {resp.status_code}) — "
                                 "copy the secret iCal address again")
            if resp.status_code >= 400:
                raise ValueError(f"The calendar answered HTTP {resp.status_code}")
            data = b""
            for piece in resp.iter_bytes():
                data += piece
                if len(data) > MAX_ICS:
                    raise ValueError("The calendar is too large (over 15 MB)")
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not reach the calendar: {exc}") from None
    if b"BEGIN:VCALENDAR" not in data[:4096].upper():
        raise ValueError("This link is not an iCal calendar (.ics). In Google Calendar: Settings → "
                         "your calendar → “Secret address in iCal format”.")
    return parse(data)


def _as_utc(value, tz_offset_min: int) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: the person's own zone
            off = tz_offset_min if -900 <= tz_offset_min <= 900 else 0
            value = value.replace(tzinfo=timezone(timedelta(minutes=off)))
        return value.astimezone(timezone.utc)
    return None  # all-day (date) events have no call to join


def _email(value) -> str:
    return re.sub(r"(?i)^mailto:", "", str(value or "")).strip().lower()


def parse(data: bytes, now: datetime | None = None) -> tuple[list[dict], str]:
    """ICS → (occurrences in [now - 12 h, now + HORIZON], calendar name)."""
    import icalendar
    import recurring_ical_events
    import x_wr_timezone

    try:
        cal = icalendar.Calendar.from_ical(data)
        cal = x_wr_timezone.to_standard(cal)
    except Exception as exc:  # noqa: BLE001 — any parse error is "not a calendar"
        raise ValueError(f"Could not read the calendar: {exc}") from None
    name = str(cal.get("X-WR-CALNAME") or "")
    now = now or datetime.now(timezone.utc)
    s = load_settings()
    out = []
    try:
        occurrences = recurring_ical_events.of(cal).between(now - timedelta(hours=12), now + HORIZON)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not read the calendar's events: {exc}") from None
    for ev in occurrences:
        start = _as_utc(ev.decoded("DTSTART", None), s.tz_offset_min)
        if not start:
            continue
        end = _as_utc(ev.decoded("DTEND", None), s.tz_offset_min) if ev.get("DTEND") else None
        if not end:
            dur = ev.decoded("DURATION", None)
            end = start + (dur if isinstance(dur, timedelta) else timedelta(hours=1))
        conference = ev.get("CONFERENCE")
        platform, link = find_link(str(ev.get("X-GOOGLE-CONFERENCE") or ""),
                                   " ".join(str(x) for x in (conference if isinstance(conference, list)
                                                             else [conference or ""])),
                                   str(ev.get("URL") or ""), str(ev.get("LOCATION") or ""),
                                   str(ev.get("DESCRIPTION") or ""), str(ev.get("X-ALT-DESC") or ""))
        attendees = ev.get("ATTENDEE") or []
        if not isinstance(attendees, list):
            attendees = [attendees]
        people = []
        for a in attendees:
            params = getattr(a, "params", {}) or {}
            people.append({"name": str(params.get("CN") or ""), "email": _email(a),
                           "status": str(params.get("PARTSTAT") or "").upper()})
        org = ev.get("ORGANIZER")
        uid = str(ev.get("UID") or "")
        key = hashlib.sha1(f"{uid}|{start.isoformat()}".encode()).hexdigest()[:12]
        out.append({
            "key": key, "uid": uid, "title": str(ev.get("SUMMARY") or "")[:200],
            "start": start.isoformat(), "end": end.isoformat(),
            "status": str(ev.get("STATUS") or "").upper(),
            "platform": platform, "url": link,
            "organizer": _email(org), "organizer_name": str(getattr(org, "params", {}).get("CN") or "") if org else "",
            "attendees": people[:100],
        })
    out.sort(key=lambda e: e["start"])
    return out, name


# ---- the person's Google Calendar through Uno Work ---------------------------------

RESPONSES = {"accepted": "ACCEPTED", "declined": "DECLINED", "tentative": "TENTATIVE",
             "needsaction": "NEEDS-ACTION", "organizer": "ACCEPTED"}


def _utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def from_feed(feed: dict) -> list[dict]:
    """Uno's calendar_feed (Google, JSON) → the same occurrences parse() makes.
    The key is Google's occurrence id: stable when the meeting moves."""
    out = []
    for ev in feed.get("events") or []:
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        key = hashlib.sha1(f"g|{gid}".encode()).hexdigest()[:12]
        start, end = _utc(ev.get("start") or ""), _utc(ev.get("end") or "")
        status = str(ev.get("status") or "").upper()
        if status != "CANCELLED" and not start:
            continue
        if start and not end:
            end = start + timedelta(hours=1)
        platform, link = find_link(str(ev.get("conference_url") or ""), str(ev.get("location") or ""),
                                   str(ev.get("description") or ""))
        people = []
        for a in ev.get("attendees") or []:
            people.append({"name": str(a.get("name") or ""), "email": _email(a.get("email")),
                           "status": RESPONSES.get(str(a.get("response") or "").lower(), ""),
                           "self": bool(a.get("self")), "resource": bool(a.get("resource"))})
        org = ev.get("organizer") or {}
        out.append({
            "key": key, "uid": str(ev.get("ical_uid") or gid), "gid": gid,
            "title": str(ev.get("title") or "")[:200],
            "start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
            "status": status, "platform": platform, "url": link,
            "organizer": _email(org.get("email")), "organizer_name": str(org.get("name") or ""),
            "organizer_self": bool(org.get("self")) or ev.get("self_response") == "organizer",
            "attendees": people[:100],
        })
    out.sort(key=lambda e: e["start"] or "9999")
    return out


def _fetch_uno() -> tuple[list[dict], str]:
    from . import uno_app
    from .config import app_api_config
    cfg = app_api_config()
    if not cfg:
        raise ValueError("No Uno Work on this computer")
    now = datetime.now(timezone.utc)
    client = uno_app.Client(url=cfg["url"], token=cfg["token"], timeout=45.0)
    try:
        feed = client.calendar_events((now - timedelta(hours=12)).isoformat(), (now + HORIZON).isoformat())
    except uno_app.UnoAppError as exc:
        if exc.code in ("google_not_connected", "google_reconnect"):
            raise ValueError("Google Calendar needs signing in again in Uno Work") from None
        raise ValueError(f"Uno Work: {exc.message}") from None
    account = str(feed.get("account") or "")
    events = from_feed(feed)
    with _lock:
        info = _cache.get(UNO_CAL) or {}
        info["account"] = account
        _cache[UNO_CAL] = info
    return events, account or "Google Calendar"


# ---- the same call once ----------------------------------------------------------------

def canonical(url: str) -> str:
    """One string per call, whatever form the link came in (Meet with
    ?authuser=, Zoom's /j/ vs /wc/join/ and its subdomains)."""
    u = (url or "").strip()
    m = re.search(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", u, re.I)
    if m:
        return "meet:" + m.group(1).lower()
    m = re.search(r"zoom(?:gov)?\.(?:us|com)/(?:j|w|s|wc/join|wc)/(\d{9,12})", u, re.I)
    if m:
        return "zoom:" + m.group(1)
    m = re.search(r"telemost\.(?:360\.)?yandex\.[a-z.]+/j/(\d+)", u, re.I)
    if m:
        return "telemost:" + m.group(1)
    return re.split(r"[?#]", u)[0].rstrip("/").lower()


def refresh(force: bool = False) -> None:
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        every = REFRESH_UNO_S if cal.get("kind") == "uno" else REFRESH_S
        if not force and time.time() - (info.get("tried") or 0) < every:
            continue
        try:
            events, name = _fetch(cal)
            info = {"at": int(time.time()), "tried": time.time(), "events": events, "error": "", "name": name,
                    "account": (_cache.get(cal["id"]) or {}).get("account", "")}
        except ValueError as exc:
            log.warning("calendar %s: %s", cal["id"], exc)
            info = {**info, "tried": time.time(), "error": str(exc)}
        with _lock:
            _cache[cal["id"]] = info


# ---- who goes ------------------------------------------------------------------

def my_emails() -> set[str]:
    """The person's addresses: Settings, plus each calendar's name when it is
    an address (Google names the primary calendar by its owner's email)."""
    s = load_settings()
    out = {e.strip().lower() for e in re.split(r"[,\s;]+", s.my_emails) if "@" in e}
    for info in _cache.values():
        if "@" in (info.get("name") or ""):
            out.add(info["name"].strip().lower())
        if "@" in (info.get("account") or ""):
            out.add(info["account"].strip().lower())
    from . import assistant
    if assistant.owner_email():
        out.add(assistant.owner_email())
    return out


def decide(ev: dict, overrides: dict | None = None, mine: set[str] | None = None) -> tuple[bool, str]:
    """(the bot joins?, why) for one occurrence."""
    overrides = _load_overrides() if overrides is None else overrides
    mine = my_emails() if mine is None else mine
    choice = (overrides.get("events") or {}).get(ev["key"])
    if ev.get("status") == "CANCELLED":
        return False, "cancelled"
    if not ev.get("url"):
        return False, "no call link"
    if choice == "skip":
        return False, "you turned it off"
    if choice == "join":
        return True, "you turned it on"
    mode = auto_join_mode()
    if mode == "off":
        return False, "auto-join is off"
    me = [a for a in ev.get("attendees") or [] if a.get("self") or a["email"] in mine]
    if any(a["status"] == "DECLINED" for a in me):
        return False, "you declined"
    if mode == "mine":
        organizer = bool(ev.get("organizer_self")) or ev.get("organizer") in mine
        if not (organizer or me):
            return False, "you're not in this meeting"
        others = [a for a in ev.get("attendees") or []
                  if not (a.get("self") or a["email"] in mine or a.get("resource"))]
        if not others:
            return False, "nobody else is invited"
        return True, "auto"
    if mode == "accepted":
        organizer = ev.get("organizer") in mine
        accepted = any(a["status"] == "ACCEPTED" for a in me)
        if not (organizer or accepted or not ev.get("attendees")):
            return False, "you haven't accepted"
    return True, "auto"


def auto_join_mode() -> str:
    """While an assistant sends the bot, its rule wins over the app's setting."""
    from . import assistant
    return assistant.rule() if assistant.active() else load_settings().bot_auto_join


def apply_work_choices(choices: dict) -> int:
    """Per-meeting switches pressed in Uno Work ("don't go to this one"): a
    choice newer than the last one applied wins. → how many applied."""
    if not choices:
        return 0
    n = 0
    with _lock:
        d = _load_overrides()
        applied = d.setdefault("work_applied", {})
        for key, v in choices.items():
            at = int(v.get("at") or 0)
            if at <= int(applied.get(key) or 0):
                continue
            applied[key] = at
            events = d.setdefault("events", {})
            if v.get("choice") in ("join", "skip"):
                events[key] = v["choice"]
            else:
                events.pop(key, None)
            n += 1
        if n:
            if len(applied) > 500:
                d["work_applied"] = dict(sorted(applied.items(), key=lambda kv: kv[1])[-300:])
            _save_overrides(d)
    if n:
        _wake.set()
    return n


def set_choice(key: str, choice: str | None) -> None:
    with _lock:
        d = _load_overrides()
        events = d.setdefault("events", {})
        if choice in ("join", "skip"):
            events[key] = choice
        else:
            events.pop(key, None)
        # forget choices for events long gone (keys are per occurrence)
        known = {e["key"] for info in _cache.values() for e in info.get("events") or []}
        if len(events) > 500:
            d["events"] = {k: v for k, v in events.items() if k in known}
        _save_overrides(d)


def upcoming(include_past_hours: float = 2) -> list[dict]:
    """Occurrences from every calendar, de-duplicated (the same meeting in two
    calendars has the same UID and start), each with the bot's decision."""
    now = datetime.now(timezone.utc)
    overrides = _load_overrides()
    mine = my_emails()
    seen: dict[str, dict] = {}
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        for ev in info.get("events") or []:
            if ev.get("end") and datetime.fromisoformat(ev["end"]) < now - timedelta(hours=include_past_hours):
                continue
            if ev["key"] in seen:
                continue
            join, why = decide(ev, overrides, mine)
            choice = (overrides.get("events") or {}).get(ev["key"], "")
            seen[ev["key"]] = {**ev, "calendar": cal["id"], "join": join, "why": why, "choice": choice,
                               "platform_name": PLATFORM_NAMES.get(ev["platform"], "")}
    return sorted(seen.values(), key=lambda e: e["start"])


def loaded() -> bool:
    """Every calendar was read at least once without an error (so a meeting
    missing from it is really gone, not just not fetched yet)."""
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        if not info.get("at") or info.get("error"):
            return False
    return True


def find_event(key: str) -> dict | None:
    return next((e for e in upcoming(include_past_hours=24) if e["key"] == key), None)


# ---- the loop: send the bot when a call starts --------------------------------

def start(on_due) -> None:
    """Background thread: refresh calendars, call on_due(event) once when a
    call the bot should join is about to start."""
    def loop():
        while True:
            try:
                from . import bot  # late: bot imports this module
                if calendars():
                    refresh()
                bot.reconcile()
                if calendars():
                    s = load_settings()
                    now = datetime.now(timezone.utc)
                    early = timedelta(minutes=max(0, min(15, s.bot_join_early_min)))
                    for ev in upcoming(include_past_hours=0):
                        if not ev["join"]:
                            continue
                        start_at = datetime.fromisoformat(ev["start"])
                        end_at = datetime.fromisoformat(ev["end"])
                        # Late too: a restart in the middle of a call still sends the bot,
                        # unless less than 5 minutes are left.
                        if start_at - early <= now < end_at - timedelta(minutes=5):
                            on_due(ev)
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("calendar loop")
            _wake.wait(30)
            _wake.clear()

    threading.Thread(target=loop, daemon=True, name="notetaker-calendar").start()


def poke() -> None:
    _wake.set()
