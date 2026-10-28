"""Uno Notetaker bot: waits for calls from the app and joins them.

    python -m notetaker_bot          (in the bot container; see ../Dockerfile)

Environment: NOTETAKER_URL (http://notetaker:8430), BOT_KEY_FILE (the token
the app writes, /bot-key/token), MAX_CALLS (2 — each call is a Chromium of
400–800 MB), DISPLAY (Xvfb; without it Chromium runs headless).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from playwright.async_api import async_playwright

from .api import APP, Api, ApiError, read_token
from .capture import setup_pulse
from .session import Session

VERSION = "0.5.2"
MAX_CALLS = max(1, int(os.environ.get("MAX_CALLS", "2")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


async def main() -> None:
    setup_pulse()
    api = Api()
    running: dict[str, tuple[Session, asyncio.Task]] = {}
    quit_ = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, quit_.set)
    display = bool(os.environ.get("DISPLAY"))
    log.info("Uno Notetaker bot %s → %s, up to %d calls at once, %s", VERSION, APP, MAX_CALLS,
             "headed (Xvfb)" if display else "headless")
    async with async_playwright() as pw:
        backoff = 2
        while not quit_.is_set():
            for sid, (_, task) in list(running.items()):
                if task.done():
                    running.pop(sid)
            if not read_token():
                log.info("waiting for the app to write the bot token (%s)…", os.environ.get("BOT_KEY_FILE"))
                await asyncio.sleep(5)
                continue
            try:
                work = await api.get("/work", capacity=MAX_CALLS, running=",".join(running), wait=20,
                                     version=VERSION)
                backoff = 2
            except (ApiError, OSError, Exception) as exc:  # noqa: BLE001 — the app restarts, the bot waits
                log.warning("app not reachable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if quit_.is_set():
                break  # stopping: whatever was just handed out goes back to the queue (the app sees it lost)
            for job in work.get("start") or []:
                if job["id"] in running:
                    continue
                s = Session(job, api, pw, display)
                running[job["id"]] = (s, asyncio.create_task(s.run(), name=f"call-{job['id']}"))
                log.info("joining %s (%s)", job["url"], job.get("title") or "no title")
            for sid in work.get("stop") or []:
                if sid in running:
                    running[sid][0].stop.set()
        # Shutting down (docker stop): leave every call properly, keep the audio.
        for s, _ in running.values():
            s.stop.set()
        if running:
            await asyncio.wait([t for _, t in running.values()], timeout=25)


if __name__ == "__main__":
    asyncio.run(main())
