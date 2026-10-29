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

Calendars from the person's Uno account (0.7): Uno Work lists them on the App
API (GET /v1/calendars, manifest "calendar": true) — Google Calendar (the
"Gmail & Calendar" connector) and Outlook & Teams (the "microsoft" connector).
Each connected one is a virtual calendar — "uno" (Google) and "uno-microsoft" —
read as JSON from /v1/calendar/events?provider=…, nothing to paste and no
assistant needed. The setting "Use calendars from my Uno account" (on by
default) turns them off. An older Uno Work (404 on /v1/calendars) works as in
0.5: the Google calendar "uno" only while an assistant has "Join my calls" on.
Google and Microsoft give each occurrence a stable id, so a meeting moved to
another time keeps its key: the bot follows it instead of going twice.

One call — one bot: the same meeting seen in two calendars (Google through Uno
and an iCal link of the same calendar; a Teams invitation in both Google and
Outlook) is one row in upcoming() — same canonical call link or same UID, at
the same minute — and bot.create() never sends a second bot to the same link.
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
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from .config import STATE_DIR, USER_AGENT, load_settings, save_settings

log = logging.getLogger("notetaker.calendar")

REFRESH_S = 5 * 60
REFRESH_UNO_S = 3 * 60
REFRESH_UNO_LIST_S = 3 * 60
UNO_LIST_FORCE_MIN_S = 3          # the UI re-checks every few seconds after "Connect"
UNO_CAL = "uno"
UNO_MS_CAL = "uno-microsoft"
# provider (Uno connector) → (virtual calendar id, short name, the name shown in the app)
UNO_PROVIDERS = {
    "gmail": (UNO_CAL, "Google Calendar", "Google Calendar · from your Uno account"),
    "microsoft": (UNO_MS_CAL, "Outlook & Teams", "Outlook & Teams · from your Uno account"),
}
KEY_PREFIX = {"gmail": "g", "microsoft": "m"}   # occurrence keys never collide across providers
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
# What Uno Work said about the calendars of the account (GET /v1/calendars).
# supported: None = not asked yet, False = no Uno Work / an older one (404).
_uno: dict = {"checked": 0.0, "supported": None, "list": [], "error": ""}
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


# ---- calendars of the Uno account (Uno Work App API) --------------------------------

def _app_client(timeout: float = 15.0):
    from . import uno_app
    from .config import app_api_config
    cfg = app_api_config()
    return uno_app.Client(url=cfg["url"], token=cfg["token"], timeout=timeout) if cfg else None


