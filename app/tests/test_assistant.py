"""0.5: an assistant in Uno Work sends the notetaker to the person's calls.

    python3 -m unittest discover -s app/tests       (from the repo root)

Uno Work is faked at the SDK level (uno_app.Client): the link, the Google
calendar feed, the events the app sends back.
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

TMP = tempfile.mkdtemp(prefix="nt-assistant-")
os.environ.setdefault("MEETINGS_DIR", os.path.join(TMP, "Meetings"))
os.environ.setdefault("STATE_DIR", os.path.join(TMP, "state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

HAS_DEPS = all(importlib.util.find_spec(m) for m in ("icalendar", "recurring_ical_events", "httpx"))

if HAS_DEPS:
    from notetaker import (actions, assistant, bot, calendars, config, deliver, pipeline,  # noqa: E402
                           store, uno_app)

STATE = os.path.join(TMP, "state-a")
MEETINGS = os.path.join(TMP, "Meetings-a")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class FakeWork:
    """What Uno Work answers on the App API."""

    def __init__(self):
        self.link = {"enabled": True, "assistant": {"projectId": "assistant-ada", "name": "Ada"},
                     "owner": {"name": "Misha", "email": "misha@x.com"}, "botName": "Ada (Uno notes)",
                     "rule": "mine", "choices": {}}
        self.feed = {"account": "misha@x.com", "events": []}
        self.events: list[dict] = []
        self.down = False
        self.link_error = None

    def client(self, *a, **k):
        work = self

        class Client:
            def assistant_link(self):
                if work.link_error:
                    raise work.link_error
                if work.down:
                    raise uno_app.UnoAppError(0, "unreachable", "down")
                return work.link

            def calendar_events(self, start=None, end=None):
                return work.feed

            def assistant_event(self, ev):
                if work.down:
                    raise uno_app.UnoAppError(0, "unreachable", "down")
                work.events.append(json.loads(json.dumps(ev)))
                return {"ok": True}

            def notify(self, *a, **k):
                work.events.append({"kind": "notify"})
                return {"ok": True}

        return Client()


def gevent(gid, start, minutes=30, **extra):
    ev = {"id": gid, "status": "confirmed", "title": extra.pop("title", gid), "start": iso(start),
          "end": iso(start + timedelta(minutes=minutes)),
          "organizer": {"email": "oleg@x.com"},
          "attendees": [{"email": "oleg@x.com", "response": "accepted", "organizer": True},
                        {"email": "misha@x.com", "response": "needsAction", "self": True}],
          "conference_url": f"https://meet.google.com/{extra.pop('code', 'abc-defg-hij')}"}
    ev.update(extra)
    return ev


@unittest.skipUnless(HAS_DEPS, "needs icalendar, recurring-ical-events, httpx")
class AssistantCalls(unittest.TestCase):
    def setUp(self):
        for mod, name, value in ((config, "STATE_DIR", STATE), (bot, "STATE_DIR", STATE),
                                 (calendars, "STATE_DIR", STATE), (assistant, "STATE_DIR", STATE),
                                 (config, "MEETINGS_DIR", MEETINGS), (store, "MEETINGS_DIR", MEETINGS)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.rmtree(MEETINGS, ignore_errors=True)
        os.makedirs(STATE)
        store._index.clear()
        calendars._cache.clear()
        def reset_link():
            assistant._link.clear()
            assistant._link.update(enabled=False, checked=0.0, supported=None, error="")
            calendars._cache.clear()
        reset_link()
        self.addCleanup(reset_link)  # other test modules run with no assistant
        self.work = FakeWork()
        self._patch(uno_app, "Client", self.work.client)
        self._patch(assistant, "app_api_config", lambda: {"url": "http://w", "token": "t"})
        self._patch(config, "app_api_config", lambda: {"url": "http://w", "token": "t"})
        self._patch(pipeline, "enqueue", lambda mid, kind="full": None)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def _patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def link_on(self):
        assistant.refresh_link(force=True)
        self.assertTrue(assistant.active())

    def load_feed(self, *events):
        self.work.feed = {"account": "misha@x.com", "events": list(events)}
        calendars.refresh(force=True)

    def by_title(self):
        return {e["title"]: e for e in calendars.upcoming()}

    # ---- the link ----

    def test_link_and_identity(self):
        self.assertFalse(assistant.active())
        self.link_on()
        self.assertEqual(assistant.bot_name(), "Ada (Uno notes)")
        name, hello = bot.bot_identity()
        self.assertEqual(name, "Ada (Uno notes)")
        self.assertIn("take notes for Misha", hello)
        self.assertIn("uno", [c["id"] for c in calendars.calendars()])
        # Uno Work restarting: the last link is kept.
        self.work.down = True
        assistant.refresh_link(force=True)
        self.assertTrue(assistant.active())
        # Turned off in Work.
        self.work.down = False
        self.work.link_error = uno_app.UnoAppError(409, "calls_off", "off")
        assistant.refresh_link(force=True)
        self.assertFalse(assistant.active())
        self.assertNotIn("uno", [c["id"] for c in calendars.calendars()])
        # An older Uno Work: no such route — nothing changes for the app.
        self.work.link_error = uno_app.UnoAppError(404, "not_found", "No GET /v1/assistant/link")
        assistant.refresh_link(force=True)
        self.assertFalse(assistant.active())
        self.assertEqual(bot.bot_identity()[0], "Uno Notetaker")

    def test_uno_calendar_is_not_saved_with_ical_ones(self):
        self.link_on()
        self.assertEqual(config.load_settings().calendars, [])
        self.assertFalse(calendars.remove("uno"))

    # ---- who goes ----

    def test_rule_mine(self):
        self.link_on()
        t = self.now + timedelta(hours=2)
        self.load_feed(
            gevent("invited", t, title="Invited"),
            gevent("organizer", t, title="Mine", organizer={"email": "misha@x.com", "self": True},
                   self_response="organizer",
                   attendees=[{"email": "misha@x.com", "self": True, "organizer": True, "response": "accepted"},
                              {"email": "client@y.com", "response": "needsAction"}], code="aaa-bbbb-ccc"),
            gevent("solo", t, title="Focus", organizer={"email": "misha@x.com", "self": True},
                   attendees=[], code="ddd-eeee-fff"),
            gevent("declined", t, title="Declined", code="ggg-hhhh-iii",
                   attendees=[{"email": "oleg@x.com", "response": "accepted"},
                              {"email": "misha@x.com", "self": True, "response": "declined"}]),
            gevent("other", t, title="Not mine", code="jjj-kkkk-lll",
                   attendees=[{"email": "oleg@x.com"}, {"email": "sarah@x.com"}]),
            gevent("nolink", t, title="No link", conference_url="", description="in the office"),
            {"id": "gone", "status": "cancelled", "start": iso(t), "end": iso(t + timedelta(minutes=30))},
        )
        by = self.by_title()
        self.assertEqual((by["Invited"]["join"], by["Mine"]["join"]), (True, True))
        self.assertEqual(by["Focus"]["why"], "nobody else is invited")
        self.assertEqual(by["Declined"]["why"], "you declined")
        self.assertEqual(by["Not mine"]["why"], "you're not in this meeting")
        self.assertEqual(by["No link"]["why"], "no call link")
        self.assertEqual(by[""]["why"], "cancelled")
        # A choice pressed in Uno Work ("don't go to this one") wins; an older one doesn't.
        key = by["Invited"]["key"]
        self.assertEqual(calendars.apply_work_choices({key: {"choice": "skip", "at": 100}}), 1)
        self.assertFalse(self.by_title()["Invited"]["join"])
        self.assertEqual(calendars.apply_work_choices({key: {"choice": "skip", "at": 100}}), 0)
        calendars.apply_work_choices({key: {"choice": "", "at": 101}})
        self.assertTrue(self.by_title()["Invited"]["join"])

    def test_zoom_link_from_the_description_and_stable_key(self):
        self.link_on()
        t = self.now + timedelta(hours=1)
        ev = gevent("z1", t, title="Client", conference_url="",
                    description="Join Zoom https://us02web.zoom.us/j/123456789?pwd=abc<br>")
        self.load_feed(ev)
        a = self.by_title()["Client"]
        self.assertEqual((a["platform"], a["url"]), ("zoom", "https://us02web.zoom.us/j/123456789?pwd=abc"))
        self.load_feed({**ev, "start": iso(t + timedelta(hours=2)), "end": iso(t + timedelta(hours=3))})
        self.assertEqual(self.by_title()["Client"]["key"], a["key"])  # moved: same occurrence

    # ---- the same call once ----

    def test_canonical_links(self):
        c = calendars.canonical
        self.assertEqual(c("https://meet.google.com/abc-defg-hij?authuser=1&hl=en"), c("https://meet.google.com/abc-defg-hij"))
        self.assertEqual(c("https://us02web.zoom.us/j/123456789?pwd=x"), c("https://app.zoom.us/wc/join/123456789"))
        self.assertNotEqual(c("https://meet.google.com/abc-defg-hij"), c("https://meet.google.com/abc-defg-hik"))

    def test_never_twice(self):
        t = self.now - timedelta(minutes=1)
        a = {"key": "k1", "title": "Sync", "url": "https://meet.google.com/abc-defg-hij", "start": iso(t),
             "end": iso(t + timedelta(minutes=30)), "attendees": []}
        x = bot.create(a["url"], a["title"], a)
        # The same meeting in a second calendar (another key, the link with ?authuser=).
        b = {**a, "key": "k2", "url": a["url"] + "?authuser=1"}
        self.assertEqual(bot.create(b["url"], "", b)["id"], x["id"])
        # Sent by hand to the same call meanwhile.
        self.assertEqual(bot.create("https://meet.google.com/abc-defg-hij?hl=en")["id"], x["id"])
        # Even after the call ended, the second calendar entry doesn't send it again.
        bot._update(x["id"], status="done")
        self.assertEqual(bot.create(b["url"], "", b)["id"], x["id"])
        self.assertEqual(len(bot.sessions()), 1)

    # ---- the calendar changes under a call ----

    def test_moved_cancelled_turned_off(self):
        self.link_on()
        t = self.now + timedelta(seconds=30)
        ev = gevent("m1", t, title="Pilot sync")
        self.load_feed(ev, gevent("c1", t, title="Board", code="bbb-cccc-ddd"),
                       gevent("o1", t, title="Retro", code="eee-ffff-ggg"))
        up = self.by_title()
        for title in ("Pilot sync", "Board", "Retro"):
            bot.due_from_calendar(up[title])
        s = {x["title"]: x for x in bot.sessions()}
        self.assertEqual(s["Pilot sync"]["status"], "queued")
        bot._update(s["Board"]["id"], status="lobby")  # the Board bot is already in the lobby

        later = t + timedelta(hours=3)
        self.load_feed({**ev, "start": iso(later), "end": iso(later + timedelta(minutes=30))},
                       {"id": "c1", "status": "cancelled"},
                       gevent("o1", t, title="Retro", code="eee-ffff-ggg"))
        calendars.apply_work_choices({up["Retro"]["key"]: {"choice": "skip", "at": 5}})
        self.assertEqual(bot.reconcile(), 3)
        s = {x["title"]: x for x in bot.sessions()}
        self.assertEqual((s["Pilot sync"]["status"], s["Pilot sync"]["reason"]), ("cancelled", "moved"))
        self.assertEqual((s["Retro"]["status"], s["Retro"]["reason"]), ("cancelled", "turned_off"))
        board = s["Board"]
        self.assertTrue(board["stop"])
        self.assertEqual(board["reason"], "cancelled")
        # The bot in the lobby leaves; that is "cancelled", not "couldn't join".
        bot.finish(board["id"], "failed", "Cancelled while waiting to be let in")
        self.assertEqual(bot.get(board["id"])["status"], "cancelled")

        # At the new time the bot goes to the moved meeting (a new session, same occurrence).
        moved = self.by_title()["Pilot sync"]
        again = bot.create(moved["url"], moved["title"], moved)
        self.assertNotEqual(again["id"], s["Pilot sync"]["id"])
        self.assertEqual(bot.create(moved["url"], moved["title"], moved)["id"], again["id"])

    def test_recording_bot_stays(self):
        self.link_on()
        t = self.now - timedelta(minutes=5)
        ev = gevent("r1", t, title="Live")
        self.load_feed(ev)
        bot.due_from_calendar(self.by_title()["Live"])
        x = bot.sessions()[0]
        bot.report(x["id"], "recording")
        self.load_feed({"id": "r1", "status": "cancelled"})
        self.assertEqual(bot.reconcile(), 0)
        self.assertEqual(bot.get(x["id"])["status"], "recording")

    # ---- events to the assistant ----

    def test_statuses_and_outbox(self):
        self.link_on()
        x = bot.create("https://meet.google.com/abc-defg-hij", "Pilot sync",
                       {"key": "k9", "title": "Pilot sync", "start": iso(self.now), "end": iso(self.now)})
        bot._update(x["id"], status="lobby")
        self.assertEqual(assistant.sync_sessions(bot.sessions()), 1)
        self.assertEqual(assistant.sync_sessions(bot.sessions()), 0)  # once per status
        bot.finish(x["id"], "not_admitted", "Nobody let the bot in within 15 min")
        assistant.sync_sessions(bot.sessions())
        self.work.down = True
        self.assertEqual(assistant.flush(), 0)
        self.assertEqual(len(assistant.pending()), 2)  # kept for later
        self.work.down = False
        self.assertEqual(assistant.flush(), 2)
        self.assertEqual(assistant.pending(), [])
        lobby, failed = self.work.events
        self.assertEqual((lobby["kind"], lobby["status"]), ("call.status", "lobby"))
        self.assertEqual(lobby["text"], "Ada (Uno notes) is waiting to be let into Pilot sync")
        self.assertEqual((failed["status"], failed["reason"]), ("not_admitted", "waiting_room"))
        self.assertEqual(failed["text"], "Couldn't join Pilot sync: waiting room — nobody let Ada (Uno notes) in")
        self.assertEqual(failed["key"], "k9")

    def test_reasons(self):
        r = assistant.reason_of
        self.assertEqual(r("failed", "Google Meet: Check your meeting code"), "meeting_not_found")
        self.assertEqual(r("failed", "Zoom: Sign in to join"), "sign_in_required")
        self.assertEqual(r("not_admitted", "Google Meet: denied your request"), "denied")
        self.assertEqual(r("failed", "The bot never picked this call up — is the bot container running?"), "bot_offline")
        self.assertEqual(r("failed", "weird"), "error")
        self.assertEqual(r("cancelled", "", "moved"), "moved")
        self.assertEqual(r("recording", "x"), "")

    def test_upcoming_only_when_changed(self):
        self.link_on()
        self.load_feed(gevent("u1", self.now + timedelta(hours=2), title="Plan"))
        calls = bot.overview()["upcoming"]
        self.assertTrue(assistant.sync_upcoming(calls))
        self.assertFalse(assistant.sync_upcoming(calls))
        ev = assistant.pending()[0]
        self.assertEqual(ev["kind"], "calls.upcoming")
        self.assertEqual(ev["calls"][0]["title"], "Plan")
        self.assertTrue(ev["calls"][0]["join"])

    def test_notes_go_to_the_assistant_not_the_inbox(self):
        self.link_on()
        x = bot.create("https://meet.google.com/abc-defg-hij", "Pilot sync",
                       {"key": "k5", "title": "Pilot sync", "start": iso(self.now),
                        "end": iso(self.now + timedelta(minutes=30)),
                        "attendees": [{"name": "Oleg", "email": "oleg@x.com", "status": ""}]})
        mid = bot.report(x["id"], "recording")["meeting_id"]
        store.write_text(mid, "notes.md", "\n".join([
            "## Summary", "- Pilot starts in November [03:10]", "- Budget is fine",
            "## Decisions", "- Go with plan B",
            "## Action items", "- [ ] **Oleg**: send the contract (due: Friday) [12:40]", "- [x] **Misha**: book a room",
            "## Open questions", "- Who pays for travel?"]))
        store.update(mid, status="done")
        deliver.after_notes(mid)
        self.assertEqual(store.get(mid)["delivery"]["assistant"], "queued")
        self.assertIsNone(store.get(mid)["delivery"].get("inbox"))
        assistant.flush()
        (ev,) = [e for e in self.work.events if e["kind"] == "meeting.notes"]
        self.assertEqual(ev["id"], f"notes-{mid}")
        self.assertEqual(ev["summary"], ["Pilot starts in November", "Budget is fine"])
        self.assertEqual(ev["decisions"], ["Go with plan B"])
        self.assertEqual(ev["questions"], ["Who pays for travel?"])
        self.assertEqual([(a["owner"], a["text"], a["due"], a["done"]) for a in ev["action_items"]],
                         [("Oleg", "send the contract", "Friday", False), ("Misha", "book a room", "", True)])
        self.assertEqual(ev["key"], "k5")
        self.assertEqual(ev["attendees"], ["Oleg"])
        self.assertTrue(ev["notes_path"].startswith("~/Meetings/") and ev["notes_path"].endswith("/notes.md"))
        self.assertNotIn("notify", [e["kind"] for e in self.work.events])
        # Delivered once: notes rewritten later don't go again.
        deliver.after_notes(mid)
        self.assertEqual(assistant.pending(), [])

    def test_without_the_link_notes_go_to_the_inbox_as_before(self):
        x = bot.create("https://meet.google.com/abc-defg-hij", "Solo", None)
        mid = bot.report(x["id"], "recording")["meeting_id"]
        store.write_text(mid, "notes.md", "## Summary\n- ok\n")
        with mock.patch.object(deliver, "app_api_config", lambda: {"url": "http://w", "token": "t"}):
            deliver.after_notes(mid)
        self.assertEqual(store.get(mid)["delivery"].get("inbox"), "sent")
        self.assertEqual(assistant.pending(), [])

    def test_tick_end_to_end(self):
        """Link on → calendar through Uno → the bot is sent → statuses go to Work."""
        t = self.now + timedelta(seconds=20)
        self.work.feed = {"account": "misha@x.com", "events": [gevent("e2e", t, title="Weekly")]}
        assistant.tick()
        calendars.refresh(force=True)
        ev = self.by_title()["Weekly"]
        self.assertTrue(ev["join"])
        bot.due_from_calendar(ev)
        job = bot.claim(1, [])["start"][0]
        self.assertEqual(job["name"], "Ada (Uno notes)")
        assistant.tick()
        kinds = [(e["kind"], e.get("status")) for e in self.work.events]
        self.assertIn(("call.status", "joining"), kinds)
        self.assertIn(("calls.upcoming", None), kinds)


if __name__ == "__main__":
    unittest.main()
