"""One call: open the link, get in, record, leave.

    joining → lobby (waiting to be let in) → recording → done
                                          ↘ not_admitted / failed

The app is told at each step (and every 15 s while in the call — the
answer says whether the person pressed "Remove the bot"). The bot leaves
when the call ends for it (ended / removed), when it is alone, when nobody
has made a sound for a while, or at the latest an hour after the scheduled
end.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from playwright.async_api import Browser, Page, Playwright
from playwright.async_api import Error as PWError

from . import page as P
from .api import Api
from .capture import Recorder, Sink
from .platforms import spec_for

log = logging.getLogger("bot.session")

SETUP_S = 120            # from opening the link to pressing Join
QUIET_LIMIT_S = 15 * 60  # nobody made a sound this long → the call is over
QUIET_AFTER_END_S = 3 * 60
ALONE_S = 90
UPLOAD_EVERY_S = 5
REPORT_EVERY_S = 15


class Refused(Exception):
    def __init__(self, outcome: str, detail: str):
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def _outcome(why: str) -> str:
    """Refused by people (not let in) or by the link (wrong, ended, needs sign-in)?"""
    people = ("denied", "declined", "let you in", "responded", "не впустил", "отклонил", "removed")
    return "not_admitted" if any(w in why.lower() for w in people) else "failed"


INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


class Session:
    def __init__(self, job: dict, api: Api, pw: Playwright, display: bool):
        self.job = job
        self.id = job["id"]
        self.api = api
        self.pw = pw
        self.display = display
        self.spec = spec_for(job.get("platform", "other"))
        self.stop = asyncio.Event()
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.rec: Recorder | None = None
        self.sent = 0
        self.last_report = 0.0
        self.why_left = "Left the call"

    # -- talking to the app -------------------------------------------------------

    async def report(self, status: str, detail: str = "") -> None:
        self.last_report = time.time()
        try:
            r = await self.api.post(f"/sessions/{self.id}/status", {"status": status, "detail": detail})
            if r.get("stop"):
                self.stop.set()
        except Exception as exc:  # noqa: BLE001 — the app may be restarting; the call goes on
            log.warning("[%s] report %s: %s", self.id, status, exc)

    async def upload(self, final: bool = False) -> None:
        if not self.rec:
            return
        data = self.rec.take()
        if not data and not final:
            return
        buf = data
        for attempt in range(6 if final else 2):
            try:
                if buf:
                    r = await self.api.put(f"/sessions/{self.id}/audio?offset={self.sent}&ext=ogg", buf)
                    self.sent = int(r.get("bytes", self.sent + len(buf)))
                return
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status", 0)
                if status == 409:  # the meeting was deleted or finished in the app: leave
                    log.info("[%s] the app stopped taking audio: %s", self.id, exc)
                    self.stop.set()
                    return
                log.warning("[%s] upload (%d bytes) failed: %s", self.id, len(buf), exc)
                await asyncio.sleep(2 * (attempt + 1))
        # keep it for the next round instead of losing it
        self.rec.pending[:0] = buf

    async def screenshot(self, why: str) -> None:
        if not self.page:
            return
        try:
            png = await self.page.screenshot(type="png", timeout=5000)
            await self.api.put(f"/sessions/{self.id}/screenshot?why={why}", png, ctype="image/png")
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] screenshot: %s", self.id, exc)

    # -- the call -------------------------------------------------------------------

    async def run(self) -> None:
        outcome, detail = "done", ""
        try:
            with Sink(self.id) as sink:
                await self.report("joining", f"Opening {self.spec.name}…")
                await self._open(sink.name)
                await self._join()
                await self._in_call(sink.name)
                outcome, detail = "done", self.why_left
        except Refused as r:
            outcome, detail = r.outcome, r.detail
            await self.screenshot(r.outcome)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] failed", self.id)
            outcome, detail = "failed", f"The bot hit an error: {str(exc).splitlines()[0][:200]}"
            await self.screenshot("error")
        finally:
            await self._close()
        if self.rec:
            await self.upload(final=True)
        log.info("[%s] %s — %s", self.id, outcome, detail)
        try:
            await self.api.post(f"/sessions/{self.id}/finish", {"outcome": outcome, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] finish: %s", self.id, exc)

    async def _open(self, sink: str) -> None:
        env = {**os.environ, "PULSE_SINK": sink}  # replaces the environment: keep DISPLAY, PULSE_SERVER
        self.browser = await self.pw.chromium.launch(
            headless=not self.display, env=env,
            # Playwright mutes Chromium by default — the sound is the whole point.
            ignore_default_args=["--mute-audio", "--enable-automation"],
            args=["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
                  "--disable-blink-features=AutomationControlled", "--lang=en-US", "--disable-dev-shm-usage",
                  "--no-first-run", "--no-default-browser-check", "--disable-features=Translate,MediaRouter",
                  "--window-size=1280,800"])
        ctx = await self.browser.new_context(locale="en-US", viewport={"width": 1280, "height": 760},
                                             permissions=["microphone", "camera", "notifications"],
                                             extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        await ctx.add_init_script(INIT_JS)
        self.page = await ctx.new_page()
        url = self.spec.url(self.job["url"])
        log.info("[%s] opening %s", self.id, url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    async def _join(self) -> None:
        pg, sp = self.page, self.spec
        assert pg
        named, filled, pressed = False, False, False
        deadline = time.time() + SETUP_S
        while not pressed:
            if self.stop.is_set():
                raise Refused("failed", "Cancelled before the bot got in")
            if time.time() > deadline:
                t = (await P.text(pg)).strip().replace("\n", " · ")[:160]
                raise Refused("failed", f"Could not find how to join on the page ({t or 'empty page'})")
            t = await P.text(pg)
            if (why := P.says(t, sp.denied)):
                raise Refused(_outcome(why), f"{sp.name}: {why}")
            if await P.inside(pg, sp) and not P.says(t, sp.lobby):
                return  # let in straight away
            if P.says(t, sp.lobby) and named:
                break
            await P.click_any(pg, sp.before)
            if not named:
                named = await P.fill(pg, sp.name_field, self.job["name"])
            if not filled:
                for label, value in sp.fields.items():
                    filled = await P.fill(pg, [label], value) or filled
            await P.click_any(pg, sp.mute)
            await P.turn_off(pg, sp.mute_switches)
            btn = await P.find(pg, "button", sp.join)
            if btn and (named or time.time() > deadline - SETUP_S / 2):
                await asyncio.sleep(0.5)
                try:
                    await btn.click(timeout=4000)
                    pressed = True
                    log.info("[%s] pressed join (name filled: %s)", self.id, named)
                except PWError:
                    pass
            await asyncio.sleep(1.5)
        # Waiting to be let in.
        wait_until = time.time() + int(self.job.get("lobby_wait_s") or 900)
        told = False
        while True:
            if self.stop.is_set():
                raise Refused("failed", "Cancelled while waiting to be let in")
            t = await P.text(pg)
            if (why := P.says(t, sp.denied)):
                raise Refused(_outcome(why), f"{sp.name}: {why}")
            if await P.inside(pg, sp) and not P.says(t, sp.lobby):
                return
            if (why := P.says(t, sp.ended)):  # Teams: "We can't find this meeting" + "Rejoin call"
                raise Refused("failed", f"{sp.name}: {why}")
            if not told and P.says(t, sp.lobby):
                await self.report("lobby")
                told = True
            elif time.time() - self.last_report > REPORT_EVERY_S:
                await self.report("lobby" if told else "joining")
            if time.time() > wait_until:
                mins = int(self.job.get("lobby_wait_s") or 900) // 60
                raise Refused("not_admitted", f"Nobody let the bot in within {mins} min")
            await P.click_any(pg, sp.before)
            await asyncio.sleep(1)  # every second counts: recording starts when we notice we're in

    async def _in_call(self, sink: str) -> None:
        pg, sp = self.page, self.spec
        assert pg
        self.rec = Recorder(sink)
        await self.rec.start()
        await self.report("recording")
        joined = time.time()
        end = self.job.get("end") or ""
        end_ts = datetime.fromisoformat(end).timestamp() if end else 0
        hard_stop = joined + int(self.job.get("max_s") or 4 * 3600)
        chat_done = not self.job.get("chat_message")
        alone_since = 0.0
        gone_checks = 0
        last_upload = time.time()
        while True:
            now = time.time()
            if now - joined < 40:
                await P.click_any(pg, sp.after_join)  # "Join audio by computer", popups
            if not chat_done and now - joined > 4:
                chat_done = await self._say_in_chat(self.job["chat_message"]) or now - joined > 60
            if now - last_upload >= UPLOAD_EVERY_S:
                await self.upload()
                last_upload = now
            if now - self.last_report >= REPORT_EVERY_S:
                await self.report("recording", self._status_line())
            if self.stop.is_set():
                self.why_left = "Removed from the call in Notetaker"
                break
            t = await P.text(pg)
            if (why := P.says(t, sp.ended)):
                self.why_left = f"The call ended ({why})"
                break
            if await P.inside(pg, sp):
                gone_checks = 0
            else:
                gone_checks += 1
                if gone_checks >= 5:
                    self.why_left = "The bot is no longer in the call"
                    break
            if P.says(t, sp.alone):
                alone_since = alone_since or now
                if now - alone_since > ALONE_S:
                    self.why_left = "Everyone else left"
                    break
            else:
                alone_since = 0.0
            quiet = self.rec.quiet_for()
            if quiet > QUIET_LIMIT_S or (end_ts and now > end_ts and quiet > QUIET_AFTER_END_S):
                self.why_left = f"Nobody has spoken for {int(quiet // 60)} min"
                break
            if now > hard_stop:
                self.why_left = "The call went on for too long — the bot left (time limit)"
                break
            if self.rec.proc and self.rec.proc.returncode is not None:
                raise RuntimeError("the audio recorder stopped")
            await asyncio.sleep(3)
        await self.report("leaving", self.why_left)
        await self._leave()

    def _status_line(self) -> str:
        mins = int((time.time() - (self.rec.started if self.rec else time.time())) // 60)
        return f"In the call, recording · {mins} min"

    async def _say_in_chat(self, message: str) -> bool:
        pg, sp = self.page, self.spec
        assert pg
        try:
            box = await P.find(pg, "textbox", sp.chat_input)
            if not box:
                await P.click(pg, sp.chat_open)
                await asyncio.sleep(1.5)
                box = await P.find(pg, "textbox", sp.chat_input)
            if not box:
                return False
            await box.click(timeout=3000)
            await box.type(message, delay=10)
            await box.press("Enter")
            await asyncio.sleep(0.5)
            await P.click(pg, sp.chat_open)  # close the panel again (it is a toggle on most platforms)
            log.info("[%s] said hello in the chat", self.id)
            return True
        except PWError as exc:
            log.info("[%s] chat: %s", self.id, exc)
            return False

    async def _leave(self) -> None:
        pg = self.page
        if not pg:
            return
        try:
            if await P.click(pg, self.spec.leave):
                await asyncio.sleep(1)
                await P.click(pg, self.spec.leave_confirm)
                await asyncio.sleep(1)
        except PWError:
            pass

    async def _close(self) -> None:
        if self.rec:
            await self.rec.stop()
        if self.browser:
            try:
                await self.browser.close()
            except PWError:
                pass

