import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import codex_automerge as ca

REPO = "github.com/octo/direct-repo"
HEAD = "bfd4fae453adcbc45e6cc0a4034898a3002d97ea"
BOT = {"login": "chatgpt-codex-connector[bot]", "id": 199175422}
ME = {"login": "watcher", "id": 42}  # the account gh is logged in as
CLEAN = "<!-- codex-review-receipt:v1 -->\n\nCodex Review: Didn't find any major issues. Keep them coming!\n\n**Reviewed commit:** `{}`"
PUSHED, AFTER, BEFORE = "2026-10-26T11:17:58Z", "2026-10-26T11:27:33Z", "2026-10-26T11:00:00Z"
THUMBS = [{"user": BOT, "content": "+1", "created_at": AFTER}]


def receipt(sha=HEAD[:10], at=AFTER, body=CLEAN):
    return {"user": BOT, "body": body.format(sha), "created_at": at}


class FakeGh:
    """Stands in for `gh`; records every call."""

    def __init__(self, reactions=(), comments=(), fail=(), merge=(0, "✓ Squashed and merged pull request"),
                 suites=(PUSHED,), timeline=(), mergeable="MERGEABLE"):
        self.calls = []
        self.reactions, self.comments, self.fail, self.merge = reactions, comments, fail, merge
        self.suites, self.timeline, self.mergeable = suites, timeline, mergeable

    def __call__(self, *args, cwd=None):
        self.calls.append((args, cwd))
        done = lambda code, out: subprocess.CompletedProcess(args, code, out, "")
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if args[:2] == ("repo", "view"):
            return done(0, "https://github.example/acme/local-repo\n" if cwd else f"https://github.com/{args[2]}\n")
        if args[:2] == ("pr", "list"):
            return done(0, json.dumps([{"number": 7, "title": "t", "headRefOid": HEAD,
                                        "mergeable": self.mergeable, "baseRefName": "main"}]))
        if args[:2] == ("api", "--hostname") and args[-1] == "user":
            return done(0, json.dumps(ME))
        if args[0] == "api" and "check-suites" in args[-1]:
            return done(0, json.dumps([{"check_suites": [{"created_at": t} for t in self.suites]}]))
        if args[0] == "api" and "timeline" in args[-1]:
            return done(0, json.dumps([list(self.timeline)]))
        if args[0] == "api":
            return done(0, json.dumps([list(self.reactions if "reactions" in args[-1] else self.comments)]))
        if args[:2] == ("pr", "merge"):
            return done(*self.merge)
        return done(0, "")

    def merges(self):
        return [args for args, _ in self.calls if args[:2] == ("pr", "merge")]

    def comments_posted(self):
        return [args[-1] for args, _ in self.calls if args[:2] == ("pr", "comment")]


def run(fake, mode=""):
    logs = []
    with mock.patch.object(ca, "gh", fake):
        rows = ca.poll(REPO, logs.append, lambda: mode)
    return rows, "\n".join(logs)