def _normalize_uno_list(got) -> list[dict]:
    rows = (got or {}).get("calendars") if isinstance(got, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("provider") not in UNO_PROVIDERS:
            continue
        out.append({"provider": r["provider"], "name": str(r.get("name") or UNO_PROVIDERS[r["provider"]][1])[:60],
                    "available": bool(r.get("available")), "connected": bool(r.get("connected")),
                    "account": str(r.get("account") or "").strip()[:200],
                    "needs_reconnect": bool(r.get("needsReconnect"))})
    return out


def refresh_uno_list(force: bool = False) -> dict:
    """Ask Uno Work which calendars the account has (every few minutes; with
    force — at most every few seconds). Keeps the last answer while Work is
    unreachable for a moment."""
    from . import uno_app
    with _lock:
        age = time.time() - _uno["checked"]
        if age < (UNO_LIST_FORCE_MIN_S if force else REFRESH_UNO_LIST_S):
            return dict(_uno)
        _uno["checked"] = time.time()
    client = _app_client()
    if not client:
        with _lock:
            _uno.update(supported=False, list=[], error="no Uno Work on this computer")
        return dict(_uno)
    try:
        got = client.calendars()
    except uno_app.UnoAppError as exc:
        with _lock:
            if exc.status in (403, 404) or exc.code in ("not_found", "calendar_not_allowed", "calls_not_allowed"):
                if _uno["supported"] is not False:
                    log.info("Uno Work has no calendars list (%s) — calendars from Uno as in 0.5", exc.code)
                _uno.update(supported=False, list=[], error=exc.message)
            else:
                _uno.update(error=exc.message)  # Work restarting: keep what it said last
        return dict(_uno)
    rows = _normalize_uno_list(got)
    with _lock:
        if [r["provider"] for r in rows if r["connected"]] != \
                [r["provider"] for r in _uno["list"] if r["connected"]]:
            _wake.set()
        _uno.update(supported=True, list=rows, error="")
    return dict(_uno)


def uno_enabled() -> bool:
    return bool(load_settings().uno_calendars)


def _uno_calendars() -> list[dict]:
    from . import assistant  # late: assistant imports nothing of ours at import time
    with _lock:
        supported, rows = _uno["supported"], list(_uno["list"])
    if supported:
        # The assistant's "Join my calls" (switched on in Uno Work) uses them whatever this setting says.
        if not (uno_enabled() or assistant.active()):
            return []
        out = []
        for r in rows:
            if r["available"] and r["connected"]:
                cid, _, label = UNO_PROVIDERS[r["provider"]]
                out.append({"id": cid, "kind": "uno", "provider": r["provider"], "url": "", "name": label,
                            "account": r["account"], "needs_reconnect": r["needs_reconnect"]})
        return out
    if assistant.active():  # an older Uno Work: Google through Uno, only with an assistant (0.5)
        return [{"id": UNO_CAL, "kind": "uno", "provider": "gmail", "url": "", "legacy": True,
                 "name": "Google Calendar (through Uno)"}]
    return []


def calendars() -> list[dict]:
    """Uno's first: when the same meeting is also in an iCal link, the row
    shown (and its key) is the one with Google's / Microsoft's stable id."""
    return _uno_calendars() + _ical_calendars()


def uno_status() -> dict:
    """For Settings → Calendars: each calendar of the Uno account and its state —
    connected | not_connected | reconnect | unavailable | error."""
    from . import assistant
    from .config import app_api_config
    with _lock:
        supported, rows, error = _uno["supported"], list(_uno["list"]), _uno["error"]
    by = {r["provider"]: r for r in rows}
    enabled = uno_enabled()
    out = []
    for provider, (cid, short, label) in UNO_PROVIDERS.items():
        r = by.get(provider)
        info = _cache.get(cid) or {}
        if not supported or not r or not r["available"]:
            state = "unavailable"
        elif r["needs_reconnect"]:
            state = "reconnect"
        elif not r["connected"]:
            state = "not_connected"
        else:
            state = info.get("state") or "connected"
            if state == "ok":
                state = "connected"
        out.append({"provider": provider, "id": cid, "name": short, "label": label, "state": state,
                    "account": (r or {}).get("account") or info.get("account") or "",
                    "events": len(info.get("events") or []) if state == "connected" else 0,
                    "fetched_at": info.get("at"),
                    "error": info.get("error", "") if state in ("error", "reconnect", "not_connected") else ""})
    return {"work": bool(app_api_config()), "supported": supported, "enabled": enabled,
            "assistant": assistant.active(), "error": error, "calendars": out}


def public_list() -> list[dict]:
    out = []
    for c in calendars():
        info = _cache.get(c["id"]) or {}
        if c.get("kind") == "uno":
            short = UNO_PROVIDERS.get(c.get("provider") or "gmail", UNO_PROVIDERS["gmail"])[1]
            account = c.get("account") or info.get("account") or ""
            where = f"Uno account · {short}" + (f" · {account}" if account else "")
        else:
            where = _mask(c["url"])
        out.append({"id": c["id"], "name": c.get("name") or info.get("name") or "",
                    "where": where, "kind": c.get("kind", "ical"), "provider": c.get("provider", ""),
                    "state": info.get("state", ""),
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

class CalendarState(ValueError):
    """A calendar that can't be read for a reason the person fixes in Uno Work
    (not connected, sign in again) — a status, not an error."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def _fetch(cal: dict) -> tuple[list[dict], str]:
    if cal.get("kind") == "uno":
        return _fetch_uno(cal)
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


def from_feed(feed: dict, provider: str = "gmail") -> list[dict]:
    """Uno's calendar_feed (Google or Microsoft, the same JSON) → the same
    occurrences parse() makes. The key is the provider's occurrence id: stable
    when the meeting moves; prefixed per provider, so a Google id and a Graph id
    never make the same key."""
    prefix = KEY_PREFIX.get(provider, provider)
    out = []
    for ev in feed.get("events") or []:
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        key = hashlib.sha1(f"{prefix}|{gid}".encode()).hexdigest()[:12]
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
            "key": key, "uid": str(ev.get("ical_uid") or gid), "gid": gid, "provider": provider,
            "title": str(ev.get("title") or "")[:200],
            "start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
            "status": status, "platform": platform, "url": link,
            "organizer": _email(org.get("email")), "organizer_name": str(org.get("name") or ""),
            "organizer_self": bool(org.get("self")) or ev.get("self_response") == "organizer",
            "attendees": people[:100],
        })
    out.sort(key=lambda e: e["start"] or "9999")
    return out


def _fetch_uno(cal: dict | None = None) -> tuple[list[dict], str]:
    from . import uno_app
    cal = cal or {"id": UNO_CAL, "provider": "gmail", "legacy": True}
    provider = cal.get("provider") or "gmail"
    short = UNO_PROVIDERS.get(provider, UNO_PROVIDERS["gmail"])[1]
    if cal.get("needs_reconnect"):
        raise CalendarState("reconnect", f"{short}: sign in again in Uno Work → Settings → Connectors")
    client = _app_client(timeout=45.0)
    if not client:
        raise ValueError("No Uno Work on this computer")
    now = datetime.now(timezone.utc)
    try:
        # An older Uno Work knows only Google and no provider parameter: don't send one.
        window = ((now - timedelta(hours=12)).isoformat(), (now + HORIZON).isoformat())
        feed = client.calendar_events(*window) if cal.get("legacy") else \
            client.calendar_events(*window, provider=provider)
    except uno_app.UnoAppError as exc:
        if exc.code.endswith("_reconnect"):
            raise CalendarState("reconnect", f"{short}: sign in again in Uno Work → Settings → Connectors") from None
        if exc.code.endswith("_not_connected"):
            raise CalendarState("not_connected", f"{short} isn't connected in Uno Work") from None
        if exc.code == "calls_off" or exc.status == 409:
            raise CalendarState("unavailable", "This Uno Work shares the calendar only while an assistant "
                                               "has Join my calls on — update Uno Work") from None
        raise ValueError(f"Uno Work: {exc.message}") from None
    account = str(feed.get("account") or "")
    events = from_feed(feed, provider)
    with _lock:
        info = _cache.get(cal["id"]) or {}
        info["account"] = account
        # Microsoft's calendarView leaves out a deleted meeting instead of saying "cancelled": a meeting
        # missing from a complete feed is gone (bot.reconcile calls the bot off). A feed cut short
        # (complete: false) proves nothing — loaded() stays false. No field (Google, older Work) = complete.
        info["partial"] = feed.get("complete") is False
        _cache[cal["id"]] = info
    return events, account or short


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
    # Teams: the thread id of meetup-join links (encoded differently by Outlook,
    # Google and the Graph's joinUrl), or the meeting number of /meet/ links.
    dec = urllib.parse.unquote(urllib.parse.unquote(u))
    m = re.search(r"teams\.(?:microsoft|live)\.com/l/meetup-join/19:meeting_([^/@\s]+)@thread\.v2", dec, re.I)
    if m:
        return "teams:" + m.group(1)  # the id itself is case-sensitive
    m = re.search(r"teams\.(?:microsoft|live)\.com/meet/(\d{6,20})", dec, re.I)
    if m:
        return "teams:" + m.group(1)
    return re.split(r"[?#]", u)[0].rstrip("/").lower()


def refresh(force: bool = False) -> None:
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        every = REFRESH_UNO_S if cal.get("kind") == "uno" else REFRESH_S
        if not force and time.time() - (info.get("tried") or 0) < every:
            continue
        try:
            events, name = _fetch(cal)
            was = _cache.get(cal["id"]) or {}
            info = {"at": int(time.time()), "tried": time.time(), "events": events, "error": "", "name": name,
                    "state": "ok", "account": was.get("account", ""), "partial": bool(was.get("partial"))}
        except ValueError as exc:
            state = getattr(exc, "state", "error")
            if str(exc) != info.get("error"):  # once per change, not every few minutes
                (log.info if state != "error" else log.warning)("calendar %s: %s", cal["id"], exc)
            # Last good events stay: a calendar that needs signing in again still sends the bot
            # to what was known, and loaded() stays false so nothing is called off for it.
            info = {**info, "tried": time.time(), "error": str(exc), "state": state}
        with _lock:
            _cache[cal["id"]] = info


# ---- who goes ------------------------------------------------------------------

def my_emails() -> set[str]:
    """The person's addresses: Settings, plus each calendar's name when it is
    an address (Google names the primary calendar by its owner's email)."""
    s = load_settings()
    out = {e.strip().lower() for e in re.split(r"[,\s;]+", s.my_emails) if "@" in e}
    with _lock:
        rows = list(_uno["list"])
    for r in rows:  # the accounts of the calendars connected in Uno
        if "@" in r.get("account", ""):
            out.add(r["account"].lower())
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
    chosen = overrides.get("events") or {}
    # the same meeting in two calendars: a choice on either row counts
    choice = next((chosen[k] for k in ev.get("keys") or [ev["key"]] if chosen.get(k)), None)
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
        organizer = bool(ev.get("organizer_self")) or ev.get("organizer") in mine
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
    ev = find_event(key)
    keys = (ev or {}).get("keys") or [key]   # the same meeting in another calendar too
    with _lock:
        d = _load_overrides()
        events = d.setdefault("events", {})
        for k in keys:
            if choice in ("join", "skip"):
                events[k] = choice
            else:
                events.pop(k, None)
        # forget choices for events long gone (keys are per occurrence)
        known = {e["key"] for info in _cache.values() for e in info.get("events") or []}
        if len(events) > 500:
            d["events"] = {k: v for k, v in events.items() if k in known}
        _save_overrides(d)


def _same_meeting_marks(ev: dict) -> list[tuple]:
    """What makes two rows the same meeting: the same call (canonical link) or
    the same UID, at the same minute."""
    try:
        minute = int(datetime.fromisoformat(ev["start"]).timestamp()) // 60
    except (KeyError, TypeError, ValueError):
        return []
    marks = []
    if ev.get("url"):
        marks.append(("link", canonical(ev["url"]), minute))
    if ev.get("uid"):
        marks.append(("uid", ev["uid"], minute))
    return marks


def upcoming(include_past_hours: float = 2) -> list[dict]:
    """Occurrences from every calendar, each with the bot's decision. The same
    meeting in two calendars (an iCal link of the Google calendar that also
    comes through Uno; a Teams invitation in Google and in Outlook) is one row:
    the first calendar's, with every row's key in "keys". The bot goes if any
    of the rows says so; a choice on any of them counts for all."""
    now = datetime.now(timezone.utc)
    overrides = _load_overrides()
    chosen = overrides.get("events") or {}
    mine = my_emails()
    rows: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    marks: dict[tuple, str] = {}
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        for ev in info.get("events") or []:
            if ev.get("end") and datetime.fromisoformat(ev["end"]) < now - timedelta(hours=include_past_hours):
                continue
            if ev["key"] in rows:
                continue
            own = _same_meeting_marks(ev)
            head = next((marks[m] for m in own if m in marks), None)
            if head:
                groups[head].append({**ev, "calendar": cal["id"]})
                rows[head]["keys"].append(ev["key"])
                rows[ev["key"]] = rows[head]
            else:
                head = ev["key"]
                rows[head] = {**ev, "calendar": cal["id"], "keys": [head]}
                groups[head] = [rows[head]]
            for m in own:
                marks.setdefault(m, head)
    out = []
    for head, members in groups.items():
        row = rows[head]
        keys = row["keys"]
        decisions = [decide({**m, "keys": keys}, overrides, mine) for m in members]
        join, why = next((d for d in decisions if d[0]), decisions[0])
        choice = next((chosen[k] for k in keys if chosen.get(k)), "")
        row.update(join=join, why=why, choice=choice, platform_name=PLATFORM_NAMES.get(row["platform"], ""),
                   calendars=[m["calendar"] for m in members])
        out.append(row)
    return sorted(out, key=lambda e: e["start"])


def loaded() -> bool:
    """Every calendar was read at least once without an error and in full (so a
    meeting missing from it is really gone, not just not fetched yet)."""
    for cal in calendars():
        info = _cache.get(cal["id"]) or {}
        if not info.get("at") or info.get("error") or info.get("partial"):
            return False
    return True


def find_event(key: str) -> dict | None:
    return next((e for e in upcoming(include_past_hours=24) if key in (e.get("keys") or [e["key"]])), None)


# ---- the loop: send the bot when a call starts --------------------------------

def start(on_due) -> None:
    """Background thread: refresh calendars, call on_due(event) once when a
    call the bot should join is about to start."""
    def loop():
        while True:
            try:
                from . import bot  # late: bot imports this module
                refresh_uno_list()
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
