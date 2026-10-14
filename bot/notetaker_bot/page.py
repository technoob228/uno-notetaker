"""Looking at a call's page: buttons and fields by accessible name, visible
text — across every frame (Zoom's web client lives in an iframe)."""
from __future__ import annotations

import logging
import re

from playwright.async_api import Error as PWError
from playwright.async_api import Locator, Page

log = logging.getLogger("bot.page")


def rx(patterns: list[str]) -> re.Pattern | None:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


async def text(page: Page) -> str:
    """The visible text of the page and its frames."""
    out = []
    for fr in page.frames:
        try:
            out.append(await fr.evaluate("() => document.body ? document.body.innerText : ''"))
        except PWError:
            continue
    return "\n".join(out)


def says(page_text: str, patterns: list[str]) -> str:
    """The first line of the page text matching any pattern, else ""."""
    r = rx(patterns)
    if not r:
        return ""
    m = r.search(page_text)
    if not m:
        return ""
    start = page_text.rfind("\n", 0, m.start()) + 1
    end = page_text.find("\n", m.end())
    return page_text[start:end if end >= 0 else None].strip()[:200]


async def find(page: Page, role: str, patterns: list[str]) -> Locator | None:
    r = rx(patterns)
    if not r:
        return None
    for fr in page.frames:
        try:
            loc = fr.get_by_role(role, name=r)
            n = await loc.count()
            for i in range(min(n, 6)):
                el = loc.nth(i)
                if await el.is_visible() and await el.is_enabled():
                    return el
        except PWError:
            continue
    return None


async def click(page: Page, patterns: list[str], role: str = "button") -> bool:
    el = await find(page, role, patterns)
    if not el:
        return False
    try:
        await el.click(timeout=3000)
        return True
    except PWError as exc:
        log.debug("click %s failed: %s", patterns, exc)
        return False


async def click_any(page: Page, patterns: list[str]) -> list[str]:
    """Click every visible button (or link) that matches, once each."""
    done = []
    for p in patterns:
        if await click(page, [p]) or await click(page, [p], role="link"):
            done.append(p)
    return done


async def fill(page: Page, patterns: list[str], value: str) -> bool:
    """A text field by its accessible name or placeholder."""
    r = rx(patterns)
    if not r:
        return False
    for fr in page.frames:
        try:
            for loc in (fr.get_by_role("textbox", name=r), fr.get_by_placeholder(r)):
                n = await loc.count()
                for i in range(min(n, 4)):
                    el = loc.nth(i)
                    if await el.is_visible() and await el.is_editable():
                        await el.fill("")
                        await el.type(value, delay=35)  # like a person: some pages listen to key events only
                        return True
        except PWError:
            continue
    return False


async def turn_off(page: Page, patterns: list[str]) -> None:
    """Toggles (role switch / checkbox) for mic and camera that are on."""
    r = rx(patterns)
    if not r:
        return
    for fr in page.frames:
        for role in ("switch", "checkbox"):
            try:
                loc = fr.get_by_role(role, name=r)
                for i in range(min(await loc.count(), 4)):
                    el = loc.nth(i)
                    if await el.is_visible() and await el.is_checked():
                        await el.click(timeout=2000)
            except PWError:
                continue


async def inside(page: Page, spec) -> bool:
    """In the call: its Leave button is there and the Join button is not
    (Jitsi shows "Leave the meeting" on the page before joining too)."""
    return bool(await find(page, "button", spec.in_call)) and not await find(page, "button", spec.join)
