import io
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import prherd as ca

REPO = "github.com/octo/direct-repo"
ROW = f"{REPO}#7"  # FakeGh's one PR, as a window row
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
                 suites=(PUSHED,), timeline=(), mergeable="MERGEABLE", labels=(), late_labels=None, auto=None,
                 more=()):
        self.calls = []
        self.reactions, self.comments, self.fail, self.merge = reactions, comments, fail, merge
        self.suites, self.timeline, self.mergeable = suites, timeline, mergeable
        self.labels, self.auto = labels, auto
        self.more = more  # further PRs after #7, each as {"number": n, ...fields that differ from #7}
        self.late_labels = labels if late_labels is None else late_labels  # as seen right before a merge

    def __call__(self, *args, cwd=None):
        self.calls.append((args, cwd))
        done = lambda code, out: subprocess.CompletedProcess(args, code, out, "")
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if args[:2] == ("repo", "view"):
            return done(0, "https://github.example/acme/local-repo\n" if cwd else f"https://github.com/{args[2]}\n")
        if args[:2] == ("pr", "list"):
            pr = {"number": 7, "title": "t", "headRefOid": HEAD, "mergeable": self.mergeable,
                  "baseRefName": "main", "labels": [{"name": n} for n in self.labels],
                  "autoMergeRequest": self.auto, "isDraft": False, "author": {"login": "octocat"}}
            return done(0, json.dumps([pr, *(dict(pr, **more) for more in self.more)]))
        if args[:2] == ("pr", "view"):
            return done(0, json.dumps({"labels": [{"name": n} for n in self.late_labels]}))
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


