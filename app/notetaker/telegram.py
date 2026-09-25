"""Telegram: the person's own bot — recordings in, notes out.

Why a bot of their own (a token from @BotFather) and not a shared Uno bot: the
notes never pass through anyone else's server, and nothing on the computer
has to be reachable from the internet — the app long-polls Telegram.

  * Pairing: Settings → Telegram → paste the token → the app shows a link
    t.me/<bot>?start=<code>. The first chat that sends that code (valid 15
    minutes) becomes the only chat the bot talks to.
  * In: a voice message, an audio/video file or a round video from that chat →
    a new meeting (Telegram lets bots download files up to 20 MB — about an
    hour of a voice message).
  * Out: when notes are ready — the summary and action items, with a link to
    the meeting in the app. /todo — open action items, /last — the last
    meeting, /find <words> — search.
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import time

import httpx

from . import actions, pipeline, store
from .config import APP_URL, STATE_DIR, USER_AGENT, load_settings, save_settings

log = logging.getLogger("notetaker.telegram")
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
HTTP = httpx.Client(timeout=httpx.Timeout(70.0, connect=15.0), headers={"User-Agent": USER_AGENT})
MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API getFile limit
PAIR_TTL = 15 * 60
MSG_MAX = 4000

_pair: dict = {}          # {"code", "until"}
_wake = threading.Event()  # token changed: restart polling at once
_started = False


class TelegramError(RuntimeError):
    pass


def call(token: str, method: str, **params) -> dict | list | bool:
    try:
        resp = HTTP.post(f"{API}/bot{token}/{method}", json=params)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramError(f"Telegram is not reachable: {exc}") from None
    if not body.get("ok"):
        desc = body.get("description") or f"HTTP {resp.status_code}"
        if resp.status_code == 401:
            desc = "Telegram did not accept this bot token — copy it again from @BotFather"
        raise TelegramError(desc)
    return body["result"]


def _offset_path() -> str:
    return os.path.join(STATE_DIR, "telegram.json")


def _load_offset() -> int:
    try:
        with open(_offset_path()) as fh:
            return int(json.load(fh).get("offset", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _save_offset(offset: int) -> None:
    try:
        with open(_offset_path(), "w") as fh:
            json.dump({"offset": offset}, fh)
    except OSError:
        pass


# ---- settings side -----------------------------------------------------------

def connect(token: str) -> dict:
    """Check the token, remember the bot, and start pairing."""
    token = token.strip()
    if token.startswith("•"):
        token = load_settings().telegram_token
    if not token or ":" not in token:
        raise TelegramError("Paste the token @BotFather gave you (it looks like 123456:ABC…)")
    me = call(token, "getMe")
    s = load_settings()
    changed = token != s.telegram_token
    save_settings({"telegram_token": token}, internal=True)
    save_settings({"telegram_bot_name": me.get("username", ""),
                   **({"telegram_chat_id": "", "telegram_chat_name": ""} if changed else {})}, internal=True)
    _pair.update(code=secrets.token_hex(4), until=time.time() + PAIR_TTL)
    _wake.set()
    return status()


def disconnect() -> dict:
    save_settings({"telegram_token": ""}, internal=True)
    save_settings({"telegram_chat_id": "", "telegram_chat_name": "", "telegram_bot_name": ""}, internal=True)
    _pair.clear()
    _wake.set()
    return status()


def status() -> dict:
    s = load_settings()
    out = {"connected": bool(s.telegram_token), "bot": s.telegram_bot_name,
           "chat": s.telegram_chat_name if s.telegram_chat_id else "", "paired": bool(s.telegram_chat_id)}
    if s.telegram_token and not s.telegram_chat_id and _pair.get("until", 0) > time.time():
        out["code"] = _pair["code"]
        out["link"] = f"https://t.me/{s.telegram_bot_name}?start={_pair['code']}"
    return out


def ready() -> bool:
    s = load_settings()
    return bool(s.telegram_token and s.telegram_chat_id)


# ---- sending -------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def meeting_link(mid: str) -> str:
    return f"{APP_URL}/m/{mid}" if APP_URL else ""


def send(text: str, chat_id: str | None = None, reply_to: int | None = None) -> None:
    s = load_settings()
    chat = chat_id or s.telegram_chat_id
    if not (s.telegram_token and chat):
        raise TelegramError("Telegram is not connected")
    params = {"chat_id": chat, "text": text[:MSG_MAX], "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    call(s.telegram_token, "sendMessage", **params)


def notes_message(mid: str) -> str:
    m = store.get(mid) or {}
    notes = store.read_text(mid, "notes.md")
    items = [it for it in actions.parse(notes) if not it["done"]]
    lines = [f"<b>📝 {_esc(m.get('title') or 'Meeting notes')}</b>"]
    bullets = actions.summary_bullets(notes, 5)
    if bullets:
        lines += [""] + [f"• {_esc(b)}" for b in bullets]
    if items:
        lines += ["", f"<b>Action items ({len(items)})</b>"]
        lines += [f"☐ {_esc(actions.one_line(it))}" for it in items[:12]]
    link = meeting_link(mid)
    if link:
        lines += ["", f'<a href="{_esc(link)}">Open the notes</a>']
    return "\n".join(lines)


def send_notes(mid: str) -> None:
    m = store.get(mid) or {}
    tg = m.get("telegram") or {}
    send(notes_message(mid), reply_to=tg.get("message_id"))


# ---- receiving -----------------------------------------------------------------

HELP = ("Send me a voice message or an audio/video recording of a meeting — I'll reply with the notes "
        "and action items.\n\n/todo — open action items\n/last — the last meeting\n/find words — search meetings")


def _media(msg: dict) -> tuple[dict, str, str] | None:
    """(file object, extension, title) of a recording in the message, or None."""
    for kind, ext in (("voice", "ogg"), ("audio", "m4a"), ("video", "mp4"), ("video_note", "mp4")):
        f = msg.get(kind)
        if f:
            name = f.get("file_name") or ""
            if "." in name:
                ext = name.rsplit(".", 1)[1].lower()[:5]
            return f, ext, (msg.get("caption") or os.path.splitext(name)[0] or "").strip()
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith(("audio/", "video/")):
        name = doc.get("file_name") or "recording"
        ext = name.rsplit(".", 1)[1].lower()[:5] if "." in name else "bin"
        return doc, ext, (msg.get("caption") or os.path.splitext(name)[0]).strip()
    return None


def _take_recording(token: str, msg: dict, media: tuple[dict, str, str]) -> None:
    f, ext, title = media
    chat = str(msg["chat"]["id"])
    if not store.EXT_RE.match(ext):
        ext = "bin"
    if int(f.get("file_size") or 0) > MAX_DOWNLOAD:
        send("This file is bigger than 20 MB — Telegram doesn't let bots download that. "
             + (f"Upload it in the app: {_esc(APP_URL)}" if APP_URL else "Upload it in the app."),
             chat, msg.get("message_id"))
        return
    info = call(token, "getFile", file_id=f["file_id"])
    s = load_settings()
    m = store.create(title[:120], "telegram", s.template, s.language,
                     s.tz_offset_min if s.tz_offset_min != -10000 else None)
    try:
        with HTTP.stream("GET", f"{API}/file/bot{token}/{info['file_path']}") as resp:
            resp.raise_for_status()
            offset = 0
            for chunk in resp.iter_bytes(1024 * 1024):
                offset = store.append_track(m["id"], "upload", ext, chunk, offset)
    except (httpx.HTTPError, ValueError) as exc:
        store.update(m["id"], status="error", error=f"Could not download the file from Telegram: {exc}")
        send("I couldn't download that file from Telegram — try sending it again.", chat, msg.get("message_id"))
        return
    store.update(m["id"], status="queued", progress="Waiting in line…",
                 telegram={"chat_id": chat, "message_id": msg.get("message_id")})
    pipeline.enqueue(m["id"], "full")
    dur = f.get("duration")
    took = f" ({int(dur) // 60}:{int(dur) % 60:02d})" if dur else ""
    send(f"Got it{took} — transcribing. I'll send the notes here in a minute or two.", chat, msg.get("message_id"))


def _command(msg: dict, text: str) -> None:
    chat = str(msg["chat"]["id"])
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd == "/todo":
        items = actions.across_meetings()[:20]
        if not items:
            send("No open action items. 🎉", chat)
            return
        lines = [f"<b>Open action items ({len(items)})</b>"]
        last = None
        for it in items:
            if it["meeting_id"] != last:
                lines.append(f"\n<i>{_esc(it['meeting_title'])}</i>")
                last = it["meeting_id"]
            lines.append(f"☐ {_esc(actions.one_line(it))}")
        send("\n".join(lines), chat)
    elif cmd == "/last":
        done = [m for m in store.list_all() if m["status"] == "done"]
        send(notes_message(done[0]["id"]) if done else "No meetings yet.", chat)
    elif cmd in ("/find", "/search"):
        if not arg.strip():
            send("What should I look for? Example: /find pricing", chat)
            return
        hits = store.search(arg)[:5]
        if not hits:
            send("Nothing found.", chat)
            return
        lines = []
        for m in hits:
            link = meeting_link(m["id"])
            head = f'<a href="{_esc(link)}">{_esc(m["title"] or "Untitled")}</a>' if link else \
                f"<b>{_esc(m['title'] or 'Untitled')}</b>"
            lines.append(f"{head} · {_esc((m['created_at'] or '')[:10])}")
            for h in m.get("hits", [])[:2]:
                lines.append(f"  … {_esc(h['text'])}")
        send("\n".join(lines), chat)
    else:
        send(HELP, chat)


def handle(token: str, update: dict) -> None:
    msg = update.get("message") or {}
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    s = load_settings()
    chat = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not s.telegram_chat_id:
        code = _pair.get("code")
        if code and _pair.get("until", 0) > time.time() and text in (f"/start {code}", code):
            who = msg["chat"].get("first_name") or msg["chat"].get("username") or "you"
            save_settings({"telegram_chat_id": chat, "telegram_chat_name": who}, internal=True)
            _pair.clear()
            send(f"Connected ✓ This bot now sends meeting notes to {_esc(who)} only.\n\n{HELP}", chat)
        else:
            send("This is a private notetaker bot. To connect, open Notetaker → Settings → Telegram.", chat)
        return
    if chat != s.telegram_chat_id:
        send("This is a private notetaker bot.", chat)
        return
    media = _media(msg)
    if media:
        _take_recording(token, msg, media)
    elif text.startswith("/"):
        _command(msg, text)
    else:
        send(HELP, chat)


def _loop() -> None:
    offset = _load_offset()
    while True:
        token = load_settings().telegram_token
        if not token:
            _wake.wait(30)
            _wake.clear()
            continue
        _wake.clear()
        try:
            updates = call(token, "getUpdates", offset=offset, timeout=25,
                           allowed_updates=["message"])
        except TelegramError as exc:
            log.warning("telegram: %s", exc)
            _wake.wait(20)
            continue
        for up in updates or []:
            offset = max(offset, int(up.get("update_id", 0)) + 1)
            _save_offset(offset)
            try:
                handle(token, up)
            except Exception:  # noqa: BLE001 — one bad message must not stop the bot
                log.exception("telegram: update failed")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="notetaker-telegram").start()
