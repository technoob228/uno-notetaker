"""v2: action items, search, speaker rename, Telegram pairing, Inbox notify.

    python3 -m unittest discover -s app/tests       (from the repo root)

Stdlib only for actions/search; the Telegram and delivery tests need httpx
(run them in the image: docker run --rm -v $PWD/app/tests:/app/tests
--entrypoint python <image> -m unittest discover -s /app/tests).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="nt-test-")
os.environ["MEETINGS_DIR"] = os.path.join(TMP, "Meetings")
os.environ["STATE_DIR"] = os.path.join(TMP, "state")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from notetaker import actions, config, store  # noqa: E402

HAS_HTTPX = importlib.util.find_spec("httpx") is not None
if config.MEETINGS_DIR != os.environ["MEETINGS_DIR"]:  # imported earlier by another test module
    config.MEETINGS_DIR = store.MEETINGS_DIR = os.environ["MEETINGS_DIR"]
    config.STATE_DIR = os.environ["STATE_DIR"]

NOTES = """# Pilot launch

## Summary
- Agreed to start the pilot on Monday. [00:10]
- Price stays at $10.

## Decisions
- Start on Monday [00:10]

## Action items
- [ ] **Oleg**: send the list of users (due: tomorrow) [00:22]
- [x] **Marina**: give access to the test server (до пятницы) [00:30]
- [ ] **—**: check VAT with the lawyers [1:02:03]

