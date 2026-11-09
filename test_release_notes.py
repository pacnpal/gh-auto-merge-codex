import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import release_notes as rn  # noqa: E402

LOG = """commit abc1234
feat: add dark mode (#14)

Adds a theme switch.
----
commit def5678
fix(ui): stop the log jumping (#13)

----
commit 0a1b2c3
chore: bump pillow
----"""

REPLY = """TITLE: Dark mode, a steadier log.
=== BREAKING ===
NONE
=== NOTES ===
prherd now follows your system's light or dark appearance, and the log stays put.

---

## Dark mode (#14)

Pick **Light**, **Dark** or **Auto** in the sidebar.

---
"""

PREVIOUS = """# ⚠️ **BREAKING CHANGES**

**Read this before upgrading.** Each item says what changed, who it affects, and what to
do instead. If nothing here describes your setup, the upgrade needs no action.

### **In 1.1.0**

**Settings moved (#9).**
- **What changed:** x

---

## v1.1.0 - Settings window

Intro.

**Full Changelog**: https://github.com/o/r/compare/v1.0.0...v1.1.0"""


class Test(unittest.TestCase):
    def test_subjects_and_fallback_title(self):
        self.assertEqual(rn.subjects(LOG), ["feat: add dark mode (#14)", "fix(ui): stop the log jumping (#13)",
                                            "chore: bump pillow"])
        self.assertEqual(rn.fallback_title(rn.subjects(LOG)), "Add dark mode (#14), Stop the log jumping (#13), Bump pillow")
        self.assertEqual(rn.fallback_title([]), "Maintenance")
        self.assertLessEqual(len(rn.fallback_title(["feat: " + "word " * 40])), 80)
        self.assertEqual(rn.pull_numbers(LOG), {"14", "13"})

    def test_parse_reply(self):
        title, breaking, notes = rn.parse_reply(REPLY, {"14", "13"})
        self.assertEqual((title, breaking), ("Dark mode, a steadier log", ""))
        self.assertTrue(notes.startswith("prherd now follows") and notes.endswith("in the sidebar."))
        self.assertIsNone(rn.parse_reply(REPLY, {"13"}))  # cites #14, which no commit mentions
        breaking_reply = REPLY.replace("NONE", "**Config moved (#13).**\n- **What changed:** y")
        self.assertEqual(rn.parse_reply(breaking_reply, {"14", "13"})[1], "**Config moved (#13).**\n- **What changed:** y")
        for bad in (None, "", "no markers", REPLY.replace("=== NOTES ===", ""), "TITLE: x\n=== BREAKING ===\nNONE\n=== NOTES ===\nshort"):
            self.assertIsNone(rn.parse_reply(bad, {"14", "13"}), bad)

    def test_compose_carries_history(self):
        out = rn.compose("1.2.0", "Dark mode", "**New thing (#14).**", "Intro.\n\n---\n\n## Dark mode (#14)\n\nText.",
                         "https://github.com/o/r/compare/v1.1.0...v1.2.0", PREVIOUS)
        self.assertTrue(out.startswith("# ⚠️ **BREAKING CHANGES**"))
        self.assertLess(out.index("### **In 1.2.0**"), out.index("### **In 1.1.0**"))  # newest first
        self.assertLess(out.index("## v1.2.0 - Dark mode"), out.index("## v1.1.0 - Settings window"))
        self.assertIn("`prherd-1.2.0-macos-arm64.dmg`", out)
        self.assertEqual(out.count("BREAKING CHANGES"), 1)
        self.assertEqual(out.count("**Read this before upgrading.**"), 1)
        plain = rn.compose("1.0.0", "First", "", "Intro.", "https://github.com/o/r/commits/v1.0.0", "")
        self.assertNotIn("BREAKING", plain)
        self.assertTrue(plain.startswith("## v1.0.0 - First"))

    def test_compose_stays_under_githubs_limit(self):
        with mock.patch.object(rn, "MAX_NOTES_CHARS", 400):
            out = rn.compose("1.2.0", "T", "", "Intro.", "link", "## v1.1.0 - Old\n\n" + "x " * 400)
        self.assertLessEqual(len(out), 450)
        self.assertIn("Older notes are on the earlier releases.", out)

    def test_previous_tag(self):
        with tempfile.TemporaryDirectory() as d:
            def git(*args):
                return subprocess.run(["git", "-C", d, *args], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
            git("tag", "v1.0.0")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
            git("tag", "v1.1.0")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "three")
            head = git("rev-parse", "HEAD")
            git("tag", "v1.2.0")
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(rn.previous_tag(head, "v1.2.0"), "v1.1.0")  # the tag being released is skipped
                self.assertEqual(rn.subjects(rn.commit_log("v1.1.0", head)), ["three"])
                self.assertEqual(rn.previous_tag(git("rev-list", "--max-parents=0", "HEAD"), "v1.0.0"), None)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
