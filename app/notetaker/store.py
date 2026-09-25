"""Meetings on disk: ~/Meetings/<date time title>/ with plain files.

  meeting.json     metadata + status + chat history (this app's own file)
  audio/           original tracks (mic.webm, tab.webm, upload.*) + recording.ogg
  transcript.md    human-readable transcript with [mm:ss]
  transcript.json  segments {start,end,speaker,text}
  notes.md         the AI notes (the person may edit them)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import MEETINGS_DIR

_lock = threading.RLock()
_index: dict[str, str] = {}  # id → folder path
ID_RE = re.compile(r"^[a-z0-9]{10}$")
TRACK_RE = re.compile(r"^(mic|tab|upload|bot)$")
EXT_RE = re.compile(r"^[a-z0-9]{1,5}$")


def _safe_title(title: str) -> str:
    title = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", title or "").strip()
    return re.sub(r"\s+", " ", title)[:60] or "Meeting"


def _rescan() -> None:
    _index.clear()
    if not os.path.isdir(MEETINGS_DIR):
        return
    for name in os.listdir(MEETINGS_DIR):
        path = os.path.join(MEETINGS_DIR, name)
        meta = os.path.join(path, "meeting.json")
        if os.path.isfile(meta):
            try:
                with open(meta) as fh:
                    mid = json.load(fh).get("id", "")
                if ID_RE.match(mid):
                    _index[mid] = path
            except (OSError, ValueError):
                continue


def folder(mid: str) -> str | None:
    if not ID_RE.match(mid or ""):
        return None
    with _lock:
        path = _index.get(mid)
        if not path or not os.path.isdir(path):
            _rescan()
            path = _index.get(mid)
        return path


def _local_now(tz_offset_min: int | None) -> datetime:
    """Now in the person's time zone (the browser sends its offset; the
    container runs in UTC), with the offset in the ISO string so every
    browser shows the right time."""
    if tz_offset_min is None or not -900 <= tz_offset_min <= 900:
        return datetime.now().astimezone()
    return datetime.now(timezone(timedelta(minutes=tz_offset_min)))


def create(title: str, source: str, template: str, language: str, tz_offset_min: int | None = None) -> dict:
    with _lock:
        os.makedirs(MEETINGS_DIR, exist_ok=True)
        now = _local_now(tz_offset_min)
        mid = secrets.token_hex(5)
        base = f"{now:%Y-%m-%d %H-%M} {_safe_title(title)}"
        path, n = os.path.join(MEETINGS_DIR, base), 2
        while os.path.exists(path):
            path, n = os.path.join(MEETINGS_DIR, f"{base} ({n})"), n + 1
        os.makedirs(os.path.join(path, "audio"))
        meta = {
            "id": mid, "title": title.strip() or "", "created_at": now.isoformat(timespec="seconds"),
            "source": source, "template": template, "language": language,
            "status": "recording" if source == "browser" else "uploading",
            "progress": "", "error": "", "duration": 0, "tracks": {},
            "share": None, "chat": [], "usage": {}, "folder": os.path.basename(path),
        }
        _index[mid] = path
        _write(path, meta)
        return meta


def _write(path: str, meta: dict) -> None:
    tmp = os.path.join(path, ".meeting.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(path, "meeting.json"))


def get(mid: str) -> dict | None:
    path = folder(mid)
    if not path:
        return None
    try:
        with open(os.path.join(path, "meeting.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def update(mid: str, **changes) -> dict | None:
    with _lock:
        meta = get(mid)
        if meta is None:
            return None
        meta.update(changes)
        _write(folder(mid), meta)
        return meta


def rename_folder(mid: str, title: str) -> None:
    """'2026-09-23 10-30 Meeting' → '2026-09-23 10-30 Pilot launch with Oleg'
    once the AI has named an untitled meeting — so it reads well in Files."""
    with _lock:
        path = folder(mid)
        base = os.path.basename(path)
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2} Meeting( \(\d+\))?$", base):
            return
        stem = base[:16]
        new, n = os.path.join(MEETINGS_DIR, f"{stem} {_safe_title(title)}"), 2
        while os.path.exists(new):
            new, n = os.path.join(MEETINGS_DIR, f"{stem} {_safe_title(title)} ({n})"), n + 1
        try:
            os.rename(path, new)
        except OSError:
            return
        _index[mid] = new
        update(mid, folder=os.path.basename(new))


def read_text(mid: str, name: str) -> str:
    path = folder(mid)
    if not path:
        return ""
    try:
        with open(os.path.join(path, name), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def write_text(mid: str, name: str, text: str) -> None:
    path = folder(mid)
    tmp = os.path.join(path, f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, os.path.join(path, name))


def track_path(mid: str, track: str, ext: str) -> str:
    return os.path.join(folder(mid), "audio", f"{track}.{ext}")


def append_track(mid: str, track: str, ext: str, data: bytes, offset: int) -> int:
    """Append a piece of a recording/upload. `offset` = bytes the client
    already sent; a repeated piece (retry after a network blip) is ignored."""
    with _lock:
        meta = get(mid)
        tracks = meta.get("tracks", {})
        info = tracks.get(track) or {"ext": ext, "bytes": 0}
        if info["ext"] != ext:
            raise ValueError("track extension changed")
        if offset < info["bytes"]:
            return info["bytes"]  # duplicate
        if offset > info["bytes"]:
            raise ValueError(f"gap in upload: server has {info['bytes']} bytes, got offset {offset}")
        with open(track_path(mid, track, ext), "ab") as fh:
            fh.write(data)
        info["bytes"] += len(data)
        tracks[track] = info
        update(mid, tracks=tracks)
        return info["bytes"]


def list_all() -> list[dict]:
    with _lock:
        _rescan()
        out = []
        for mid in list(_index):
            m = get(mid)
            if m:
                out.append({k: m.get(k) for k in ("id", "title", "created_at", "status", "duration",
                                                  "source", "template", "progress", "folder")})
        out.sort(key=lambda m: m["created_at"] or "", reverse=True)
        return out


# ---- search -------------------------------------------------------------------
#
# Across every meeting: title, notes (line by line) and the transcript
# (utterance by utterance, so a hit knows its timestamp and speaker). All
# words of the query must appear in the meeting; lines are ranked by how many
# of the words they hold. Texts are cached per file mtime, so typing in the
# search box does not re-read ~/Meetings on every key.

_text_cache: dict[str, tuple[tuple, object]] = {}


def _cached(mid: str, name: str, parse):
    path = folder(mid)
    if not path:
        return parse("")
    full = os.path.join(path, name)
    try:
        st = os.stat(full)
    except OSError:
        return parse("")
    # inode too: every write is a new file (os.replace), and two edits within
    # one mtime tick must not look the same.
    mtime = (st.st_ino, st.st_mtime_ns, st.st_size)
    key = f"{mid}/{name}/{getattr(parse, '__name__', '')}"
    hit = _text_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    value = parse(read_text(mid, name))
    _text_cache[key] = (mtime, value)
    return value


def _raw(text: str) -> str:
    return text


def _segments(raw: str) -> list:
    try:
        segs = json.loads(raw or "[]")
        return segs if isinstance(segs, list) else []
    except ValueError:
        return []


def _snippet(text: str, words: list[str], width: int = 150) -> str:
    low = text.lower()
    pos = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, pos - 50)
    out = text[start:start + width].strip()
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


def _clean_md(line: str) -> str:
    line = re.sub(r"^\s*(#+|[-*]\s+\[[ xX]\]|[-*]|\d+[.)])\s*", "", line)
    line = re.sub(r"\s*\[\d{1,2}:\d{2}(?::\d{2})?\]", "", line)
    return line.replace("**", "").strip()


def search(q: str, limit_hits: int = 3) -> list[dict]:
    words = [w for w in re.split(r"\s+", q.strip().lower()) if w][:8]
    if not words:
        return list_all()
    out = []
    for m in list_all():
        mid = m["id"]
        notes = _cached(mid, "notes.md", _raw)
        segs = _cached(mid, "transcript.json", _segments)
        title = (m["title"] or "").lower()
        blob = " ".join([title, notes.lower(), " ".join(str(x.get("text", "")).lower() for x in segs)])
        if not all(w in blob for w in words):
            continue
        hits = []
        for line in notes.splitlines():
            clean = _clean_md(line)
            n = sum(w in clean.lower() for w in words)
            if n and clean.lower() != title:
                hits.append((n + 0.5, {"where": "notes", "text": _snippet(clean, words)}))
        for x in segs:
            text = str(x.get("text", ""))
            n = sum(w in text.lower() for w in words)
            if n:
                hits.append((n, {"where": "transcript", "text": _snippet(text, words), "ts": x.get("start", 0),
                                 "speaker": x.get("speaker") or ""}))
        hits.sort(key=lambda h: -h[0])
        score = sum(w in title for w in words) * 3 + (hits[0][0] if hits else 0)
        out.append((score, m["created_at"] or "", {**m, "hits": [h[1] for h in hits[:limit_hits]],
                                                   "hit_count": len(hits)}))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [x[2] for x in out]


def delete(mid: str) -> bool:
    with _lock:
        path = folder(mid)
        if not path:
            return False
        shutil.rmtree(path)
        _index.pop(mid, None)
        return True


def by_share_token(token: str) -> dict | None:
    if not token or len(token) < 16:
        return None
    for m in list_all():
        full = get(m["id"])
        share = (full or {}).get("share") or {}
        if share.get("token") and secrets.compare_digest(share["token"], token):
            return full
    return None


def stale_recordings(max_idle_s: int = 6 * 3600) -> list[str]:
    """Recordings (and uploads) whose browser went away without finishing."""
    out = []
    for m in list_all():
        if m["status"] in ("recording", "uploading"):
            path = folder(m["id"])
            try:
                if time.time() - os.path.getmtime(os.path.join(path, "meeting.json")) > max_idle_s:
                    out.append(m["id"])
            except OSError:
                pass
    return out