class Test(unittest.TestCase):
    def test_resolve(self):
        fake = FakeGh()
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.resolve("."), "github.example/acme/local-repo")
            self.assertEqual(ca.resolve("octo/direct-repo"), REPO)

    def test_thumbs_up_merges_queued_on_head(self):
        fake = FakeGh(reactions=THUMBS)
        rows, log = run(fake)
        self.assertEqual(rows[0][1], "👍")
        merge = fake.merges()[0]
        self.assertIn("--auto", merge)
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], HEAD)

    def test_merge_mode_is_asked_at_merge_time(self):
        # GUI: repo removed or Auto-merge unticked while this poll was running.
        fake = FakeGh(reactions=THUMBS)
        with mock.patch.object(ca, "gh", fake):
            rows = ca.poll(REPO, lambda _: None, lambda: None)
        self.assertEqual(rows[0][1], "👍")
        self.assertEqual(fake.merges(), [])

    def test_impersonator_does_not_merge(self):
        # Codex's login on another account: only the id counts.
        fake = FakeGh(reactions=[dict(THUMBS[0], user=dict(BOT, id=1))],
                      comments=[dict(receipt(), user=dict(BOT, id=1))])
        _, log = run(fake)
        self.assertIn("#7: no 👍 or clean review yet", log)
        self.assertEqual(fake.merges(), [])

    def test_clean_comment_on_current_head_merges(self):
        for c in (receipt(), receipt(HEAD, body=CLEAN.replace("'", "’"))):
            fake = FakeGh(comments=[c])
            rows, _ = run(fake)
            self.assertEqual(rows[0][1], "no major issues")
            self.assertEqual(len(fake.merges()), 1)

    def test_clean_comment_on_stale_head_does_not_merge(self):
        fake = FakeGh(comments=[receipt("0123456789")])
        rows, _ = run(fake)
        self.assertIsNone(rows[0][1])
        self.assertEqual(fake.merges(), [])

    def test_signals_older_than_the_push_do_not_merge(self):
        # Pushed after Codex's go-ahead: a 👍 or a receipt whose short hash a crafted commit shares.
        fake = FakeGh(reactions=[dict(THUMBS[0], created_at=BEFORE)], comments=[receipt(at=BEFORE)])
        rows, _ = run(fake)
        self.assertIsNone(rows[0][1])
        self.assertEqual(fake.merges(), [])

    def test_no_push_time_means_no_go_ahead(self):
        fake = FakeGh(reactions=THUMBS, comments=[receipt()], suites=())
        rows, _ = run(fake)
        self.assertIsNone(rows[0][1])
        self.assertEqual(fake.merges(), [])

    def test_force_push_after_go_ahead_voids_it(self):
        # Head force-pushed back to an older commit whose check suites predate the 👍.
        fake = FakeGh(reactions=THUMBS, comments=[receipt()], suites=(BEFORE,),
                      timeline=[{"event": "committed"},
                                {"event": "head_ref_force_pushed", "created_at": "2026-10-26T12:00:00Z"}])
        rows, _ = run(fake)
        self.assertIsNone(rows[0][1])
        self.assertEqual(fake.merges(), [])

    def test_only_codex_review_receipts_count(self):
        reply = "Sure! Codex Review: Didn't find any major issues.\n**Reviewed commit:** `{}`"
        for c in (receipt(body=reply), receipt(HEAD[:7])):  # free-text reply; hash too short
            rows, _ = run(FakeGh(comments=[c]))
            self.assertIsNone(rows[0][1])

    def test_conflict_is_commented_once_per_head_and_not_merged(self):
        fake = FakeGh(reactions=THUMBS, mergeable="CONFLICTING")
        rows, log = run(fake)
        self.assertEqual(rows[0][1], "merge conflict")
        self.assertEqual(fake.merges(), [])
        [body] = fake.comments_posted()
        self.assertIn("merge conflicts with `main`", body)
        self.assertIn(ca.CONFLICT.format(HEAD), body)
        self.assertNotIn("@codex", body)
        self.assertIn("#7: merge conflict; commented on the PR", log)
        # Already flagged for this head (e.g. before a restart): no second comment.
        fake = FakeGh(mergeable="CONFLICTING", comments=[{"user": ME, "body": body}])
        _, log = run(fake)
        self.assertEqual(fake.comments_posted(), [])
        self.assertIn("#7: merge conflict; already commented", log)
        # Someone else planting the marker doesn't silence the notice.
        fake = FakeGh(mergeable="CONFLICTING", comments=[{"user": BOT, "body": body}])
        run(fake)
        self.assertEqual(len(fake.comments_posted()), 1)

    def test_conflict_commented_even_when_not_merging(self):
        fake = FakeGh(mergeable="CONFLICTING")
        with mock.patch.object(ca, "gh", fake):
            rows = ca.poll(REPO, lambda _: None, lambda: None)
        self.assertEqual(rows[0][1], "merge conflict")
        self.assertEqual(len(fake.comments_posted()), 1)

    def test_unknown_mergeability_takes_the_normal_path(self):
        fake = FakeGh(reactions=THUMBS, mergeable="UNKNOWN")
        rows, _ = run(fake)
        self.assertEqual(rows[0][1], "👍")
        self.assertEqual((len(fake.merges()), fake.comments_posted()), (1, []))

    def test_modes(self):
        thumbs = THUMBS
        admin, force = FakeGh(reactions=thumbs), FakeGh(reactions=thumbs)
        run(admin, "admin")
        run(force, "force")
        self.assertIn("--admin", admin.merges()[0])
        self.assertNotIn("--auto", admin.merges()[0])
        self.assertNotIn("--admin", force.merges()[0])
        self.assertNotIn("--auto", force.merges()[0])

    def test_auto_fallback_only_on_auto_merge_error(self):
        thumbs = THUMBS
        fake = FakeGh(reactions=thumbs, merge=(1, "auto merge is not allowed for this repository"))
        _, log = run(fake)
        self.assertEqual(len(fake.merges()), 2)
        self.assertNotIn("--auto", fake.merges()[1])
        fake = FakeGh(reactions=thumbs, merge=(1, "merge conflict"))
        _, log = run(fake)
        self.assertEqual(len(fake.merges()), 1)
        self.assertIn("MERGE FAILED: merge conflict", log)

    def test_not_merged_unless_gh_says_so(self):
        fake = FakeGh(reactions=THUMBS, merge=(0, "enabled auto-merge"))
        _, log = run(fake, "force")
        self.assertIn("not merged — enabled auto-merge", log)

    def test_failures_are_logged_and_retried(self):
        rows, log = run(FakeGh(fail=["pr list"]))
        self.assertIsNone(rows)
        self.assertIn("failed to list open PRs; retrying", log)
        _, log = run(FakeGh(fail=["api"]))
        self.assertIn("#7: failed to fetch Codex reactions/comments; retrying", log)

    def test_gh_timeout_is_a_gh_failure(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 120)):
            r = ca.gh("pr", "list")
        self.assertEqual((r.returncode, r.stderr), (1, "gh timed out after 120s"))

    def test_request_review_posts_comment(self):
        fake = FakeGh()
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.request_review(REPO, "7"), "asked Codex for a review")
        self.assertEqual(fake.calls[0][0], ("pr", "comment", "7", "-R", REPO, "--body", "@codex review"))

    def test_args(self):
        self.assertEqual(ca.parse(["--admin", "--force", "a/b"]).mode, "force")
        self.assertEqual((ca.parse([]).mode, ca.parse([]).interval), (None, None))  # saved settings apply
        self.assertEqual(ca.parse(["--interval", "5", "a/b", "c/d"]).repos, ["a/b", "c/d"])
        for bad in (["--interval", "0"], ["--interval", "x"], ["--unknown"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                ca.parse(bad)

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/sub/s.json"):
            self.assertEqual(ca.load_settings(), {"repos": [], "mode": "", "interval": 60,
                                                  "merging": True, "geometry": None})
            saved = {"repos": [REPO], "mode": "admin", "interval": 5, "merging": False, "geometry": "800x600+0+0"}
            ca.save_settings(saved)
            self.assertEqual(ca.load_settings(), saved)

    def test_bad_settings_are_not_trusted_or_overwritten(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/s.json"):
            pathlib.Path(ca.CONFIG).write_text('{"mode": "foo", "interval": 0, "repos": [1, "a/b"], "merging": "no"}')
            s = ca.load_settings()
            self.assertEqual((s["mode"], s["interval"], s["repos"]), ("", 60, ["a/b"]))  # "foo" would force-merge
            pathlib.Path(ca.CONFIG).write_text("{oops")
            with self.assertRaises(SystemExit):
                ca.load_settings()
            self.assertEqual(pathlib.Path(ca.CONFIG).read_text(), "{oops")

    def test_procfile(self):
        procfile = pathlib.Path(__file__).with_name("Procfile").read_text()
        self.assertIn("codex-automerge: ./codex_automerge.py --headless --admin owner/repo", procfile)


if __name__ == "__main__":
    unittest.main()
