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
from datetime import datetime

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


def create(title: str, source: str, template: str, language: str) -> dict:
    with _lock:
        os.makedirs(MEETINGS_DIR, exist_ok=True)
        now = datetime.now()
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


def search(q: str) -> list[dict]:
    q = q.strip().lower()
    if not q:
        return list_all()
    hits = []
    for m in list_all():
        text = " ".join([m["title"] or "", read_text(m["id"], "notes.md"),
                         read_text(m["id"], "transcript.md")]).lower()
        pos = text.find(q)
        if pos >= 0:
            snippet = text[max(0, pos - 60): pos + 90].replace("\n", " ")
            hits.append({**m, "snippet": snippet})
    return hits


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
    """Recordings whose browser went away without pressing Stop."""
    out = []
    for m in list_all():
        if m["status"] == "recording":
            path = folder(m["id"])
            try:
                if time.time() - os.path.getmtime(os.path.join(path, "meeting.json")) > max_idle_s:
                    out.append(m["id"])
            except OSError:
                pass
    return out
