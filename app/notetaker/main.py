"""Uno Notetaker — HTTP API + the single-page UI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections import defaultdict
from urllib.parse import quote

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import actions, ai, deliver, pipeline, store, telegram
from .config import (ADMIN_PASSWORD, APP_URL, APP_VERSION, MODEL_CHOICES, ROUTE_APP, ai_provider,
                     cookie_secret, load_settings, notes_model, save_settings)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notetaker")

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
PORT = int(os.environ.get("PORT", "8430"))
COOKIE = "nt_session"
SESSION_TTL = 30 * 24 * 3600
MAX_PIECE = 16 * 1024 * 1024

app = FastAPI(title="Uno Notetaker", docs_url=None, redoc_url=None, openapi_url=None)


# ---- auth ------------------------------------------------------------------

def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(cookie_secret(), payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def _session_ok(request: Request) -> bool:
    if not ADMIN_PASSWORD:
        return True  # no password configured (local development only; the image refuses this in prod)
    raw = request.cookies.get(COOKIE, "")
    try:
        ver, ts, sig = raw.split(".")
        pw_tag = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:12]
        return ver == "v1" and time.time() - int(ts) < SESSION_TTL and \
            hmac.compare_digest(sig, _sign(f"v1.{ts}.{pw_tag}"))
    except (ValueError, TypeError):
        return False


_attempts: dict[str, list[float]] = defaultdict(list)
PUBLIC_PREFIXES = ("/login", "/s/", "/static/", "/healthz", "/favicon", "/manifest.webmanifest", "/sw.js",
                   "/widget", "/m/", "/share-target")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC_PREFIXES) and path != "/" and not _session_ok(request):
        return JSONResponse({"error": "login required"}, status_code=401)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


@app.post("/login")
async def login(request: Request):
    ip = _client_ip(request)
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 300]
    if len(_attempts[ip]) >= 10:
        return JSONResponse({"error": "Too many attempts. Wait 5 minutes."}, status_code=429)
    body = await request.json()
    if not ADMIN_PASSWORD or not hmac.compare_digest(str(body.get("password", "")), ADMIN_PASSWORD):
        _attempts[ip].append(now)
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    ts = str(int(now))
    pw_tag = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:12]
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, f"v1.{ts}.{_sign(f'v1.{ts}.{pw_tag}')}", max_age=SESSION_TTL, httponly=True,
                    secure=request.headers.get("x-forwarded-proto") == "https", samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---- pages -----------------------------------------------------------------

@app.get("/")
async def index(request: Request):
    page = "index.html" if _session_ok(request) else "login.html"
    return FileResponse(os.path.join(STATIC, page), headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---- settings --------------------------------------------------------------

def _state() -> dict:
    s = load_settings()
    p = ai_provider(s)
    return {
        "version": APP_VERSION, "app_url": APP_URL, "settings": s.public(),
        "provider": {"name": p.name, "ready": p.ready, "source": p.source, "base_url": p.base_url,
                     "route": p.route, "route_label": p.route_label, "model": notes_model(s, p)},
        "templates": {k: v["name"] for k, v in ai.TEMPLATES.items()},
        "models": MODEL_CHOICES, "password_protected": bool(ADMIN_PASSWORD),
        # What this computer offers: the Uno Work App API (Inbox, tasks for the AI) and Telegram.
        "uno_work": p.route == ROUTE_APP, "inbox": deliver.inbox_available(),
        "telegram": telegram.status(),
    }


@app.get("/api/state")
async def state():
    return _state()


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    body.pop("telegram_token", None)  # only through /api/telegram/connect (checked with Telegram)
    if body.get("provider") == "custom":
        url = str(body.get("custom_base_url", "")).strip()
        if not url.startswith(("https://", "http://")):
            raise HTTPException(400, "The provider address must start with https://")
    save_settings(body)
    return _state()


def _silence_ogg() -> str:
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ac", "1", "-c:a", "libopus", "-b:a", "24k", path], capture_output=True, timeout=30)
    return path


@app.post("/api/settings/test")
def test_settings():  # sync on purpose: FastAPI runs it in a thread (network calls block)
    """Check both halves: the notes model and speech-to-text."""
    s = load_settings()
    p = ai_provider(s)
    out = {"provider": p.name, "source": p.source, "route": p.route, "route_label": p.route_label}
    try:
        text, used = ai.chat(p, s, [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=5)
        out["notes"] = {"ok": True, "detail": f"{used.get('model') or notes_model(s, p)} via {p.route_label} "
                                              f"answered: {text[:20]}"}
    except ai.AIError as exc:
        out["notes"] = {"ok": False, "detail": str(exc)}
    if s.stt == "local":
        out["stt"] = {"ok": True, "detail": f"Whisper {s.local_model} on this computer (downloads on first use)"}
    else:
        path = _silence_ogg()
        try:
            ai.transcribe_remote(p, s, path)
            out["stt"] = {"ok": True, "detail": "speech-to-text is available"}
        except ai.AIError as exc:
            if exc.status in (401, 403) and p.name == "uno":
                why = ("Uno Work did not let this app use speech-to-text" if p.route == ROUTE_APP else
                       "Uno AI speech-to-text isn't enabled for this computer's key yet")
                out["stt"] = {"ok": True, "detail": (
                    f"{why} — recordings are transcribed on this computer (Whisper {s.local_model}) instead")}
            else:
                out["stt"] = {"ok": False, "detail": str(exc)}
        finally:
            os.remove(path)
    return out


# ---- meetings --------------------------------------------------------------

def _meeting_or_404(mid: str) -> dict:
    m = store.get(mid)
    if not m:
        raise HTTPException(404, "No such meeting")
    return m


def _open_actions(mid: str) -> int:
    return sum(1 for it in store._cached(mid, "notes.md", actions.parse) if not it["done"])


@app.get("/api/meetings")
async def list_meetings(q: str = ""):
    meetings = store.search(q) if q.strip() else store.list_all()
    for m in meetings:
        m["open_actions"] = _open_actions(m["id"]) if m["status"] == "done" else 0
    return {"meetings": meetings}


@app.get("/api/actions")
async def list_actions(done: int = 0):
    return {"actions": actions.across_meetings(include_done=bool(done))}


@app.post("/api/meetings")
async def create_meeting(request: Request):
    body = await request.json()
    source = body.get("source")
    if source not in ("browser", "upload"):
        raise HTTPException(400, "source must be browser or upload")
    s = load_settings()
    template = body.get("template") if body.get("template") in ai.TEMPLATES else s.template
    lang = body.get("language") if body.get("language") in ("auto", "ru", "en") else s.language
    tz = body.get("tz_offset")
    if isinstance(tz, (int, float)) and int(tz) != s.tz_offset_min and -900 <= int(tz) <= 900:
        save_settings({"tz_offset_min": int(tz)}, internal=True)
    return store.create(str(body.get("title", ""))[:120], source, template, lang,
                        int(tz) if isinstance(tz, (int, float)) else None)


@app.put("/api/meetings/{mid}/tracks/{track}")
async def append_track(mid: str, track: str, request: Request, ext: str = "webm", offset: int = 0):
    m = _meeting_or_404(mid)
    if not store.TRACK_RE.match(track) or not store.EXT_RE.match(ext):
        raise HTTPException(400, "bad track")
    if m["status"] not in ("recording", "uploading"):
        raise HTTPException(409, "This meeting is no longer recording")
    data = await request.body()
    if len(data) > MAX_PIECE:
        raise HTTPException(413, "piece too large")
    try:
        total = store.append_track(mid, track, ext, data, offset)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"bytes": total}


@app.post("/api/meetings/{mid}/finish")
async def finish(mid: str):
    m = _meeting_or_404(mid)
    if m["status"] in ("recording", "uploading"):
        store.update(mid, status="queued", progress="Waiting in line…")
        pipeline.enqueue(mid, "full")
    return store.get(mid)


@app.get("/api/meetings/{mid}")
async def get_meeting(mid: str):
    m = _meeting_or_404(mid)
    try:
        segments = json.loads(store.read_text(mid, "transcript.json") or "[]")
    except ValueError:
        segments = []
    has_audio = os.path.exists(os.path.join(store.folder(mid), "audio", "recording.ogg"))
    notes = store.read_text(mid, "notes.md")
    return {**m, "segments": segments, "notes": notes, "has_audio": has_audio,
            "path": f"~/Meetings/{m.get('folder', '')}", "actions": actions.parse(notes),
            "talk_time": pipeline.talk_time(segments)}


@app.put("/api/meetings/{mid}")
async def edit_meeting(mid: str, request: Request):
    _meeting_or_404(mid)
    body = await request.json()
    changes = {}
    if "title" in body:
        changes["title"] = str(body["title"])[:120]
    if body.get("template") in ai.TEMPLATES:
        changes["template"] = body["template"]
    if "notes" in body:
        store.write_text(mid, "notes.md", str(body["notes"]))
    if changes:
        store.update(mid, **changes)
    return await get_meeting(mid)


@app.post("/api/meetings/{mid}/retry")
async def retry(mid: str):
    m = _meeting_or_404(mid)
    if m["status"] in ("queued", "transcribing", "summarizing"):
        return m
    kind = "notes" if store.read_text(mid, "transcript.json") else "full"
    store.update(mid, status="queued" if kind == "full" else "summarizing", error="", progress="Waiting in line…")
    pipeline.enqueue(mid, kind)
    return store.get(mid)


@app.post("/api/meetings/{mid}/regenerate")
async def regenerate(mid: str, request: Request):
    m = _meeting_or_404(mid)
    body = await request.json()
    if body.get("template") in ai.TEMPLATES:
        store.update(mid, template=body["template"])
    if not store.read_text(mid, "transcript.json"):
        raise HTTPException(409, "There is no transcript yet")
    if m["status"] not in ("queued", "transcribing", "summarizing"):
        store.update(mid, status="summarizing", progress="Writing notes…", error="")
        pipeline.enqueue(mid, "notes")
    return store.get(mid)


@app.post("/api/meetings/{mid}/ask")
async def ask(mid: str, request: Request):
    m = _meeting_or_404(mid)
    question = str((await request.json()).get("question", "")).strip()[:2000]
    if not question:
        raise HTTPException(400, "Ask something")
    transcript = store.read_text(mid, "transcript.md")
    if not transcript:
        raise HTTPException(409, "There is no transcript yet")
    s = load_settings()
    history = (m.get("chat") or [])[-8:]
    messages = [{"role": "system", "content": ai.ask_prompt(m.get("title") or "Meeting", transcript,
                                                            store.read_text(mid, "notes.md"))}]
    messages += [{"role": x["role"], "content": x["content"]} for x in history]
    messages.append({"role": "user", "content": question})
    try:
        answer, usage = await run_in_threadpool(ai.chat, ai_provider(s), s, messages, 1200)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc
    chat = (m.get("chat") or []) + [{"role": "user", "content": question},
                                    {"role": "assistant", "content": answer}]
    store.update(mid, chat=chat[-40:])
    return {"answer": answer, "chat": chat[-40:], "usage": usage}


@app.post("/api/meetings/{mid}/share")
async def share(mid: str, request: Request):
    _meeting_or_404(mid)
    body = await request.json()
    token = secrets.token_urlsafe(18)
    store.update(mid, share={"token": token, "transcript": bool(body.get("transcript")),
                             "created_at": int(time.time())})
    return {"url": f"{APP_URL or ''}/s/{token}", "token": token}


@app.delete("/api/meetings/{mid}/share")
async def unshare(mid: str):
    _meeting_or_404(mid)
    store.update(mid, share=None)
    return {"ok": True}


@app.patch("/api/meetings/{mid}/actions/{aid}")
async def toggle_action(mid: str, aid: str, request: Request):
    _meeting_or_404(mid)
    body = await request.json()
    it = actions.set_done(mid, aid, bool(body.get("done")))
    if not it:
        raise HTTPException(404, "No such action item — the notes may have changed. Reload the page.")
    return it


@app.post("/api/meetings/{mid}/actions/{aid}/ai")
def action_to_ai(mid: str, aid: str):  # sync: the App API call blocks
    _meeting_or_404(mid)
    try:
        return deliver.hand_to_ai(mid, aid)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/meetings/{mid}/speakers")
async def rename_speaker(mid: str, request: Request):
    m = _meeting_or_404(mid)
    if m["status"] in ("queued", "transcribing", "summarizing"):
        raise HTTPException(409, "Wait until processing finishes")
    body = await request.json()
    n = pipeline.rename_speaker(mid, str(body.get("from", "")), str(body.get("to", "")))
    if not n:
        raise HTTPException(400, "Nothing to rename")
    return await get_meeting(mid)


@app.post("/api/meetings/{mid}/send")
def send_meeting(mid: str, to: str = "telegram"):  # sync: network calls
    m = _meeting_or_404(mid)
    if m["status"] != "done":
        raise HTTPException(409, "The notes are not ready yet")
    if to == "inbox":
        res = deliver.notify_inbox(mid)
        if res != "sent":
            raise HTTPException(502, res)
    else:
        try:
            telegram.send_notes(mid)
        except telegram.TelegramError as exc:
            raise HTTPException(502, str(exc)) from exc
    return {"ok": True}


@app.get("/api/meetings/{mid}/export")
async def export(mid: str, what: str = "all"):
    m = _meeting_or_404(mid)
    parts = []
    if what in ("notes", "all"):
        parts.append(store.read_text(mid, "notes.md").strip())
    if what in ("transcript", "all"):
        parts.append(store.read_text(mid, "transcript.md").strip())
    name = re.sub(r"[^\w\- ]+", "", m.get("title") or "meeting").strip()[:60] or "meeting"
    suffix = {"notes": " - notes", "transcript": " - transcript"}.get(what, "")
    fname = f"{(m.get('created_at') or '')[:10]} {name}{suffix}.md"
    return Response("\n\n---\n\n".join(p for p in parts if p) + "\n", media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


# ---- telegram --------------------------------------------------------------

@app.get("/api/telegram")
async def telegram_status():
    return telegram.status()


@app.post("/api/telegram/connect")
def telegram_connect(body: dict):  # sync: calls Telegram
    try:
        return telegram.connect(str(body.get("token", "")))
    except telegram.TelegramError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/telegram")
async def telegram_disconnect():
    return telegram.disconnect()


@app.post("/api/telegram/test")
def telegram_test():
    try:
        telegram.send("✓ Notetaker can reach you here. Meeting notes will arrive in this chat.")
    except telegram.TelegramError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/meetings/{mid}")
async def delete_meeting(mid: str):
    m = _meeting_or_404(mid)
    if m["status"] in ("queued", "transcribing", "summarizing"):
        raise HTTPException(409, "Wait until processing finishes")
    store.delete(mid)
    return {"ok": True}


@app.get("/api/meetings/{mid}/audio")
async def meeting_audio(mid: str):
    _meeting_or_404(mid)
    path = os.path.join(store.folder(mid), "audio", "recording.ogg")
    if not os.path.exists(path):
        raise HTTPException(404, "No audio yet")
    return FileResponse(path, media_type="audio/ogg")


# ---- public share page -----------------------------------------------------

SHARE_TMPL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{title}</title><link rel="stylesheet" href="/static/share.css"></head><body><main>
<p class="kicker">Meeting notes · {date}</p>{notes}{transcript}
<footer>Shared from Uno Notetaker</footer></main></body></html>"""