## Open questions
- [ ] not an action item
"""

TRANSCRIPT = [
    {"start": 0.0, "end": 5.0, "speaker": "Speaker 1", "text": "Hi Oleg, let's talk about the pilot."},
    {"start": 5.0, "end": 12.0, "speaker": "Oleg", "text": "Sure. We need VAT on the contract."},
    {"start": 12.0, "end": 14.0, "speaker": "Speaker 1", "text": "Speaker 1 will check with the lawyers."},
]


def make_meeting(title="Pilot launch", notes=NOTES, status="done"):
    m = store.create(title, "upload", "general", "auto", tz_offset_min=-180)
    store.write_text(m["id"], "notes.md", notes)
    store.write_text(m["id"], "transcript.json", json.dumps(TRANSCRIPT))
    store.update(m["id"], status=status)
    return m["id"]


class ActionsTest(unittest.TestCase):
    def test_parse(self):
        items = actions.parse(NOTES)
        self.assertEqual([i["owner"] for i in items], ["Oleg", "Marina", ""])
        self.assertEqual(items[0]["text"], "send the list of users")
        self.assertEqual(items[0]["due"], "tomorrow")
        self.assertEqual(items[1]["due"], "до пятницы")
        self.assertTrue(items[1]["done"])
        self.assertEqual(items[2]["ts_seconds"], 3723)
        self.assertEqual(len(items), 3, "task lines outside 'Action items' are not action items")

    def test_toggle_rewrites_one_line(self):
        mid = make_meeting()
        first = actions.for_meeting(mid)[0]
        actions.set_done(mid, first["id"], True)
        notes = store.read_text(mid, "notes.md")
        self.assertIn("- [x] **Oleg**: send the list of users", notes)
        self.assertEqual(notes.count("\n"), NOTES.count("\n"))
        self.assertTrue(actions.find(mid, first["id"])["done"])
        actions.set_done(mid, first["id"], False)
        self.assertEqual(store.read_text(mid, "notes.md"), NOTES)

    def test_across_meetings_skips_done_and_unfinished(self):
        a = make_meeting("A")
        make_meeting("B", status="summarizing")
        ids = {x["meeting_id"] for x in actions.across_meetings()}
        self.assertIn(a, ids)
        self.assertTrue(all(not x["done"] for x in actions.across_meetings()))

    def test_summary_bullets(self):
        self.assertEqual(actions.summary_bullets(NOTES), ["Agreed to start the pilot on Monday.", "Price stays at $10."])

    def test_created_at_has_offset(self):
        mid = make_meeting()
        self.assertTrue(store.get(mid)["created_at"].endswith("-03:00"))


class SearchTest(unittest.TestCase):
    def test_all_words_ranked_with_timestamps(self):
        mid = make_meeting("Search me")
        res = [m for m in store.search("vat contract") if m["id"] == mid]
        self.assertEqual(len(res), 1)
        hit = res[0]["hits"][0]
        self.assertEqual(hit["where"], "transcript")
        self.assertEqual(hit["ts"], 5.0)
        self.assertEqual(hit["speaker"], "Oleg")
        self.assertIn("VAT", hit["text"], "snippets keep the original case")
        self.assertFalse([m for m in store.search("vat zebra") if m["id"] == mid])

    def test_cache_sees_edits(self):
        mid = make_meeting("Cache")
        self.assertFalse([m for m in store.search("kangaroo") if m["id"] == mid])
        os.utime(os.path.join(store.folder(mid), "notes.md"), (1, 1))
        store.write_text(mid, "notes.md", NOTES + "\n- kangaroo\n")
        self.assertTrue([m for m in store.search("kangaroo") if m["id"] == mid])


@unittest.skipUnless(HAS_HTTPX, "needs httpx (run in the image)")
class RenameTest(unittest.TestCase):
    def test_rename_everywhere(self):
        from notetaker import pipeline
        mid = make_meeting("Rename", notes=NOTES.replace("**—**", "**Speaker 1**"))
        n = pipeline.rename_speaker(mid, "Speaker 1", "Mike")
        self.assertEqual(n, 2)
        segs = json.loads(store.read_text(mid, "transcript.json"))
        self.assertEqual([s["speaker"] for s in segs], ["Mike", "Oleg", "Mike"])
        self.assertIn("**Mike**: check VAT", store.read_text(mid, "notes.md"))
        self.assertIn("Mike:", store.read_text(mid, "transcript.md"))
        self.assertIn("Speaker 1 will check", segs[2]["text"], "the spoken words are not rewritten")

    def test_action_id_survives_rename(self):
        from notetaker import pipeline
        mid = make_meeting("Rename id", notes=NOTES.replace("**—**", "**Speaker 1**"))
        before = actions.for_meeting(mid)[2]["id"]
        pipeline.rename_speaker(mid, "Speaker 1", "Mike")
        after = actions.for_meeting(mid)[2]
        self.assertEqual((after["id"], after["owner"]), (before, "Mike"))

    def test_talk_time(self):
        from notetaker import pipeline
        tt = pipeline.talk_time(TRANSCRIPT)
        self.assertEqual(tt[0]["speaker"], "Speaker 1")
        self.assertEqual(tt[0]["seconds"], 7)


@unittest.skipUnless(HAS_HTTPX, "needs httpx (run in the image)")
class TelegramTest(unittest.TestCase):
    def setUp(self):
        from notetaker import telegram
        self.tg = telegram
        self.sent = []
        self.p = mock.patch.object(telegram, "call", side_effect=self.fake_call)
        self.p.start()
        self.addCleanup(self.p.stop)
        config.save_settings({"telegram_token": "1:x"}, internal=True)
        config.save_settings({"telegram_chat_id": "", "telegram_bot_name": "b"}, internal=True)

    def fake_call(self, token, method, **params):
        if method == "getMe":
            return {"username": "notes_bot"}
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": 1}
        return True

    def msg(self, chat, text):
        return {"update_id": 1, "message": {"message_id": 7, "chat": {"id": chat, "type": "private", "first_name": "Misha"},
                                            "text": text}}

    def test_pairing_only_with_code_then_private(self):
        st = self.tg.connect("1:x")
        self.assertIn("start=", st["link"])
        self.tg.handle("1:x", self.msg(42, "/start wrong"))
        self.assertFalse(self.tg.ready())
        self.tg.handle("1:x", self.msg(42, f"/start {st['code']}"))
        self.assertTrue(self.tg.ready())
        self.assertEqual(config.load_settings().telegram_chat_id, "42")
        self.tg.handle("1:x", self.msg(99, "/todo"))
        self.assertEqual(self.sent[-1]["chat_id"], "99")
        self.assertIn("private", self.sent[-1]["text"])
        self.tg.handle("1:x", self.msg(42, "/todo"))
        self.assertEqual(self.sent[-1]["chat_id"], "42")

    def test_token_masked_and_not_settable_from_form(self):
        config.save_settings({"telegram_chat_id": "13"})  # the form may not pair a chat
        self.assertNotEqual(config.load_settings().telegram_chat_id, "13")
        self.assertTrue(config.load_settings().public()["telegram_token"].startswith("•"))

    def test_notes_message_escapes(self):
        mid = make_meeting("<b>x</b> & co")
        text = self.tg.notes_message(mid)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; co", text)
        self.assertIn("Action items (2)", text)


@unittest.skipUnless(HAS_HTTPX, "needs httpx (run in the image)")
class InboxTest(unittest.TestCase):
    def test_notify_body_and_open(self):
        from notetaker import deliver
        mid = make_meeting("Inbox")
        client = mock.Mock()
        with mock.patch.object(deliver, "_app_client", return_value=client):
            self.assertEqual(deliver.notify_inbox(mid), "sent")
        args, kw = client.notify.call_args
        self.assertEqual(args[0], "Notes ready: Inbox")
        self.assertTrue(kw["body"].startswith("2 action items · Oleg: send the list of users (tomorrow)"))
        self.assertEqual(kw["open"], {"app": True, "path": f"/m/{mid}"})

    def test_no_uno_work(self):
        from notetaker import deliver
        with mock.patch.object(deliver, "_app_client", return_value=None):
            self.assertEqual(deliver.notify_inbox(make_meeting()), "no Uno Work on this computer")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
