"""When notes are ready: tell the person (Uno Work Inbox, Telegram), and hand
an action item to the AI of the computer (a Uno Work task).

Everything here is best effort: a failed notification never fails a meeting.
What happened is kept in meeting.json → "delivery" and shown in the app.
"""
from __future__ import annotations

import logging
import time

from . import actions, ai, store, uno_app
from .config import ROUTE_APP, ai_provider, app_api_config, load_settings

log = logging.getLogger("notetaker.deliver")


def _app_client() -> uno_app.Client | None:
    cfg = app_api_config()
    return uno_app.Client(url=cfg["url"], token=cfg["token"], timeout=30.0) if cfg else None


def inbox_available() -> bool:
    return app_api_config() is not None


def notify_inbox(mid: str) -> str:
    """Post "Notes ready" to the Uno Work Inbox. → "sent" or why not."""
    client = _app_client()
    if not client:
        return "no Uno Work on this computer"
    m = store.get(mid) or {}
    items = [it for it in actions.for_meeting(mid) if not it["done"]]
    title = f"Notes ready: {m.get('title') or 'Meeting'}"[:140]
    if items:
        n = len(items)
        body = f"{n} action item{'s' if n != 1 else ''} · " + "; ".join(actions.one_line(it) for it in items[:3])
    else:
        bullets = actions.summary_bullets(store.read_text(mid, "notes.md"), 2)
        body = " ".join(bullets) or "No action items."
    try:
        client.notify(title, body=body[:500], open={"app": True, "path": f"/m/{mid}"}, group=f"meeting-{mid}")
        return "sent"
    except uno_app.UnoAppError as exc:
        if exc.code == "notify_not_allowed":
            return "Uno Work did not let this app post to the Inbox"
        return f"Inbox: {exc.message}"


def after_notes(mid: str) -> None:
    """Called by the pipeline once notes are written. Sends once per meeting
    (a rewrite of the notes does not notify again)."""
    m = store.get(mid) or {}
    if m.get("delivery", {}).get("at"):
        return
    from . import telegram  # late: telegram imports the pipeline
    s = load_settings()
    out: dict = {"at": int(time.time())}
    if s.notify_inbox:
        out["inbox"] = notify_inbox(mid)
    from_telegram = bool((m.get("telegram") or {}).get("chat_id"))
    if (s.notify_telegram or from_telegram) and telegram.ready():
        try:
            telegram.send_notes(mid)
            out["telegram"] = "sent"
        except telegram.TelegramError as exc:
            out["telegram"] = str(exc)
    store.update(mid, delivery=out)
    log.info("meeting %s delivered: %s", mid, out)


def after_error(mid: str) -> None:
    """A Telegram recording failed: say so in the chat it came from."""
    m = store.get(mid) or {}
    tg = m.get("telegram") or {}
    if not tg.get("chat_id"):
        return
    from . import telegram
    try:
        telegram.send(f"Sorry, I couldn't make notes from that recording: {telegram._esc(m.get('error', ''))}",
                      tg["chat_id"], tg.get("message_id"))
    except telegram.TelegramError:
        pass


TASK_PROMPT = """You help with an action item from a meeting.

Meeting: {title} ({date})
Action item: {item}

The meeting's notes and transcript are in the folder {folder} (notes.md, transcript.md) — read them for context.
Do what you can on this computer to move the action item forward: draft the email or message, prepare the
document, look up what is needed. Save anything you write next to the notes in that folder.
Do not send anything to other people and do not spend money — leave that to me; tell me what is ready
and what I still need to do."""


def hand_to_ai(mid: str, item_id: str) -> dict:
    """Start a Uno Work task (a chat the person sees) for one action item."""
    m = store.get(mid)
    it = actions.find(mid, item_id) if m else None
    if not it:
        raise ai.AIError("No such action item (the notes may have changed) — reload the page.")
    p = ai_provider()
    client = _app_client()
    if not client or p.route != ROUTE_APP:
        raise ai.AIError("Handing tasks to the AI needs Uno Work on this computer (the App API).")
    folder = f"~/Meetings/{m.get('folder', '')}"
    prompt = TASK_PROMPT.format(title=m.get("title") or "Meeting", date=(m.get("created_at") or "")[:10],
                                item=actions.one_line(it), folder=folder)
    try:
        task = client.task(prompt, cwd=folder, title=it["text"][:80], tools="ask")
    except uno_app.UnoAppError as exc:
        raise ai._app_error(exc, "Hand to AI") from None
    handed = dict(m.get("handed") or {})
    handed[item_id] = {"task": task.id, "thread": task.thread_id, "at": int(time.time())}
    store.update(mid, handed=handed)
    return {"task": task.id, "thread": task.thread_id, "tools": task.tools}