@app.get("/s/{token}", response_class=HTMLResponse)
async def shared(token: str):
    m = store.by_share_token(token)
    if not m:
        return HTMLResponse("<h1>This link is no longer shared</h1>", status_code=404)
    notes_html = md.markdown(html.escape(store.read_text(m["id"], "notes.md"), quote=False),
                             extensions=["sane_lists"])
    notes_html = notes_html.replace("[ ]", "☐").replace("[x]", "☑")
    tr = ""
    if (m.get("share") or {}).get("transcript"):
        tr = "<details><summary>Transcript</summary>" + md.markdown(
            html.escape(store.read_text(m["id"], "transcript.md"), quote=False)) + "</details>"
    return HTMLResponse(SHARE_TMPL.format(title=html.escape(m.get("title") or "Meeting notes"),
                                          date=html.escape(m["created_at"][:10]), notes=notes_html,
                                          transcript=tr), headers={
        "Cache-Control": "no-store",
        # notes are Markdown the owner (or an LLM) wrote: no scripts, ever
        "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'"})


# ---- deep links, Home widget, installable app ----------------------------------

@app.get("/m/{mid}")
async def meeting_link(mid: str):
    """A plain link to a meeting (Inbox "Open", Telegram) → the app at that meeting."""
    if not store.ID_RE.match(mid):
        return RedirectResponse("/")
    return RedirectResponse(f"/#/m/{mid}")


