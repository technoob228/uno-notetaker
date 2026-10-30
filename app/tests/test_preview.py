"""The one-line summary under each meeting on the home page (0.8.0).

    python3 -m unittest discover -s app/tests       (from the repo root)
"""
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="nt-preview-")
os.environ.setdefault("MEETINGS_DIR", os.path.join(TMP, "Meetings"))
os.environ.setdefault("STATE_DIR", os.path.join(TMP, "state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from notetaker.main import summary_line  # noqa: E402


class SummaryLine(unittest.TestCase):
    def test_first_point_under_summary(self):
        md = "# Pilot sync\n\n## Action items\n- [ ] **Oleg** send it [12:40]\n\n## Summary\n- Acme is **happy** [01:10]\n- Second\n"
        self.assertEqual(summary_line(md), "Acme is happy")

    def test_russian_heading(self):
        self.assertEqual(summary_line("## Резюме\n\n1. Бюджет одобрен\n"), "Бюджет одобрен")

    def test_no_summary_takes_first_plain_line(self):
        self.assertEqual(summary_line("# Title\n\n## Notes\nJust a voice memo.\n"), "Just a voice memo.")

    def test_empty_summary_section_gives_nothing(self):
        self.assertEqual(summary_line("## Summary\n\n## Decisions\n- Ship it\n"), "")

    def test_long_line_is_cut(self):
        self.assertEqual(len(summary_line("## Summary\n" + "word " * 100)), 220)


if __name__ == "__main__":
    unittest.main()
