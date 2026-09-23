"""Uno Notetaker — HTTP API + the single-page UI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from collections import defaultdict

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import ai, pipeline, store
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
PUBLIC_PREFIXES = ("/login", "/s/", "/static/", "/healthz", "/favicon")


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
    }


@app.get("/api/state")
async def state():
    return _state()


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
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


@app.get("/api/meetings")
async def list_meetings(q: str = ""):
    return {"meetings": store.search(q) if q else store.list_all()}


@app.post("/api/meetings")
async def create_meeting(request: Request):
    body = await request.json()
    source = body.get("source")
    if source not in ("browser", "upload"):
        raise HTTPException(400, "source must be browser or upload")
    s = load_settings()
    template = body.get("template") if body.get("template") in ai.TEMPLATES else s.template
    lang = body.get("language") if body.get("language") in ("auto", "ru", "en") else s.language
    return store.create(str(body.get("title", ""))[:120], source, template, lang)


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
    return {**m, "segments": segments, "notes": store.read_text(mid, "notes.md"), "has_audio": has_audio,
            "path": f"~/Meetings/{m.get('folder', '')}"}


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


# ---- startup ---------------------------------------------------------------

def write_manifest() -> None:
    """~/.uno/apps/notetaker.json — Uno Work shows the app on the desktop."""
    d = os.environ.get("UNO_APPS_DIR", "/uno-apps")
    if not os.path.isdir(d):
        return
    manifest = {"name": "Notetaker", "icon": "🎙️", "port": PORT,
                "description": "Meeting notes: record a call or upload a file — transcript, summary, action items.",
                # The AI of this computer (Uno Work App API): chat + speech-to-text, no agent
                # tasks, at most $10 unless the person raises it in Settings → Apps.
                "ai": {"chat": True, "tasks": False, "limitUsd": 10}}
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
