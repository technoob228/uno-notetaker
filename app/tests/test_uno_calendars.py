"""0.7: calendars from the person's Uno account (Google, Outlook & Teams) without an assistant.

    python3 -m unittest discover -s app/tests       (from the repo root)

Uno Work is faked at the SDK level (uno_app.Client): GET /v1/calendars, the
feed per provider, the assistant link.
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

TMP = tempfile.mkdtemp(prefix="nt-unocal-")
os.environ.setdefault("MEETINGS_DIR", os.path.join(TMP, "Meetings"))
os.environ.setdefault("STATE_DIR", os.path.join(TMP, "state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("icalendar", "recurring_ical_events", "httpx"))

if HAS_DEPS:
    from notetaker import assistant, bot, calendars, config, store, uno_app  # noqa: E402

STATE = os.path.join(TMP, "state-u")
MEETINGS = os.path.join(TMP, "Meetings-u")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def row(provider, connected=True, available=True, account="", reconnect=False):
    return {"provider": provider, "name": {"gmail": "Google Calendar", "microsoft": "Outlook & Teams"}[provider],
            "available": available, "connected": connected, "account": account, "needsReconnect": reconnect}


class FakeWork:
    """What Uno Work answers on the App API."""

    def __init__(self):
        self.list = {"calendars": [row("gmail", account="misha@x.com"),
                                   row("microsoft", account="misha@corp.com")]}
        self.list_error = None
        self.feeds = {"gmail": {"account": "misha@x.com", "events": []},
                      "microsoft": {"account": "misha@corp.com", "events": []}}
        self.feed_errors: dict = {}
        self.calls: list = []        # (route, provider)
        self.link_error = uno_app.UnoAppError(409, "calls_off", "Join my calls is off")
        self.link = {"enabled": True, "assistant": {"projectId": "p", "name": "Ada"},
                     "owner": {"name": "Misha", "email": "misha@x.com"}, "botName": "Ada (Uno notes)",
                     "rule": "mine", "choices": {}}

    def client(self, *a, **k):
        work = self

        class Client:
            def calendars(self):
                work.calls.append(("list", None))
                if work.list_error:
                    raise work.list_error
                return work.list

            def calendar_events(self, start=None, end=None, **kw):
                provider = kw.get("provider")
                work.calls.append(("events", provider))
                err = work.feed_errors.get(provider or "gmail")
                if err:
                    raise err
                return work.feeds[provider or "gmail"]

            def assistant_link(self):
                if work.link_error:
                    raise work.link_error
                return work.link

            def assistant_event(self, ev):
                return {"ok": True}

        return Client()


def gevent(gid, start, minutes=30, **extra):
    ev = {"id": gid, "status": "confirmed", "title": extra.pop("title", gid), "start": iso(start),
          "end": iso(start + timedelta(minutes=minutes)),
          "organizer": {"email": "oleg@x.com"},
          "attendees": [{"email": "oleg@x.com", "response": "accepted", "organizer": True},
                        {"email": "misha@x.com", "response": "accepted", "self": True}],
          "conference_url": f"https://meet.google.com/{extra.pop('code', 'abc-defg-hij')}"}
    ev.update(extra)
    return ev


TEAMS_GOOGLE = ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_NjQ5ZTdk%40thread.v2/0"
                "?context=%7b%22Tid%22%3a%22x%22%7d")
TEAMS_GRAPH = ("https://teams.microsoft.com/l/meetup-join/19%3AMeeting_NjQ5ZTdk%40thread.v2/0"
               "?context=%7B%22Tid%22%3A%22x%22%7D")


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class UnoCalendars(unittest.TestCase):
    def setUp(self):
        for mod, name, value in ((config, "STATE_DIR", STATE), (bot, "STATE_DIR", STATE),
                                 (calendars, "STATE_DIR", STATE), (assistant, "STATE_DIR", STATE),
                                 (config, "MEETINGS_DIR", MEETINGS), (store, "MEETINGS_DIR", MEETINGS)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.rmtree(MEETINGS, ignore_errors=True)
        os.makedirs(STATE)

        def reset():
            assistant._link.clear()
            assistant._link.update(enabled=False, checked=0.0, supported=None, error="")
            calendars._cache.clear()
            calendars._uno.clear()
            calendars._uno.update(checked=0.0, supported=None, list=[], error="")
        reset()
        self.addCleanup(reset)  # other test modules run with no Uno Work
        self.work = FakeWork()
        self._patch(uno_app, "Client", self.work.client)
        self._patch(assistant, "app_api_config", lambda: {"url": "http://w", "token": "t"})
        self._patch(config, "app_api_config", lambda: {"url": "http://w", "token": "t"})
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def _patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def sync(self):
        calendars.refresh_uno_list(force=True)
        calendars._uno["checked"] = 0.0  # the next sync asks again
        calendars.refresh(force=True)

    def ids(self):
        return [c["id"] for c in calendars.calendars()]

    def by_title(self):
        return {e["title"]: e for e in calendars.upcoming()}

    def states(self):
        return {c["provider"]: c["state"] for c in calendars.uno_status()["calendars"]}

    # ---- the list → virtual calendars ----

    def test_list_makes_virtual_calendars_without_an_assistant(self):
        t = self.now + timedelta(hours=2)
        self.work.feeds["gmail"]["events"] = [gevent("g1", t, title="Google sync")]
        self.work.feeds["microsoft"]["events"] = [gevent("AAMk1", t + timedelta(hours=1), title="Teams review",
                                                         conference_url=TEAMS_GRAPH)]
        self.sync()
        self.assertFalse(assistant.active())
        self.assertEqual(self.ids(), ["uno", "uno-microsoft"])
        names = {c["id"]: c["name"] for c in calendars.public_list()}
        self.assertEqual(names, {"uno": "Google Calendar · from your Uno account",
                                 "uno-microsoft": "Outlook & Teams · from your Uno account"})
        # Each provider fetched with its parameter.
        self.assertIn(("events", "gmail"), self.work.calls)
        self.assertIn(("events", "microsoft"), self.work.calls)
        by = self.by_title()
        self.assertTrue(by["Google sync"]["join"])
        self.assertEqual((by["Teams review"]["platform"], by["Teams review"]["join"]), ("teams", True))
        self.assertEqual(self.states(), {"gmail": "connected", "microsoft": "connected"})
        # The accounts from the list are the person's addresses.
        self.assertTrue({"misha@x.com", "misha@corp.com"} <= calendars.my_emails())
        # Standalone: the bot is "Uno Notetaker", the rule is the app's own, nothing goes to an assistant.
        self.assertEqual(bot.bot_identity()[0], "Uno Notetaker")
        self.assertEqual(calendars.auto_join_mode(), "all")
        self.assertIsNone(assistant.emit("calls.upcoming", {"calls": []}))

    def test_only_connected_ones_are_fetched(self):
        self.work.list = {"calendars": [row("gmail", account="misha@x.com"),
                                        row("microsoft", connected=False)]}
        self.sync()
        self.assertEqual(self.ids(), ["uno"])
        self.assertNotIn(("events", "microsoft"), self.work.calls)
        self.assertEqual(self.states(), {"gmail": "connected", "microsoft": "not_connected"})
        # An older console: no microsoft row at all → "Not available yet".
        self.work.list = {"calendars": [row("gmail", account="misha@x.com")]}
        self.sync()
        self.assertEqual(self.states()["microsoft"], "unavailable")
        self.work.list = {"calendars": [row("gmail"), row("microsoft", available=False, connected=False)]}
        self.sync()
        self.assertEqual(self.states()["microsoft"], "unavailable")

    def test_microsoft_keys_never_collide_and_stay_when_moved(self):
        t = self.now + timedelta(hours=3)
        same_id = "evt-1"
        g = calendars.from_feed({"events": [gevent(same_id, t)]}, "gmail")[0]
        m = calendars.from_feed({"events": [gevent(same_id, t)]}, "microsoft")[0]
        self.assertNotEqual(g["key"], m["key"])
        self.assertRegex(m["key"], r"^[a-f0-9]{12}$")  # Work's per-meeting choices accept it
        # The old Google key is kept as it was in 0.5 (choices saved then still apply).
        self.assertEqual(calendars.from_feed({"events": [gevent(same_id, t)]})[0]["key"], g["key"])
        moved = calendars.from_feed({"events": [gevent(same_id, t + timedelta(hours=2))]}, "microsoft")[0]
        self.assertEqual(moved["key"], m["key"])

    # ---- older Uno Work ----

    def test_older_work_404_behaves_as_before(self):
        self.work.list_error = uno_app.UnoAppError(404, "not_found", "No GET /v1/calendars")
        t = self.now + timedelta(hours=1)
        self.work.feeds["gmail"]["events"] = [gevent("g1", t, title="Old way")]
        self.sync()
        self.assertFalse(calendars.uno_status()["supported"])
        self.assertEqual(self.ids(), [])  # no assistant → only iCal links, as in 0.4
        self.assertNotIn(("events", "gmail"), self.work.calls)
        # With an assistant's Join my calls → Google through Uno, called without a provider (0.5).
        self.work.link_error = None
        assistant.refresh_link(force=True)
        self.sync()
        self.assertEqual(self.ids(), ["uno"])
        self.assertIn(("events", None), self.work.calls)
        self.assertNotIn(("events", "gmail"), self.work.calls)
        self.assertEqual(self.by_title()["Old way"]["calendar"], "uno")
        self.assertEqual(bot.bot_identity()[0], "Ada (Uno notes)")

    def test_work_unreachable_keeps_the_last_list(self):
        self.sync()
        self.work.list_error = uno_app.UnoAppError(0, "unreachable", "down")
        self.sync()
        self.assertEqual(self.ids(), ["uno", "uno-microsoft"])

    # ---- not connected / sign in again ----

    def test_424_is_a_status_not_an_error(self):
        t = self.now + timedelta(hours=1)
        self.work.feeds["microsoft"]["events"] = [gevent("m1", t, title="Kept", conference_url=TEAMS_GRAPH)]
        self.sync()
        self.assertIn("Kept", self.by_title())
        self.work.feed_errors["microsoft"] = uno_app.UnoAppError(424, "microsoft_reconnect", "reconnect")
        with self.assertNoLogs("notetaker.calendar", level="WARNING"):
            self.sync()
            self.sync()
        st = {c["provider"]: c for c in calendars.uno_status()["calendars"]}["microsoft"]
        self.assertEqual(st["state"], "reconnect")
        self.assertIn("sign in again", st["error"])
        self.assertIn("Kept", self.by_title())  # what was known still counts
        self.assertFalse(calendars.loaded())     # …and nothing is called off for this calendar
        self.work.feed_errors["microsoft"] = uno_app.UnoAppError(424, "microsoft_not_connected", "no")
        self.sync()
        self.assertEqual(self.states()["microsoft"], "not_connected")
        self.work.feed_errors["gmail"] = uno_app.UnoAppError(424, "google_reconnect", "reconnect")
        self.sync()
        self.assertEqual(self.states()["gmail"], "reconnect")

    def test_needs_reconnect_from_the_list_is_not_fetched(self):
        self.work.list = {"calendars": [row("gmail", account="misha@x.com", reconnect=True)]}
        self.sync()
        self.assertEqual(self.states()["gmail"], "reconnect")
        self.assertNotIn(("events", "gmail"), self.work.calls)

    def test_meeting_deleted_in_outlook_calls_the_bot_off(self):
        """Graph's calendarView leaves a deleted meeting out (no "cancelled"): missing from a
        complete feed = gone; missing from a feed cut short proves nothing."""
        self.work.list = {"calendars": [row("microsoft", account="misha@corp.com")]}
        t = self.now + timedelta(seconds=30)
        keep = gevent("AAMk-keep", t, title="Stays", code="kkk-llll-mmm")
        gone = gevent("AAMk-gone", t, title="Deleted", conference_url=TEAMS_GRAPH)
        self.work.feeds["microsoft"] = {"account": "misha@corp.com", "complete": True, "events": [keep, gone]}
        self.sync()
        for title in ("Stays", "Deleted"):
            bot.due_from_calendar(self.by_title()[title])
        self.assertEqual({x["title"]: x["status"] for x in bot.sessions()}, {"Stays": "queued", "Deleted": "queued"})
        # Cut short (a page of the Graph failed): nothing is called off.
        self.work.feeds["microsoft"] = {"account": "misha@corp.com", "complete": False, "events": [keep]}
        self.sync()
        self.assertFalse(calendars.loaded())
        self.assertEqual(bot.reconcile(), 0)
        # The whole feed, without it: the meeting was deleted — the bot doesn't go.
        self.work.feeds["microsoft"] = {"account": "misha@corp.com", "complete": True, "events": [keep]}
        self.sync()
        self.assertTrue(calendars.loaded())
        self.assertEqual(bot.reconcile(), 1)
        s = {x["title"]: x for x in bot.sessions()}
        self.assertEqual((s["Deleted"]["status"], s["Deleted"]["reason"]), ("cancelled", "cancelled"))
        self.assertEqual(s["Stays"]["status"], "queued")
        # A Zoom link in the invitation's plain-text body is found.
        body = gevent("AAMk-zoom", t + timedelta(hours=1), title="Zoom via Outlook", conference_url="",
                      description="Hi all,\nJoin Zoom Meeting\nhttps://us06web.zoom.us/j/81234567890?pwd=Qx1\n")
        self.work.feeds["microsoft"]["events"].append(body)
        self.sync()
        z = self.by_title()["Zoom via Outlook"]
        self.assertEqual((z["platform"], z["url"]), ("zoom", "https://us06web.zoom.us/j/81234567890?pwd=Qx1"))

    # ---- the setting ----

    def test_setting_off(self):
        self.sync()
        config.save_settings({"uno_calendars": False})
        self.assertEqual(self.ids(), [])
        self.assertFalse(calendars.uno_status()["enabled"])
        # An assistant's Join my calls (switched on in Uno Work) still uses them.
        self.work.link_error = None
        assistant.refresh_link(force=True)
        self.assertEqual(self.ids(), ["uno", "uno-microsoft"])
        config.save_settings({"uno_calendars": True})
        self.assertTrue(config.load_settings().uno_calendars)

    def test_assistant_mode_uses_both_calendars(self):
        self.work.link_error = None
        assistant.refresh_link(force=True)
        t = self.now + timedelta(seconds=30)
        self.work.feeds["microsoft"]["events"] = [gevent("m1", t, title="Outlook call", conference_url=TEAMS_GRAPH,
                                                         attendees=[{"email": "misha@corp.com", "self": True,
                                                                     "response": "accepted"},
                                                                    {"email": "boss@corp.com", "response": "accepted"}])]
        self.sync()
        ev = self.by_title()["Outlook call"]
        self.assertTrue(ev["join"])  # rule "mine" of the assistant
        bot.due_from_calendar(ev)
        self.assertEqual(bot.claim(1, [])["start"][0]["name"], "Ada (Uno notes)")

    # ---- one call, one bot ----

    def test_google_through_uno_and_its_ical_link_are_one_meeting(self):
        t = (self.now + timedelta(minutes=1)).replace(second=0)
        self.work.list = {"calendars": [row("gmail", account="misha@x.com")]}
        self.work.feeds["gmail"]["events"] = [gevent("g1_20261029", t, title="Weekly", ical_uid="weekly@google.com")]
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "X-WR-CALNAME:misha@x.com", "BEGIN:VEVENT",
            "UID:weekly@google.com", f"DTSTART:{t.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{(t + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}", "SUMMARY:Weekly",
            "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij", "END:VEVENT", "END:VCALENDAR", ""]).encode()
        config.save_settings({"calendars": [{"id": "ical1", "url": "https://calendar.google.com/x.ics", "name": ""}]},
                             internal=True)
        with mock.patch.object(calendars, "_fetch", side_effect=lambda cal: calendars.parse(ics)
                               if cal["id"] == "ical1" else calendars._fetch_uno(cal)):
            self.sync()
        self.assertEqual(self.ids(), ["uno", "ical1"])
        rows = [e for e in calendars.upcoming() if e["title"] == "Weekly"]
        self.assertEqual(len(rows), 1)
        one = rows[0]
        self.assertEqual(one["calendar"], "uno")  # the row with Google's stable id
        self.assertEqual(len(one["keys"]), 2)
        self.assertEqual(one["calendars"], ["uno", "ical1"])
        # Turning it off on that one row turns off the meeting in both calendars.
        calendars.set_choice(one["key"], "skip")
        self.assertFalse(self.by_title()["Weekly"]["join"])
        calendars.set_choice(one["key"], None)
        # A choice saved earlier on the iCal row (before Uno's calendar appeared) still counts.
        calendars.set_choice(one["keys"][1], None)
        d = calendars._load_overrides()
        d.setdefault("events", {})[one["keys"][1]] = "skip"
        calendars._save_overrides(d)
        self.assertFalse(self.by_title()["Weekly"]["join"])
        d["events"].pop(one["keys"][1])
        calendars._save_overrides(d)
        # The bot goes once, whichever row the calendar loop sees.
        bot.due_from_calendar(self.by_title()["Weekly"])
        ical_row = next(e for e in calendars.parse(ics)[0])
        bot.due_from_calendar(ical_row)
        self.assertEqual(len(bot.sessions()), 1)
        # A session made for the iCal row is matched to the merged row (Calls page, reconcile).
        x = bot.sessions()[0]
        self.assertEqual(bot.overview()["upcoming"][0]["session"]["id"], x["id"])

    def test_teams_invitation_in_google_and_outlook_sends_one_bot(self):
        t = (self.now + timedelta(seconds=30)).replace(microsecond=0)
        self.work.feeds["gmail"]["events"] = [gevent("g-teams", t, title="Vendor call", conference_url="",
                                                     description=f"Join on Teams: <{TEAMS_GOOGLE}>")]
        self.work.feeds["microsoft"]["events"] = [gevent("AAMkTeams", t, title="Vendor call",
                                                         conference_url=TEAMS_GRAPH, ical_uid="040000008200E0")]
        self.sync()
        self.assertEqual(calendars.canonical(TEAMS_GOOGLE), calendars.canonical(TEAMS_GRAPH))
        self.assertEqual(calendars.canonical(TEAMS_GOOGLE), "teams:NjQ5ZTdk")
        rows = [e for e in calendars.upcoming() if e["title"] == "Vendor call"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calendars"], ["uno", "uno-microsoft"])
        # Even if both rows reached the bot separately, one bot.
        g = calendars.from_feed(self.work.feeds["gmail"], "gmail")[0]
        m = calendars.from_feed(self.work.feeds["microsoft"], "microsoft")[0]
        bot.due_from_calendar(g)
        bot.due_from_calendar(m)
        self.assertEqual(len(bot.sessions()), 1)
        # Another meeting's thread id is another call.
        self.assertNotEqual(calendars.canonical(TEAMS_GOOGLE), calendars.canonical(TEAMS_GOOGLE.replace("NjQ5", "Xy12")))
        self.assertTrue(calendars.canonical("https://teams.live.com/meet/9381234567890?p=abc")
                        .startswith("teams:9381234567890"))

    def test_different_meetings_with_the_same_link_stay_apart(self):
        """A personal Zoom room used for two meetings at different times: two rows."""
        t = self.now + timedelta(hours=2)
        zoom = "https://us02web.zoom.us/j/123456789?pwd=abc"
        self.work.feeds["gmail"]["events"] = [gevent("a", t, title="A", conference_url=zoom),
                                              gevent("b", t + timedelta(hours=1), title="B", conference_url=zoom)]
        self.sync()
        self.assertEqual(sorted(self.by_title()), ["A", "B"])

    def test_status_for_the_ui(self):
        self.sync()
        st = calendars.uno_status()
        self.assertTrue(st["work"] and st["supported"] and st["enabled"])
        self.assertEqual([(c["provider"], c["account"]) for c in st["calendars"]],
                         [("gmail", "misha@x.com"), ("microsoft", "misha@corp.com")])
        json.dumps(st)  # goes to the browser as is


if __name__ == "__main__":
    unittest.main()