def widget_key() -> str:
    """Read-only key for the Home widget. Uno Work shows the widget in a frame
    on another site, where the login cookie is not sent (third-party), so the
    widget page takes this key instead. It shows the last meeting's title and
    open action items — nothing else; changes if the cookie secret does."""
    return hmac.new(cookie_secret(), b"notetaker-widget-v1", hashlib.sha256).hexdigest()[:32]


WIDGET_TMPL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<meta http-equiv="refresh" content="{refresh}"><title>Notetaker</title><style>
:root {{ color-scheme: light dark; --muted: #74726c; --line: rgba(128,128,128,.25); --accent: #2f5bea; }}
@media (prefers-color-scheme: dark) {{ :root {{ --muted: #a3a19b; --accent: #8aa6ff; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 12px 14px; font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  background: transparent; overflow: hidden; }}
a {{ color: inherit; text-decoration: none; }}
.k {{ color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; }}
.t {{ font-weight: 650; font-size: 15px; margin: 2px 0 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.m {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
.n {{ display: inline-block; font-weight: 650; color: var(--accent); }}
ul {{ list-style: none; margin: 4px 0 0; padding: 0; }}
li {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0; }}
li::before {{ content: "☐ "; color: var(--muted); }}
b {{ font-weight: 600; }} .more {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
.empty {{ color: var(--muted); margin-top: 18px; }}
</style></head><body>{body}</body></html>"""


def _widget_body() -> tuple[str, int]:
    esc = html.escape
    base = APP_URL or ""
    meetings = store.list_all()
    busy = next((m for m in meetings if m["status"] in ("recording", "uploading", "queued", "transcribing",
                                                         "summarizing")), None)
    done = next((m for m in meetings if m["status"] == "done"), None)
    if not done and not busy:
        return (f'<div class="k">Notetaker</div><p class="empty">No meetings yet. '
                f'<a href="{esc(base)}/" target="_blank" rel="noopener"><span class="n">Record one →</span></a></p>', 300)
    parts = []
    if busy:
        what = "Recording…" if busy["status"] == "recording" else (busy.get("progress") or "Working…")
        parts.append(f'<div class="k">Now</div><a href="{esc(base)}/m/{busy["id"]}" target="_blank" rel="noopener">'
                     f'<div class="t">{esc(busy["title"] or "New meeting")}</div><div class="m">{esc(what)}</div></a>')
    if done:
        items = [it for it in actions.for_meeting(done["id"]) if not it["done"]]
        n = len(items)
        count = f'<span class="n">{n} action item{"s" if n != 1 else ""}</span>' if n else "no action items"
        head = "Last meeting" if not busy else "Before that"
        parts.append(f'<a href="{esc(base)}/m/{done["id"]}" target="_blank" rel="noopener">'
                     f'<div class="k">{head}</div><div class="t">{esc(done["title"] or "Untitled meeting")}</div>'
                     f'<div class="m">{esc(_when(done["created_at"]))} · {count}</div></a>')
        if items and not busy:
            lis = "".join(f"<li>{('<b>' + esc(it['owner']) + ':</b> ') if it['owner'] else ''}{esc(it['text'])}</li>"
                          for it in items[:3])
            more = f'<div class="more">+{n - 3} more</div>' if n > 3 else ""
            parts.append(f"<ul>{lis}</ul>{more}")
    return "".join(parts), 20 if busy else 300


def _when(iso: str) -> str:
    try:
        from datetime import datetime
        d = datetime.fromisoformat(iso)
        return d.strftime("%b %-d, %H:%M")
    except (ValueError, TypeError):
        return ""


@app.get("/widget", response_class=HTMLResponse)
async def widget(request: Request, k: str = ""):
    if not (_session_ok(request) or (k and hmac.compare_digest(k, widget_key()))):
        return HTMLResponse('<p style="font:13px sans-serif;color:#74726c">Open Notetaker once to show this widget.</p>',
                            status_code=401)
    body, refresh = _widget_body()
    return HTMLResponse(WIDGET_TMPL.format(body=body, refresh=refresh), headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        "Referrer-Policy": "no-referrer"})


@app.get("/manifest.webmanifest")
async def web_manifest():
    """Installable on a phone (Add to Home Screen); on Android the installed
    app is a Share target — share a voice memo or a recording to Notetaker."""
    return JSONResponse({
        "name": "Uno Notetaker", "short_name": "Notetaker", "start_url": "/", "scope": "/", "id": "/",
        "display": "standalone", "background_color": "#f7f7f5", "theme_color": "#f7f7f5",
        "description": "Meeting notes: record or upload — transcript, summary, action items.",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
        "share_target": {"action": "/share-target", "method": "POST", "enctype": "multipart/form-data",
                         "params": {"title": "title", "text": "text",
                                    "files": [{"name": "audio", "accept": [
                                        "audio/*", "video/*", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".webm",
                                        ".mp4", ".mov", ".aac", ".amr", ".flac"]}]}},
    }, media_type="application/manifest+json")


@app.api_route("/share-target", methods=["GET", "POST"])
async def share_target_fallback():
    """The service worker takes shared files; this only runs when it isn't
    installed yet (first open) — ask to open the app once and share again."""
    return RedirectResponse("/#/share-missed", status_code=303)


@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


# ---- startup ---------------------------------------------------------------

def write_manifest() -> None:
    """~/.uno/apps/notetaker.json — Uno Work shows the app on the desktop."""
    d = os.environ.get("UNO_APPS_DIR", "/uno-apps")
    if not os.path.isdir(d):
        return
    manifest = {"name": "Notetaker", "icon": "🎙️", "port": PORT,
                "description": "Meeting notes: record a call or upload a file — transcript, summary, action items.",
                # The AI of this computer (Uno Work App API): chat + speech-to-text, and
                # tasks (an action item handed to the AI becomes a Work chat the person
                # sees; asked with tools "ask"), at most $10 unless the person raises it
                # in Settings → Apps.
                "ai": {"chat": True, "tasks": True, "limitUsd": 10},
                # "Notes ready: … · 3 action items" in the Uno Work Inbox.
                "notify": True,
                # Home widget "Last meeting · 3 action items" (the key: see widget_key).
                "widget": {"path": f"/widget?k={widget_key()}", "size": "small", "title": "Last meeting"}}
    if APP_URL:
        manifest["url"] = APP_URL
    try:
        path = os.path.join(d, "notetaker.json")
        with open(path + ".tmp", "w") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        os.replace(path + ".tmp", path)
    except OSError as exc:
        log.warning("could not write the Uno Work manifest: %s", exc)


@app.on_event("startup")
async def startup():
    for mid in store.stale_recordings():
        store.update(mid, status="queued", progress="Recording was interrupted — processing what was saved…")
    pipeline.start_worker()
    telegram.start()
    write_manifest()
    p = ai_provider()
    log.info("Uno Notetaker %s on :%d — AI: %s via %s (%s)", APP_VERSION, PORT, p.name, p.route_label,
             p.source if p.ready else "NO KEY")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/login")
async def login_page():
    return RedirectResponse("/")
