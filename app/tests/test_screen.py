"""Screen recordings from Uno Work (Loom Work) in the meetings library.

    python3 -m unittest discover -s app/tests       (from the repo root)

Uno Work writes a ready folder into ~/Meetings (docs/screen-recordings.md):
meeting.json with source "screen", status "summarizing", transcript.json with
what was said, on screen and done per line. The app notices it, writes the
notes (action items included) and never tries to transcribe it.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.mkdtemp(prefix="nt-screen-")
os.environ.setdefault("MEETINGS_DIR", os.path.join(TMP, "Meetings"))
os.environ.setdefault("STATE_DIR", os.path.join(TMP, "state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from notetaker import ai, config, pipeline, store  # noqa: E402

if config.MEETINGS_DIR != os.environ["MEETINGS_DIR"]:  # imported earlier by another test module
    config.MEETINGS_DIR = store.MEETINGS_DIR = os.environ["MEETINGS_DIR"]

SEGMENTS = [
    {"start": 0, "end": 4, "speaker": "Me", "text": "Это страница настроек.",
     "screen": "Acme CRM, Settings page, form 'Profile'", "did": ""},
    {"start": 4, "end": 9, "speaker": "Me", "text": "Вот тут криво.",
     "screen": "the Save button sits below the form, misaligned", "did": "clicked Save"},
    {"start": 9, "end": 12, "speaker": "", "text": "", "screen": "a toast 'Saved'", "did": ""},
]

NOTES = """# Кривая кнопка Save в настройках

## Summary
- Показана страница настроек Acme CRM. [00:00]

## Decisions
—

## Action items
- [ ] **—**: выровнять кнопку Save в форме Profile [00:04]

## Open questions
—

## What was shown
- Страница настроек [00:00]
"""


def write_from_work(mid: str, folder: str = "2026-10-29 14-02 Screen recording", **meta_over) -> str:
    """The folder exactly as Uno Work writes it: transcript first, meeting.json last."""
    path = os.path.join(store.MEETINGS_DIR, folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "transcript.json"), "w") as fh:
        json.dump(SEGMENTS, fh)
    meta = {
        "id": mid, "title": "", "created_at": "2026-10-29T14:02:11+03:00",
        "source": "screen", "template": "screen", "language": "auto",
        "status": "summarizing", "progress": "Writing notes…", "error": "",
        "duration": 12, "tracks": {}, "share": None, "chat": [], "usage": {}, "folder": folder,
        "screen": {"drive_key": "Recordings/Screen recording 2026-10-29 at 14.02.11.webm",
                   "share_url": "https://console.uno.place/api/v1/drive/d/abc",
                   "share_expires_at": "2026-11-05T11:02:11Z", "width": 1280, "height": 800,
                   "from": "uno-work"},
    }
    meta.update(meta_over)
    with open(os.path.join(path, "meeting.json"), "w") as fh:
        json.dump(meta, fh)
    return path


class ScreenRecordings(unittest.TestCase):
    def setUp(self):
        pipeline._screen_seen.clear()

    def test_picked_up_once_and_queued_for_notes(self):
        write_from_work("scrn000001", folder="2026-10-29 14-02 Screen recording (a)")
        with mock.patch.object(pipeline, "enqueue") as enq:
            self.assertIn("scrn000001", pipeline.pick_up_screen_recordings())
            enq.assert_any_call("scrn000001", "notes")
            enq.reset_mock()
            self.assertNotIn("scrn000001", pipeline.pick_up_screen_recordings())
            enq.assert_not_called()

    def test_other_meetings_are_not_picked_up(self):
        m = store.create("Call", "upload", "general", "auto")
        store.update(m["id"], status="summarizing")
        with mock.patch.object(pipeline, "enqueue") as enq:
            self.assertNotIn(m["id"], pipeline.pick_up_screen_recordings())
            for call in enq.call_args_list:
                self.assertNotEqual(call.args[0], m["id"])

    def test_done_recording_is_left_alone(self):
        write_from_work("scrn000002", folder="2026-10-29 14-05 Screen recording", status="done")
        with mock.patch.object(pipeline, "enqueue") as enq:
            self.assertNotIn("scrn000002", pipeline.pick_up_screen_recordings())
            enq.assert_not_called()

    def test_ai_sees_the_screen(self):
        text = pipeline.transcript_for_ai(SEGMENTS)
        self.assertIn("[00:04] Me: Вот тут криво. [screen: the Save button sits below the form, misaligned]"
                      " [did: clicked Save]", text)
        self.assertIn("[00:09] (silence) [screen: a toast 'Saved']", text)

    def test_screen_template_prompt(self):
        prompt = ai.notes_prompt("screen", "auto", two_track=True)
        self.assertIn("screen recording", prompt)
        self.assertIn("What was shown", prompt)
        self.assertNotIn("screen", ai.PICKABLE_TEMPLATES)
        self.assertIn("general", ai.PICKABLE_TEMPLATES)

    def test_notes_written_without_transcribing(self):
        write_from_work("scrn000003", folder="2026-10-29 14-09 Screen recording")
        seen = {}

        def fake_chat(p, s, messages, **kw):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return NOTES, {"model": "test", "prompt_tokens": 1, "completion_tokens": 1}

        with mock.patch.object(ai, "chat", side_effect=fake_chat), \
                mock.patch.object(pipeline, "transcribe") as tr, \
                mock.patch.object(pipeline.deliver, "after_notes"):
            # A restart's "full" job for a screen recording must not transcribe either.
            pipeline.run_job("scrn000003", "full")
            tr.assert_not_called()
        m = store.get("scrn000003")
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["title"], "Кривая кнопка Save в настройках")
        self.assertIn("What was shown", seen["system"])
        self.assertIn("[did: clicked Save]", seen["user"])
        self.assertIn("- [ ] **—**: выровнять кнопку Save", store.read_text("scrn000003", "notes.md"))
        md = store.read_text("scrn000003", "transcript.md")
        self.assertIn("_On screen: the Save button sits below the form, misaligned_", md)
        self.assertIn("_Did: clicked Save_", md)
        # the folder took the title, the meeting keeps its link to the video
        self.assertTrue(m["folder"].endswith("Кривая кнопка Save в настройках"))
        self.assertEqual(m["screen"]["drive_key"], "Recordings/Screen recording 2026-10-29 at 14.02.11.webm")

    def test_full_job_of_a_call_still_transcribes(self):
        m = store.create("Call", "upload", "general", "auto")
        with mock.patch.object(pipeline, "transcribe") as tr, mock.patch.object(pipeline, "summarize") as su:
            pipeline.run_job(m["id"], "full")
        tr.assert_called_once_with(m["id"])
        su.assert_called_once_with(m["id"])


if __name__ == "__main__":
    unittest.main()