class StatefulGh(FakeGh):
    """A FakeGh whose label and auto-merge change as they're acted on, as GitHub's would."""

    def __call__(self, *args, cwd=None):
        if "POST" in args:
            self.labels = self.late_labels = ["do-not-merge"]
        elif "DELETE" in args:
            self.labels = self.late_labels = []
        elif "--disable-auto" in args:
            self.auto = None
        return super().__call__(*args, cwd=cwd)


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

    def test_eyes_shows_reviewing_without_merging(self):
        fake = FakeGh(reactions=[dict(THUMBS[0], content="eyes")])
        rows, log = run(fake)
        self.assertEqual(rows[0][1], "👀")
        self.assertIn("#7: Codex is reviewing (👀)", log)
        self.assertEqual(fake.merges(), [])
        # A clean verdict wins over a leftover 👀; a 👀 from before this head doesn't count.
        rows, _ = run(FakeGh(reactions=[dict(THUMBS[0], content="eyes")], comments=[receipt()]))
        self.assertEqual(rows[0][1], "no major issues")
        fake = FakeGh(reactions=[dict(THUMBS[0], content="eyes", created_at=BEFORE)])
        rows, _ = run(fake)
        self.assertIsNone(rows[0][1])
        self.assertEqual(fake.merges(), [])

    def test_marked_pr_is_not_merged_in_any_mode(self):
        for mode in ("", "force", "admin"):
            for label in ("do-not-merge", "Do-Not-Merge"):  # GitHub labels ignore case
                fake = FakeGh(reactions=THUMBS, labels=[label, "bug"])
                rows, log = run(fake, mode)
                self.assertEqual(rows[0][1], "don't merge")
                self.assertIn("#7: marked don't merge; skipping", log)
                self.assertEqual(fake.merges(), [])

    def test_other_labels_do_not_block(self):
        fake = FakeGh(reactions=THUMBS, labels=["do-not-merge-yet", "bug"])
        rows, _ = run(fake)
        self.assertEqual((rows[0][1], len(fake.merges())), ("👍", 1))

    def test_marked_while_polling_is_not_merged(self):
        fake = FakeGh(reactions=THUMBS, late_labels=["do-not-merge"])
        _, log = run(fake)
        self.assertEqual(fake.merges(), [])
        self.assertIn("marked don't merge; not merging", log)

    def test_labels_unreadable_before_merging_does_not_merge(self):
        fake = FakeGh(reactions=THUMBS, fail=["pr view"])
        _, log = run(fake)
        self.assertEqual(fake.merges(), [])
        self.assertIn("failed to re-check labels; not merging", log)

    def test_marking_cancels_queued_auto_merge(self):
        # Even with Auto-merge unticked: GitHub merges a queued PR by itself.
        fake = FakeGh(labels=["do-not-merge"], auto={"enabledAt": AFTER}, reactions=THUMBS)
        with mock.patch.object(ca, "gh", fake):
            logs = []
            ca.poll(REPO, logs.append, lambda: None)
        self.assertEqual(fake.merges(), [("pr", "merge", "7", "-R", REPO, "--disable-auto")])
        self.assertIn(f"  {REPO}#7: cancelled queued auto-merge", logs)
        fake = FakeGh(labels=["do-not-merge"], auto={"enabledAt": AFTER}, fail=["--disable-auto"])
        _, log = run(fake)
        self.assertIn("cancelling auto-merge FAILED: boom", log)

    def test_marked_conflict_is_not_commented_but_still_cancels_auto_merge(self):
        # Queued and conflicting: GitHub would merge it the moment the conflict is resolved.
        fake = FakeGh(labels=["do-not-merge"], mergeable="CONFLICTING", auto={"enabledAt": AFTER})
        rows, _ = run(fake)
        self.assertEqual(rows[0][1], "don't merge")
        self.assertEqual(fake.comments_posted(), [])
        self.assertEqual(len(fake.merges()), 1)
        self.assertIn("--disable-auto", fake.merges()[0])

    def test_mark_adds_and_removes_the_label(self):
        fake = FakeGh()
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.mark(REPO, "7", True), "marked don't merge")
            self.assertEqual(ca.mark(REPO, "7", False), "cleared don't merge")
        path = "repos/octo/direct-repo/issues/7/labels"
        create, add, remove = [args for args, _ in fake.calls]
        self.assertEqual(create[:3], ("label", "create", "do-not-merge"))
        self.assertNotIn("--force", create)  # a repo's own label keeps its color and description
        self.assertEqual(add, ("api", "--hostname", "github.com", "-X", "POST", path, "-f", "labels[]=do-not-merge"))
        self.assertEqual(remove, ("api", "--hostname", "github.com", "-X", "DELETE", f"{path}/do-not-merge"))

    def test_mark_failure_is_reported(self):
        # "label create" also fails when the label already exists, so only the labeling call decides.
        with mock.patch.object(ca, "gh", FakeGh(fail=["label create"])):
            self.assertEqual(ca.mark(REPO, "7", True), "marked don't merge")
        with mock.patch.object(ca, "gh", FakeGh(fail=["POST"])):
            self.assertEqual(ca.mark(REPO, "7", True), "marked don't merge FAILED: boom")
        with mock.patch.object(ca, "gh", FakeGh(fail=["DELETE"])):
            self.assertEqual(ca.mark(REPO, "7", False), "cleared don't merge FAILED: boom")

    def test_browse_opens_a_pr_or_the_repo(self):
        fake = FakeGh()
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.browse(REPO, "7"), "opened in browser")
            self.assertEqual(ca.browse(REPO), "opened in browser")
        self.assertEqual([args for args, _ in fake.calls], [("browse", "7", "-R", REPO), ("browse", "-R", REPO)])
        with mock.patch.object(ca, "gh", FakeGh(fail=["browse"])):
            self.assertEqual(ca.browse(REPO, "7"), "open in browser FAILED: boom")

    def test_update_branch(self):
        fake = FakeGh()
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.update_branch(REPO, "7"), "branch updated")
        self.assertEqual(fake.calls[0][0], ("pr", "update-branch", "7", "-R", REPO))
        with mock.patch.object(ca, "gh", FakeGh(fail=["update-branch"])):
            self.assertEqual(ca.update_branch(REPO, "7"), "update branch FAILED: boom")

    def test_row_values(self):
        pr = {"isDraft": False, "autoMergeRequest": None, "author": {"login": "octocat"}}
        self.assertEqual(ca.row_values(pr, None), ("Awaiting Codex", "octocat", "Open", "", ""))
        labels = {v: ca.row_values(pr, v)[0] for v in ("👍", "no major issues", "👀", "merge conflict", "?", "don't merge")}
        self.assertEqual(labels, {"👍": "Thumbs up", "no major issues": "No major issues", "👀": "Reviewing",
                                  "merge conflict": "Merge conflict", "?": "Lookup failed", "don't merge": "Don't merge"})
        self.assertEqual(ca.row_values(dict(pr, autoMergeRequest={}), "👍")[2], "Open")  # gh's empty value: not queued
        self.assertEqual(ca.row_values(dict(pr, autoMergeRequest={"enabledAt": AFTER}), "👍")[2], "Queued")
        self.assertEqual(ca.row_values(dict(pr, autoMergeRequest={"enabledAt": AFTER}), "don't merge")[2], "Open")
        self.assertEqual(ca.row_values(dict(pr, isDraft=True, autoMergeRequest={"enabledAt": AFTER}), "👍")[2], "Draft")
        self.assertEqual(ca.row_values(dict(pr, author=None), "👍")[1], "")  # a deleted account

    def window(self, fake, check, interval=3600):
        """Run the window against fake gh; check(root, tree, buttons, wait_for) drives it, then closes it."""
        try:
            import tkinter as tk
            from tkinter import ttk
            tk.Tk().destroy()
        except Exception as e:  # no tkinter, or tk.TclError with no display
            self.skipTest(f"no window available: {e}")

        def drive(root, *_):  # stands in for mainloop, and pumps it
            def widgets(parent):
                yield parent
                for child in parent.winfo_children():
                    yield from widgets(child)

            def wait_for(label):
                deadline = time.time() + 10
                while not (tree.exists(ROW) and tree.set(ROW, "codex") == label):
                    self.assertLess(time.time(), deadline, f"row never showed {label}")
                    root.update()
                    time.sleep(0.01)

            found = list(widgets(root))
            tree = next(w for w in found if isinstance(w, ttk.Treeview))
            buttons = {w.cget("text"): w for w in found if isinstance(w, ttk.Button)}
            check(root, tree, buttons, wait_for)
            for pending in root.tk.call("after", "info"):  # else drain fires in the next test's window, and fails
                root.after_cancel(pending)
            root.destroy()

        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/s.json"), \
                mock.patch.object(ca, "gh", fake), mock.patch.object(tk.Misc, "mainloop", drive):
            ca.gui(["octo/direct-repo"], None, interval)

    def menu_for(self, root, tree):
        """Right-click the PR row; return the menu that opened and its entries' states by label."""
        import tkinter as tk
        root.update()
        x, y, _, height = tree.bbox(ROW)
        with mock.patch.object(tk.Menu, "tk_popup", autospec=True) as popup:  # a real popup is modal on macOS
            tree.event_generate("<Button-3>", x=x + 5, y=y + height // 2)
        menu = popup.call_args.args[0]
        states = {menu.entrycget(i, "label"): menu.entrycget(i, "state")
                  for i in range(menu.index("end") + 1) if menu.type(i) == "command"}
        return menu, states

    def test_window_marks_and_clears_without_merging(self):
        fake = StatefulGh(reactions=THUMBS, auto={"enabledAt": AFTER})

        def check(root, tree, buttons, wait_for):
            wait_for("Thumbs up")
            tree.selection_set(ROW)
            buttons["Don't merge"].invoke()
            wait_for("Don't merge")  # refreshed right away, with no tick: the interval is an hour
            self.assertIsNone(fake.auto)
            buttons["Allow merge"].invoke()
            wait_for("Thumbs up")

        self.window(fake, check)
        # Cancelling the queued auto-merge is the only `gh pr merge`: neither refresh started one.
        self.assertEqual(fake.merges(), [("pr", "merge", "7", "-R", REPO, "--disable-auto")])
        self.assertEqual(fake.labels, [])

    def test_menu_guards_read_the_verdict_not_the_label(self):
        # A held PR: Allow merge on, Don't merge and Merge now off.
        def held(root, tree, buttons, wait_for):
            wait_for("Don't merge")
            _, states = self.menu_for(root, tree)
            self.assertEqual((states["Allow merge"], states["Don't merge"], states["Merge now…"]),
                             ("normal", "disabled", "disabled"))

        self.window(FakeGh(reactions=THUMBS, labels=["do-not-merge"]), held)

        def conflicting(root, tree, buttons, wait_for):
            wait_for("Merge conflict")
            _, states = self.menu_for(root, tree)
            self.assertEqual((states["Merge now…"], states["Allow merge"]), ("disabled", "disabled"))

        self.window(FakeGh(mergeable="CONFLICTING"), conflicting)

    def test_merge_now_skips_held_and_conflicting_prs(self):
        held = {"number": 8, "labels": [{"name": "do-not-merge"}]}
        fake = FakeGh(reactions=THUMBS, more=[held, {"number": 9, "mergeable": "CONFLICTING"}])
        asked = []

        def check(root, tree, buttons, wait_for):
            wait_for("Thumbs up")
            self.assertEqual([tree.set(f"{REPO}#{n}", "codex") for n in (8, 9)], ["Don't merge", "Merge conflict"])
            tree.selection_set([f"{REPO}#{n}" for n in (7, 8, 9)])
            menu, states = self.menu_for(root, tree)
            self.assertEqual(states["Merge now…"], "normal")
            with mock.patch("tkinter.messagebox.askyesno", side_effect=lambda *a, **k: asked.append(a[1])):
                menu.invoke("Merge now…")

        self.window(fake, check)
        [question] = asked
        self.assertIn("#7 t (Thumbs up)", question)
        self.assertNotIn("#8", question)
        self.assertNotIn("#9", question)
        self.assertEqual(fake.merges(), [])  # declined

    def test_window_follows_system_appearance(self):
        # Re-checked on each tick; the window is re-themed only when it changes.
        system = iter([False, False, True])
        themed = []

        def check(root, tree, buttons, wait_for):
            deadline = time.time() + 10
            while len(themed) < 2:
                self.assertLess(time.time(), deadline, "never re-themed")
                root.update()
                time.sleep(0.01)

        with mock.patch.object(ca, "system_dark", lambda root: next(system, True)), \
                mock.patch.object(ca, "theme", side_effect=lambda root, dark, appearance: themed.append(dark)):
            self.window(FakeGh(), check, interval=1)
        self.assertEqual(themed, [False, True])

    def test_system_dark_falls_back_to_defaults(self):
        from tkinter import TclError

        class Root:  # a Tk older than 8.6.10, which has no isdark
            _w = "."
            update_idletasks = lambda self: None
            tk = mock.Mock(call=mock.Mock(side_effect=TclError("bad option")))

        for out, dark in (("Dark\n", True), ("", False)):
            with mock.patch.object(ca.sys, "platform", "darwin"), \
                    mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, out, "")):
                self.assertIs(ca.system_dark(Root()), dark)
        with mock.patch.object(ca.sys, "platform", "linux"), mock.patch("subprocess.run") as run:
            self.assertFalse(ca.system_dark(Root()))
        run.assert_not_called()

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

    def test_gh_spawns_without_fork(self):  # a forked child prints macOS MallocStackLogging noise
        with mock.patch.object(ca, "GH", "/bin/echo"), mock.patch("os.posix_spawn", wraps=os.posix_spawn) as spawn:
            r = ca.gh("hi")
        self.assertEqual(r.stdout, "hi\n")
        spawn.assert_called_once()

    def test_gh_found_outside_path(self):  # opened from Finder, PATH lacks Homebrew
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("os.access", side_effect=lambda p, _: p == "/usr/local/bin/gh"):
            self.assertEqual(ca.find_gh(), "/usr/local/bin/gh")
        with mock.patch("shutil.which", return_value=None), mock.patch("os.access", return_value=False):
            self.assertEqual(ca.find_gh(), "gh")  # so the error names it
        with mock.patch("shutil.which", return_value="bin/gh"):
            self.assertEqual(ca.find_gh(), os.path.abspath("bin/gh"))  # PATH wins, made absolute

    def test_version(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ca.parse(["--version"])
        self.assertEqual(out.getvalue().strip(), f"prherd {ca.__version__}")

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
                                                  "merging": True, "geometry": None, "appearance": "system",
                                                  "view": "all", "sidebar": 232, "log": True})
            saved = {"repos": [REPO], "mode": "admin", "interval": 5, "merging": False, "geometry": "800x600+0+0",
                     "appearance": "dark", "view": "attention", "sidebar": 300, "log": False}
            ca.save_settings(saved)
            self.assertEqual(ca.load_settings(), saved)

    def test_bad_settings_are_not_trusted_or_overwritten(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/s.json"):
            pathlib.Path(ca.CONFIG).write_text('{"mode": "foo", "interval": 0, "repos": [1, "a/b"], "merging": "no", '
                                               '"appearance": "blue"}')
            s = ca.load_settings()
            self.assertEqual((s["mode"], s["interval"], s["repos"]), ("", 60, ["a/b"]))  # "foo" would force-merge
            self.assertEqual(s["appearance"], "system")
            for bad in ("Dark", 1, None, ["dark"]):
                pathlib.Path(ca.CONFIG).write_text(json.dumps({"appearance": bad}))
                self.assertEqual(ca.load_settings()["appearance"], "system")
            pathlib.Path(ca.CONFIG).write_text("{oops")
            with self.assertRaises(SystemExit):
                ca.load_settings()
            self.assertEqual(pathlib.Path(ca.CONFIG).read_text(), "{oops")

    def test_settings_fall_back_to_pre_rename_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/prherd.json"):
            pathlib.Path(f"{d}/codex-automerge.json").write_text('{"repos": ["a/b"]}')
            self.assertEqual(ca.load_settings()["repos"], ["a/b"])
            self.assertFalse(pathlib.Path(f"{d}/codex-automerge.json").exists())  # moved, can't resurface
            pathlib.Path(ca.CONFIG).unlink()
            self.assertEqual(ca.load_settings()["repos"], [])

    def test_procfile(self):
        procfile = pathlib.Path(__file__).with_name("Procfile").read_text()
        self.assertIn("prherd: ./prherd.py --headless --admin owner/repo", procfile)

    def test_row_values_head_and_age(self):
        pr = {"isDraft": False, "autoMergeRequest": None, "author": None, "headRefOid": HEAD, "updatedAt": PUSHED}
        now = time.mktime(time.strptime("2026-10-26 14:17:58 UTC", "%Y-%m-%d %H:%M:%S %Z")) - time.timezone
        self.assertEqual(ca.row_values(pr, "👍", now)[3:], ("bfd4fae", "3h"))
        self.assertEqual(ca.row_values(dict(pr, headRefOid=None, updatedAt=None), "👍", now)[3:], ("", ""))

    def test_age(self):
        now = 1_800_000_000
        for seconds, shown in ((30, "now"), (120, "2m"), (3 * 3600, "3h"), (2 * 86400, "2d"), (21 * 86400, "3w"),
                               (800 * 86400, "2y"), (-300, "now")):  # the last: GitHub's clock ahead of this one
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - seconds))
            self.assertEqual(ca.age(iso, now), shown, iso)
        for unreadable in (None, "", "yesterday", 5):
            self.assertEqual(ca.age(unreadable, now), "")

    def test_short_drops_only_github_com(self):
        self.assertEqual(ca.short(REPO), "octo/direct-repo")
        self.assertEqual(ca.short("github.example/acme/local-repo"), "github.example/acme/local-repo")

    def test_matches_views_and_search(self):
        pr = {"number": 214, "title": "Checkout: remember the address", "headRefName": "feat/remember",
              "author": {"login": "mira-k"}}
        shown = {view: [v for v in ca.VERDICTS if ca.matches(pr, v, view, "")] for view in ca.VIEWS}
        self.assertEqual(shown, {"all": list(ca.VERDICTS), "ready": ["👍", "no major issues"], "reviewing": ["👀"],
                                 "attention": ["merge conflict", "?"], "waiting": [None], "held": ["don't merge"]})
        for query in ("checkout", "REMEMBER", "feat/", "mira", "214", "#214", "mira  checkout", " "):
            self.assertTrue(ca.matches(pr, "👍", "all", query), query)
        for query in ("cart", "#215", "mira cart"):  # every word has to be there
            self.assertFalse(ca.matches(pr, "👍", "all", query), query)
        self.assertFalse(ca.matches(pr, "👀", "ready", "checkout"))  # in the search, not in the view
        # As the fake gh lists a PR: no branch. And a deleted account's PR has no author.
        self.assertTrue(ca.matches({"number": 7, "title": "t", "author": None}, None, "waiting", "#7 t"))

    def test_kind_of(self):
        kinds = lambda log: [ca.kind_of(line) for line in log.splitlines()]
        self.assertEqual(kinds(run(FakeGh(reactions=THUMBS))[1]), ["poll", "codex", "merge"])
        self.assertEqual(kinds(run(FakeGh(reactions=[dict(THUMBS[0], content="eyes")]))[1]), ["poll", "review"])
        self.assertEqual(kinds(run(FakeGh())[1]), ["poll", "poll"])
        self.assertEqual(kinds(run(FakeGh(labels=["do-not-merge"], auto={"enabledAt": AFTER}))[1]),
                         ["poll", "poll", "merge"])
        self.assertEqual(kinds(run(FakeGh(mergeable="CONFLICTING"))[1]), ["poll", "conflict"])
        self.assertEqual(kinds(run(FakeGh(mergeable="CONFLICTING", fail=["pr comment"]))[1]), ["poll", "error"])
        self.assertEqual(kinds(run(FakeGh(fail=["api"]))[1]), ["poll", "error"])
        self.assertEqual(kinds(run(FakeGh(fail=["pr list"]))[1]), ["error"])
        self.assertEqual(kinds(run(FakeGh(reactions=THUMBS, merge=(1, "nope")))[1]), ["poll", "codex", "error"])
        for msg, kind in (("asked Codex for a review", "review"), ("review request FAILED: boom", "error"),
                          ("merged now (force)", "merge"), ("not merged — enabled auto-merge", "merge"),
                          ("can't add octo/x: boom", "error"), ("poll crashed; retrying next interval", "error"),
                          ("marked don't merge", "info"), ("branch updated", "info"), ("opened in browser", "info")):
            self.assertEqual(ca.kind_of(msg), kind, msg)

    def test_split_log(self):
        self.assertEqual(ca.split_log(f"  {ROW}: marked don't merge; skipping"),
                         ("octo/direct-repo#7", "marked don't merge; skipping"))
        self.assertEqual(ca.split_log(f"{REPO}: failed to list open PRs; retrying (HTTP 502: boom)"),
                         ("octo/direct-repo", "failed to list open PRs; retrying (HTTP 502: boom)"))
        self.assertEqual(ca.split_log("ghe.example/a/b#3: branch updated"), ("ghe.example/a/b#3", "branch updated"))
        for msg in ("poll crashed; retrying next interval (boom)", "can't add octo/x: boom"):  # about no one PR
            self.assertEqual(ca.split_log(msg), ("", msg))

    def test_whoami(self):
        answer = lambda out: lambda *args, cwd=None: subprocess.CompletedProcess(args, 0, out, "")
        with mock.patch.object(ca, "gh", answer(json.dumps(ME))):
            self.assertEqual(ca.whoami(), "watcher")
        for unusable in (answer("[]"), answer(""), answer('{"message": "Bad credentials"}'), FakeGh(fail=["api"])):
            with mock.patch.object(ca, "gh", unusable):
                self.assertEqual(ca.whoami(), "")

    def test_every_look_can_be_drawn(self):
        # Each verdict, view and log kind names an icon and colors: all of them exist, in both palettes.
        for look in (*ca.VERDICTS.values(), *ca.VIEWS.values()):
            self.assertIn(look["icon"], ca.ICONS)
        for palette in ca.PALETTE.values():
            self.assertLessEqual({view["color"] for view in ca.VIEWS.values()}, set(palette))
            for tone in [look["tone"] for look in ca.VERDICTS.values()] + [tone for _, tone, _ in ca.KINDS]:
                self.assertLessEqual({f"{tone}-fg", f"{tone}-bg"}, set(palette))
        for name, shapes in ca.ICONS.items():
            png = ca.draw((("#4F46E5", 2, shapes),), 16, 16)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n", name)
            self.assertEqual(png[16:24], (16).to_bytes(4, "big") * 2, name)  # its width and height
        self.assertEqual(ca.mix("#000000", "#FFFFFF", 0.5), "#808080")

    def test_layout_settings_round_trip_and_fall_back(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/s.json"):
            layout = lambda: {key: ca.load_settings()[key] for key in ("view", "sidebar", "log")}
            ca.save_settings({"view": "attention", "sidebar": 300, "log": False})
            self.assertEqual(layout(), {"view": "attention", "sidebar": 300, "log": False})
            for bad in ({"view": "everything"}, {"view": ["all"]}, {"view": None}, {"sidebar": 20}, {"sidebar": 9000},
                        {"sidebar": "300"}, {"sidebar": True}, {"sidebar": 300.5}, {"log": "no"}, {"log": None}):
                pathlib.Path(ca.CONFIG).write_text(json.dumps(bad))
                self.assertEqual(layout(), {"view": "all", "sidebar": 232, "log": True}, bad)

    def test_window_view_and_search_filter_the_tree(self):
        conflicting = {"number": 8, "title": "Cart badge", "headRefName": "fix/cart", "mergeable": "CONFLICTING"}
        other = f"{REPO}#8"

        def check(root, tree, buttons, wait_for):
            app = root.app
            listed = lambda: list(tree.get_children(REPO)) if tree.exists(REPO) else []
            counts = lambda: {key: str(row.count.cget("text")) for key, row in app.view_rows.items()}
            wait_for("Thumbs up")
            self.assertEqual(listed(), [ROW, other])
            self.assertEqual(tree.set(other, "codex"), "Merge conflict")
            self.assertEqual(counts(), {"all": "2", "ready": "1", "reviewing": "0", "attention": "1", "waiting": "0",
                                        "held": "0"})
            app.pick_view("attention")
            self.assertEqual(listed(), [other])
            app.pick_view("held")
            self.assertEqual(listed(), [])  # a repo with nothing in the view isn't listed either
            app.pick_view("all")
            for query, found in (("CART", [other]), ("fix/", [other]), ("#7", [ROW]), ("octocat", [ROW, other]),
                                 ("nothing like it", []), ("", [ROW, other])):
                app.query.set(query)
                self.assertEqual(listed(), found, query)
            app.pick_view("ready")
            app.query.set("cart")  # both at once: the conflicting PR isn't ready
            self.assertEqual(listed(), [])
            self.assertEqual(counts()["attention"], "1")  # the counts are of the views, whatever the search

        self.window(FakeGh(reactions=THUMBS, more=[conflicting]), check)

    def test_window_selection_bar_and_countdown(self):
        other = f"{REPO}#8"

        def check(root, tree, buttons, wait_for):
            app = root.app
            wait_for("Thumbs up")
            root.update()
            self.assertRegex(app.poll_button.cget("text"), r"^Poll now · \d+[sm]$")
            tree.selection_set(ROW)
            root.update()
            self.assertFalse(app.selection_bar.winfo_ismapped())  # one PR: the right-click menu has its actions
            tree.selection_set([ROW, other])
            root.update()
            self.assertTrue(app.selection_bar.winfo_ismapped())
            self.assertEqual(app.selected.cget("text"), "2 selected")
            offered = {text for text, button in buttons.items() if button.master is app.selection_bar
                       and button.winfo_ismapped()}  # with "": the button that clears the selection has no text
            self.assertEqual(offered, {"Request Codex review", "Don't merge", "Update branch", "Merge now…", ""})

        self.window(FakeGh(reactions=THUMBS, more=[{"number": 8}]), check)


if __name__ == "__main__":
    unittest.main()
