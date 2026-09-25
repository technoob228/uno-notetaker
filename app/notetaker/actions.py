"""Action items — parsed from notes.md, the single source of truth.

The notes model writes them as Markdown task lines under "## Action items":

    - [ ] **Oleg**: send the contract (due: Friday) [12:40]

The person may edit notes.md (in the app or in Files), so items are never
stored separately: they are read from the file and "done" is the [x] of the
line. Toggling rewrites that one line.
"""
from __future__ import annotations

import hashlib
import re

from . import store

TASK_RE = re.compile(r"^(\s*[-*]\s+\[)( |x|X)(\]\s+)(.*)$")
HEADING_RE = re.compile(r"^#{1,4}\s+(.*)$")
# Headings the notes model uses for action items (EN/RU) — other task lists
# (a "By person" section, say) are not action items.
ACTION_HEADINGS = re.compile(r"(?i)action items|next steps|задачи|действия|поручения|следующие шаги")
OWNER_RE = re.compile(r"^\*\*(.+?)\*\*\s*[:—-]\s*(.*)$")
TS_RE = re.compile(r"\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*$")
DUE_RE = re.compile(r"\s*\((?:due|срок)\s*:\s*([^)]+)\)", re.I)
# The model sometimes drops "due:" in other languages: "(завтра)", "(до пятницы)".
TAIL_PAREN_RE = re.compile(r"\s*\(([^()]{1,32})\)\s*$")


def _item_id(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:8]


def _ts_seconds(ts: str) -> int:
    n = 0
    for part in ts.split(":"):
        n = n * 60 + int(part)
    return n


def parse(notes: str) -> list[dict]:
    """[{id, line, owner, text, due, ts, done}] in the order of the notes."""
    items, in_actions = [], False
    for i, line in enumerate(notes.splitlines()):
        h = HEADING_RE.match(line)
        if h:
            in_actions = bool(ACTION_HEADINGS.search(h.group(1)))
            continue
        m = TASK_RE.match(line)
        if not m or not in_actions:
            continue
        body = m.group(4).strip()
        ts = ""
        t = TS_RE.search(body)
        if t:
            ts = t.group(1)
            body = body[:t.start()].rstrip()
        owner = ""
        o = OWNER_RE.match(body)
        if o:
            owner, body = o.group(1).strip(), o.group(2).strip()
            if owner in ("—", "-", "–"):
                owner = ""
        due = ""
        d = DUE_RE.search(body) or TAIL_PAREN_RE.search(body)
        if d:
            due = d.group(1).strip()
            body = (body[:d.start()] + body[d.end():]).strip()
        body = body.rstrip(" .;")
        if not body:
            continue
        items.append({"id": _item_id(m.group(4)), "line": i, "owner": owner, "text": body, "due": due,
                      "ts": ts, "ts_seconds": _ts_seconds(ts) if ts else None,
                      "done": m.group(2).lower() == "x"})
    return items


def for_meeting(mid: str) -> list[dict]:
    return parse(store.read_text(mid, "notes.md"))


def set_done(mid: str, item_id: str, done: bool) -> dict | None:
    notes = store.read_text(mid, "notes.md")
    lines = notes.split("\n")
    for it in parse(notes):
        if it["id"] == item_id:
            m = TASK_RE.match(lines[it["line"]])
            lines[it["line"]] = f"{m.group(1)}{'x' if done else ' '}{m.group(3)}{m.group(4)}"
            store.write_text(mid, "notes.md", "\n".join(lines))
            return {**it, "done": done}
    return None


def find(mid: str, item_id: str) -> dict | None:
    return next((it for it in for_meeting(mid) if it["id"] == item_id), None)


def across_meetings(include_done: bool = False, limit: int = 300) -> list[dict]:
    """Open action items of every finished meeting, newest meeting first."""
    out = []
    for m in store.list_all():
        if m["status"] != "done":
            continue
        for it in for_meeting(m["id"]):
            if it["done"] and not include_done:
                continue
            out.append({**it, "meeting_id": m["id"], "meeting_title": m["title"] or "Untitled meeting",
                        "meeting_at": m["created_at"]})
            if len(out) >= limit:
                return out
    return out


def one_line(it: dict) -> str:
    who = f"{it['owner']}: " if it.get("owner") else ""
    due = f" ({it['due']})" if it.get("due") else ""
    return f"{who}{it['text']}{due}"


def summary_bullets(notes: str, limit: int = 6) -> list[str]:
    """The "## Summary" bullets of the notes (for Telegram / Inbox)."""
    out, inside = [], False
    for line in notes.splitlines():
        h = HEADING_RE.match(line)
        if h:
            if inside:
                break
            inside = bool(re.search(r"(?i)summary|итоги|кратко|резюме", h.group(1)))
            continue
        if inside:
            m = re.match(r"^\s*[-*]\s+(.*)$", line)
            if m:
                out.append(TS_RE.sub("", m.group(1).replace("**", "")).strip())
    return out[:limit]
