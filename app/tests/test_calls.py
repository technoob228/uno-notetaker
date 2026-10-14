"""v4: calendars (iCal) → the bot joins calls.

    python3 -m unittest discover -s app/tests       (from the repo root)

Needs icalendar + recurring-ical-events (in requirements.txt; skipped
without them) and httpx.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TMP = tempfile.mkdtemp(prefix="nt-calls-")
os.environ["MEETINGS_DIR"] = MEETINGS = os.path.join(TMP, "Meetings")
os.environ["STATE_DIR"] = STATE = os.path.join(TMP, "state")
os.environ["BOT_KEY_FILE"] = os.path.join(TMP, "bot-key", "token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("icalendar", "recurring_ical_events", "httpx"))

if HAS_DEPS:
    from notetaker import config, store  # noqa: E402
    if config.MEETINGS_DIR != os.environ["MEETINGS_DIR"]:  # imported earlier by another test module
        config.MEETINGS_DIR = store.MEETINGS_DIR = os.environ["MEETINGS_DIR"]
        config.STATE_DIR = os.environ["STATE_DIR"]
    from notetaker import bot, calendars, pipeline  # noqa: E402
    bot.STATE_DIR = calendars.STATE_DIR = config.STATE_DIR
    bot.KEY_FILE = os.environ["BOT_KEY_FILE"]

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def ics(*events: str, name: str = "me@example.com") -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", f"X-WR-CALNAME:{name}",
                        *events, "END:VCALENDAR"])
    return body.encode()


def event(uid: str, start: str, end: str, summary: str, *extra: str) -> str:
    return "\r\n".join(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}",
                        *extra, "END:VEVENT"])


def reset_state(test: unittest.TestCase):
    """Our own folders — test modules are all imported before any runs, and
    each points config at its folders at import."""
    for mod, name, value in ((config, "STATE_DIR", STATE), (bot, "STATE_DIR", STATE),
                             (calendars, "STATE_DIR", STATE),
                             (config, "MEETINGS_DIR", MEETINGS),
                             (store, "MEETINGS_DIR", MEETINGS)):
        test.addCleanup(setattr, mod, name, getattr(mod, name))
        setattr(mod, name, value)
    store._index.clear()
    shutil.rmtree(config.STATE_DIR, ignore_errors=True)
    shutil.rmtree(config.MEETINGS_DIR, ignore_errors=True)
    os.makedirs(config.STATE_DIR)
    calendars._cache.clear()


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class Links(unittest.TestCase):
    def test_every_platform(self):
        cases = {
            "Join with Google Meet: https://meet.google.com/abc-defg-hij": ("meet", "https://meet.google.com/abc-defg-hij"),
            "Zoom: https://us02web.zoom.us/j/81234567890?pwd=AbC.1 ID: 812": ("zoom", "https://us02web.zoom.us/j/81234567890?pwd=AbC.1"),
            "<https://teams.microsoft.com/l/meetup-join/19%3ameeting_X%40thread.v2/0?context=%7b%22Tid%22%3a%22a%22%7d>":
                ("teams", "https://teams.microsoft.com/l/meetup-join/19%3ameeting_X%40thread.v2/0?context=%7b%22Tid%22%3a%22a%22%7d"),
            "Телемост: https://telemost.yandex.ru/j/12345678901234.": ("telemost", "https://telemost.yandex.ru/j/12345678901234"),
            "https://telemost.360.yandex.ru/j/5555": ("telemost", "https://telemost.360.yandex.ru/j/5555"),
            "Join: https://meeting.zoho.eu/meeting/videomeeting?key=123456 now": ("zoho", "https://meeting.zoho.eu/meeting/videomeeting?key=123456"),
            "a&amp;b https://meet.jit.si/MyRoom)": ("jitsi", "https://meet.jit.si/MyRoom"),
        }
        for text, want in cases.items():
            self.assertEqual(calendars.find_link(text), want, text)
        self.assertEqual(calendars.find_link("Lunch at the office", "https://example.com/menu"), ("", ""))
        self.assertEqual(calendars.platform_of("https://example.com/call"), "other")

    def test_html_description(self):
        html = '<a href="https://zoom.us/j/123456789?pwd=x&amp;uname=y">Join</a>'
        self.assertEqual(calendars.find_link(html), ("zoom", "https://zoom.us/j/123456789?pwd=x&uname=y"))


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class Parse(unittest.TestCase):
    def setUp(self):
        reset_state(self)

    def test_recurring_cancelled_declined_allday(self):
        data = ics(
            # daily stand-up with Meet, one occurrence moved, one deleted
            event("standup@x", "20261012T100000Z", "20261012T101500Z", "Stand-up",
                  "RRULE:FREQ=DAILY;COUNT=10", "EXDATE:20261016T100000Z",
                  "DESCRIPTION:Join with Google Meet: https://meet.google.com/abc-defg-hij",
                  "ATTENDEE;CN=Oleg;PARTSTAT=ACCEPTED:mailto:oleg@example.com",
                  "ATTENDEE;CN=Me;PARTSTAT=ACCEPTED:mailto:me@example.com"),
            event("standup@x", "20261015T120000Z", "20261015T121500Z", "Stand-up (moved)",
                  "RECURRENCE-ID:20261015T100000Z",
                  "DESCRIPTION:https://meet.google.com/abc-defg-hij"),
            event("zoom@x", "20261014T150000Z", "20261014T160000Z", "Client call",
                  "LOCATION:https://zoom.us/j/81234567890?pwd=abc",
                  "ATTENDEE;CN=Client;PARTSTAT=ACCEPTED:mailto:c@client.com",
                  "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com"),
            event("gone@x", "20261014T130000Z", "20261014T140000Z", "Cancelled call", "STATUS:CANCELLED",
                  "LOCATION:https://meet.google.com/zzz-zzzz-zzz"),
            "BEGIN:VEVENT\r\nUID:allday@x\r\nDTSTART;VALUE=DATE:20261014\r\nDTEND;VALUE=DATE:20261015\r\n"
            "SUMMARY:Holiday https://meet.google.com/aaa-aaaa-aaa\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:tz@x\r\nDTSTART;TZID=Europe/Moscow:20261014T180000\r\n"
            "DTEND;TZID=Europe/Moscow:20261014T190000\r\nSUMMARY:Телемост\r\n"
            "LOCATION:https://telemost.yandex.ru/j/111\r\nEND:VEVENT",
        )
        events, name = calendars.parse(data, now=NOW)
        self.assertEqual(name, "me@example.com")
        standups = [e for e in events if e["uid"] == "standup@x"]
        starts = [e["start"][:16] for e in standups]
        self.assertIn("2026-10-14T10:00", starts)
        self.assertIn("2026-10-15T12:00", starts)  # moved
        self.assertNotIn("2026-10-15T10:00", starts)
        self.assertNotIn("2026-10-16T10:00", starts)  # deleted
        self.assertEqual(len({e["key"] for e in standups}), len(standups))  # a key per occurrence
        self.assertFalse(any(e["uid"] == "allday@x" for e in events))  # all-day: nothing to join
        tz = next(e for e in events if e["uid"] == "tz@x")
        self.assertEqual(tz["start"], "2026-10-14T15:00:00+00:00")  # 18:00 Moscow
        self.assertEqual(tz["platform"], "telemost")
        oleg = next(e for e in standups)["attendees"][0]
        self.assertEqual(oleg, {"name": "Oleg", "email": "oleg@example.com", "status": "ACCEPTED"})

        # who goes
        calendars._cache["c1"] = {"name": name, "events": events}
        mine = calendars.my_emails()
        self.assertEqual(mine, {"me@example.com"})  # from the calendar's name (Google)
        by = {e["uid"]: e for e in events}
        self.assertEqual(calendars.decide(by["standup@x"], {}, mine), (True, "auto"))
        self.assertEqual(calendars.decide(by["zoom@x"], {}, mine), (False, "you declined"))
        self.assertEqual(calendars.decide(by["gone@x"], {}, mine), (False, "cancelled"))
        # an explicit choice wins over the declined invitation
        self.assertEqual(calendars.decide(by["zoom@x"], {"events": {by["zoom@x"]["key"]: "join"}}, mine)[0], True)
        self.assertEqual(calendars.decide(by["standup@x"], {"events": {by["standup@x"]["key"]: "skip"}}, mine)[0],
                         False)
        config.save_settings({"bot_auto_join": "accepted"})
        self.assertTrue(calendars.decide(by["standup@x"], {}, mine)[0])   # accepted
        self.assertTrue(calendars.decide(by["tz@x"], {}, mine)[0])        # no guests: my own event
        config.save_settings({"bot_auto_join": "off"})
        self.assertEqual(calendars.decide(by["standup@x"], {}, mine), (False, "auto-join is off"))

    def test_not_a_calendar(self):
        with mock.patch.object(calendars.HTTP, "stream") as st:
            resp = st.return_value.__enter__.return_value
            resp.status_code = 200
            resp.iter_bytes.return_value = [b"<html>login</html>"]
            with self.assertRaisesRegex(ValueError, "not an iCal calendar"):
                calendars.add("https://calendar.google.com/x")
            resp.status_code = 404
            with self.assertRaisesRegex(ValueError, "refused the link"):
                calendars.add("webcal://example.com/cal.ics")
        with self.assertRaisesRegex(ValueError, "iCal address"):
            calendars.add("not a link")

    def test_add_masks_the_secret(self):
        data = ics(event("a@x", "20261014T100000Z", "20261014T110000Z", "Call",
                         "LOCATION:https://meet.google.com/abc-defg-hij"))
        with mock.patch.object(calendars, "_fetch", return_value=calendars.parse(data, now=NOW)):
            r = calendars.add("webcal://calendar.example.com/private-secret123/basic.ics")
        self.assertEqual(r["events"], 1)
        pub = calendars.public_list()[0]
        self.assertNotIn("secret123", json.dumps(pub))
        self.assertNotIn("calendars", config.load_settings().public())
        self.assertTrue(config.load_settings().calendars[0]["url"].startswith("https://"))
        # the settings form cannot overwrite the list
        config.save_settings({"calendars": []})
        self.assertEqual(len(calendars.calendars()), 1)
        self.assertTrue(calendars.remove(r["id"] if "id" in r else calendars.calendars()[0]["id"]))
        self.assertEqual(calendars.calendars(), [])


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class Sessions(unittest.TestCase):
    def setUp(self):
        reset_state(self)
        self.enqueued = []
        self.p = mock.patch.object(pipeline, "enqueue", lambda mid, kind="full": self.enqueued.append((mid, kind)))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_token(self):
        os.makedirs(os.path.dirname(bot.KEY_FILE), exist_ok=True)
        bot.write_token()
        with open(bot.KEY_FILE) as fh:
            tok = fh.read()
        self.assertTrue(bot.token_ok(f"Bearer {tok}"))
        self.assertFalse(bot.token_ok("Bearer nope"))
        self.assertFalse(bot.token_ok(tok))

    def test_full_call(self):
        end = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        ev = {"key": "k1", "title": "Pilot sync", "url": "https://meet.google.com/abc-defg-hij",
              "start": datetime.now(timezone.utc).isoformat(), "end": end,
              "attendees": [{"name": "Oleg", "email": "oleg@x.com", "status": "ACCEPTED"},
                            {"name": "", "email": "sarah@x.com", "status": ""}]}
        x = bot.create(ev["url"], ev["title"], ev)
        self.assertEqual(bot.create(ev["url"], "", ev)["id"], x["id"])  # no second bot in the same call
        self.assertEqual(x["platform"], "meet")
        self.assertEqual(x["attendees"], ["Oleg", "sarah"])

        work = bot.claim(2, [])
        self.assertEqual([j["id"] for j in work["start"]], [x["id"]])
        job = work["start"][0]
        self.assertEqual(job["name"], "Uno Notetaker")
        self.assertIn("record", job["chat_message"])
        self.assertGreater(job["max_s"], 30 * 60)
        self.assertEqual(bot.claim(2, [x["id"]])["start"], [])  # handed out once

        self.assertEqual(bot.report(x["id"], "lobby"), {"stop": False, "meeting_id": ""})
        r = bot.report(x["id"], "recording")
        mid = r["meeting_id"]
        m = store.get(mid)
        self.assertEqual((m["source"], m["status"], m["title"]), ("bot", "recording", "Pilot sync"))
        self.assertEqual(m["attendees"], ["Oleg", "sarah"])
        self.assertEqual(bot.report(x["id"], "recording")["meeting_id"], mid)  # same meeting

        self.assertEqual(bot.audio(x["id"], b"OggS1", 0), 5)
        self.assertEqual(bot.audio(x["id"], b"OggS1", 0), 5)  # a retried piece is ignored
        self.assertEqual(bot.audio(x["id"], b"22", 5), 7)

        bot.stop(x["id"])  # the person pressed "Remove the bot"
        self.assertEqual(bot.report(x["id"], "recording")["stop"], True)
        self.assertEqual(bot.claim(2, [x["id"]])["stop"], [x["id"]])

        bot.finish(x["id"], "done", "Removed from the call in Notetaker")
        self.assertEqual(bot.get(x["id"])["status"], "done")
        self.assertEqual(store.get(mid)["status"], "queued")
        self.assertEqual(self.enqueued, [(mid, "full")])
        with self.assertRaises(ValueError):
            bot.audio(x["id"], b"late", 7)
        # the same occurrence never gets a second bot by itself
        self.assertEqual(bot.create(ev["url"], "", ev)["id"], x["id"])

    def test_not_admitted_makes_no_meeting(self):
        x = bot.create("https://zoom.us/j/81234567890", "")
        bot.claim(1, [])
        bot.report(x["id"], "lobby")
        bot.finish(x["id"], "not_admitted", "Nobody let the bot in within 15 min")
        self.assertEqual(bot.get(x["id"])["status"], "not_admitted")
        self.assertEqual(store.list_all(), [])

    def test_in_call_but_silent_recorder(self):
        x = bot.create("https://zoom.us/j/81234567890", "")
        bot.claim(1, [])
        mid = bot.report(x["id"], "recording")["meeting_id"]
        bot.finish(x["id"], "done")
        self.assertEqual(bot.get(x["id"])["status"], "failed")
        self.assertEqual(store.get(mid)["status"], "error")

    def test_bot_container_restarts(self):
        a = bot.create("https://meet.google.com/aaa-aaaa-aaa", "")
        b = bot.create("https://meet.google.com/bbb-bbbb-bbb", "")
        bot.claim(2, [])
        mid = bot.report(b["id"], "recording")["meeting_id"]
        bot.audio(b["id"], b"OggS", 0)
        later = __import__("time").time() + 60
        with mock.patch("time.time", return_value=later):
            out = bot.claim(2, [])  # a new container: runs nothing
        # a: was only opening the call → back in the queue, handed out again
        self.assertEqual(out["start"], [])
        self.assertEqual(bot.get(a["id"])["status"], "queued")
        # b: was recording → notes from what was saved
        self.assertEqual(bot.get(b["id"])["status"], "done")
        self.assertEqual(self.enqueued, [(mid, "full")])
        self.assertEqual([j["id"] for j in bot.claim(2, [])["start"]], [a["id"]])

    def test_cancel_before_join(self):
        x = bot.create("https://meet.google.com/aaa-aaaa-aaa", "")
        self.assertEqual(bot.stop(x["id"])["status"], "cancelled")
        self.assertEqual(bot.claim(2, [])["start"], [])

    def test_calendar_loop_sends_once(self):
        now = datetime.now(timezone.utc)
        data = ics(event("s@x", (now - timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ"),
                         (now + timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ"), "Stand-up",
                         "LOCATION:https://meet.google.com/abc-defg-hij"),
                   event("later@x", (now + timedelta(hours=3)).strftime("%Y%m%dT%H%M%SZ"),
                         (now + timedelta(hours=4)).strftime("%Y%m%dT%H%M%SZ"), "Later",
                         "LOCATION:https://zoom.us/j/81234567890"))
        events, name = calendars.parse(data)
        config.save_settings({"calendars": [{"id": "c1", "url": "https://x/c.ics", "name": ""}]}, internal=True)
        calendars._cache["c1"] = {"events": events, "name": name, "tried": __import__("time").time()}
        due = [e for e in calendars.upcoming(include_past_hours=0) if e["join"]]
        self.assertEqual(len(due), 2)
        for e in due:
            if datetime.fromisoformat(e["start"]) - timedelta(minutes=1) <= now:
                bot.due_from_calendar(e)
                bot.due_from_calendar(e)
        self.assertEqual([x["title"] for x in bot.sessions()], ["Stand-up"])
        ov = bot.overview()
        up = {e["title"]: e for e in ov["upcoming"]}
        self.assertEqual(up["Stand-up"]["session"]["status"], "queued")
        self.assertIsNone(up["Later"]["session"])


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class InvitedNames(unittest.TestCase):
    def test_prompt_hint(self):
        self.assertIn("Oleg, Sarah", pipeline._invited(["Oleg", "Sarah"]))
        self.assertEqual(pipeline._invited([]), "")


class ZoomWebClient(unittest.TestCase):
    """bot/notetaker_bot/platforms.py — stdlib only, no Playwright needed."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bot"))
        try:
            from notetaker_bot import platforms
        except ImportError as exc:  # Playwright is only in the bot image
            self.skipTest(str(exc))
        self.platforms = platforms

    def test_urls(self):
        z = self.platforms.ZOOM
        self.assertEqual(z.url("https://us02web.zoom.us/j/81234567890?pwd=AbC.1&from=addon"),
                         "https://app.zoom.us/wc/join/81234567890?pwd=AbC.1")
        self.assertEqual(z.url("https://acme.zoomgov.com/j/1612345678"), "https://acme.zoomgov.com/wc/join/1612345678")
        self.assertEqual(z.url("https://zoom.us/my/vanity"), "https://zoom.us/my/vanity")
        self.assertTrue(self.platforms.MEET.url("https://meet.google.com/abc-defg-hij").endswith("?hl=en"))
        self.assertIs(self.platforms.spec_for("kakao"), self.platforms.GENERIC)


if __name__ == "__main__":
    unittest.main()
