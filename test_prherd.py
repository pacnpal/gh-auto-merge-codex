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

    def answering(self, answers):
        """A gh for calls FakeGh has no answer to: {words in the call: (exit code, output)}, first match wins."""
        def fake(*args, cwd=None):
            fake.calls.append(args)
            code, out = next((a for words, a in answers.items() if words in " ".join(args)), (0, ""))
            return subprocess.CompletedProcess(args, code, out, "")

        fake.calls = []
        return fake

    def test_verdict_detail_times(self):
        pr, mid = {"number": 7, "headRefOid": HEAD}, "2026-10-26T11:20:00Z"  # mid: after the push, before AFTER

        def detail(**gh):
            with mock.patch.object(ca, "gh", FakeGh(**gh)):
                d = ca.verdict_detail(REPO, pr)
                self.assertEqual(ca.verdict(REPO, pr), d["verdict"])
            return d

        eyes = dict(THUMBS[0], content="eyes", created_at=mid)
        self.assertEqual(detail(reactions=THUMBS), {"verdict": "👍", "pushed": PUSHED, "cleared_at": AFTER,
                                                    "reviewing_at": None})
        self.assertEqual(detail(reactions=[eyes, *THUMBS]), {"verdict": "👍", "pushed": PUSHED, "cleared_at": AFTER,
                                                             "reviewing_at": mid})
        # The receipt that counted gives the time, not an earlier one for another commit.
        self.assertEqual(detail(reactions=[eyes], comments=[receipt("0123456789", at=mid), receipt()]),
                         {"verdict": "no major issues", "pushed": PUSHED, "cleared_at": AFTER, "reviewing_at": mid})
        self.assertEqual(detail(reactions=[eyes]), {"verdict": "👀", "pushed": PUSHED, "cleared_at": None,
                                                    "reviewing_at": mid})
        # Signals from before this head arrived give no times, as they give no verdict.
        stale = [dict(THUMBS[0], created_at=BEFORE), dict(eyes, created_at=BEFORE)]
        self.assertEqual(detail(reactions=stale, comments=[receipt(at=BEFORE)]),
                         {"verdict": None, "pushed": PUSHED, "cleared_at": None, "reviewing_at": None})
        self.assertEqual(detail(reactions=THUMBS, comments=[receipt()], suites=()),
                         {"verdict": None, "pushed": None, "cleared_at": None, "reviewing_at": None})

    def test_poll_stores_details_for_looked_up_prs(self):
        held = {"number": 8, "labels": [{"name": "do-not-merge"}]}
        gh = lambda: FakeGh(reactions=THUMBS, more=[held, {"number": 9, "mergeable": "CONFLICTING"}])
        plain, plain_log = run(gh())
        details, logs = {}, []
        with mock.patch.object(ca, "gh", gh()):
            rows = ca.poll(REPO, logs.append, lambda: "", details)
        self.assertEqual((rows, "\n".join(logs)), (plain, plain_log))  # asking for details changes nothing else
        self.assertEqual(details, {7: {"verdict": "👍", "pushed": PUSHED, "cleared_at": AFTER, "reviewing_at": None}})
        details = {}
        with mock.patch.object(ca, "gh", FakeGh(fail=["api"])):
            rows = ca.poll(REPO, lambda _: None, lambda: None, details)
        self.assertEqual((rows[0][1], details), ("?", {}))
        details = {}
        with mock.patch.object(ca, "gh", FakeGh()):  # looked up, with nothing found: still stored
            ca.poll(REPO, lambda _: None, lambda: None, details)
        self.assertEqual(details, {7: {"verdict": None, "pushed": PUSHED, "cleared_at": None, "reviewing_at": None}})

    def test_readiness_for_every_verdict(self):
        pr = {"number": 7, "headRefOid": HEAD, "baseRefName": "main", "mergeable": "MERGEABLE", "isDraft": False,
              "autoMergeRequest": None}
        go = {"👍": ("ok", "Codex go-ahead on the current head", "Newer than bfd4fae"),
              "no major issues": ("ok", "Codex go-ahead on the current head", "Receipt names bfd4fae"),
              "👀": ("warn", "Codex is reviewing", "Its verdict lands on the next poll"),
              "merge conflict": ("warn", "Not checked while conflicting", "Resolve the conflict first"),
              "?": ("err", "Couldn't read Codex's verdict", "GitHub returned an error; retrying"),
              "don't merge": ("warn", "Not checked while held", "Allow merge to check again"),
              None: ("warn", "No go-ahead yet", "Request a Codex review to get one")}
        self.assertEqual(set(go), set(ca.VERDICTS))
        tones = {v["tone"] for v in ca.VERDICTS.values()}
        for v, first in go.items():
            items = ca.readiness(pr, v)
            self.assertEqual((len(items), items[0]), (5, first), v)
            self.assertLessEqual({tone for tone, _, _ in items}, tones)
            conflict, label, draft, queue = items[1:]
            clear = ("ok", "No conflicts with main", "Mergeable as of the last poll")
            self.assertEqual(conflict, ("bad", "Conflicts with main", "Commented once on bfd4fae")
                             if v == "merge conflict" else clear)
            self.assertEqual(label, ("bad", "Labeled do-not-merge", "Allow merge to release it")
                             if v == "don't merge" else ("ok", "Not labeled do-not-merge", ""))
            self.assertEqual(draft, ("ok", "Ready for review", "Not a draft"))
            self.assertEqual(queue[:2], ("warn", "Not queued yet") if v in ("👍", "no major issues")
                             else ("none", "Not queued"))
        self.assertEqual(ca.readiness(dict(pr, isDraft=True), None)[3],
                         ("warn", "Draft pull request", "Mark ready for review on GitHub"))
        queued = dict(pr, autoMergeRequest={"enabledAt": AFTER})
        self.assertEqual(ca.readiness(queued, "👍")[4][:2], ("ok", "Auto-merge queued"))
        self.assertEqual(ca.readiness(queued, "don't merge")[4][:2], ("none", "Not queued"))  # the poll cancels it
        self.assertEqual(ca.readiness(dict(pr, autoMergeRequest={}), "👍")[4][:2], ("warn", "Not queued yet"))
        # Held and conflicting, and GitHub still working out whether it conflicts.
        self.assertEqual(ca.readiness(dict(pr, mergeable="CONFLICTING"), "don't merge")[1],
                         ("bad", "Conflicts with main", "Not commented on while held"))
        self.assertEqual(ca.readiness(dict(pr, mergeable="UNKNOWN"), "👍")[1][:2],
                         ("warn", "Checking for conflicts with main"))
        self.assertEqual(len(ca.readiness({"number": 7}, None)), 5)  # a PR with no other fields

    def test_headline_for_every_verdict(self):
        pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "autoMergeRequest": None}
        queued, draft = dict(pr, autoMergeRequest={"enabledAt": AFTER}), dict(pr, isDraft=True)
        titles = {"don't merge": ("hold", "Held — won't merge"),
                  "merge conflict": ("bad", "Blocked by a merge conflict"),
                  "?": ("err", "Couldn't read Codex's verdict"), "👀": ("warn", "Codex is reviewing"),
                  None: ("none", "Waiting for Codex"), "👍": ("ok", "Cleared — merges on the next poll"),
                  "no major issues": ("ok", "Cleared — merges on the next poll")}
        self.assertEqual(set(titles), set(ca.VERDICTS))
        for v, expected in titles.items():
            tone, title, detail = ca.headline(pr, v)
            self.assertEqual((tone, title), expected)
            self.assertEqual(tone, ca.VERDICTS[v]["tone"])  # the card matches the row's pill
            self.assertTrue(detail)
        for v in ("👍", "no major issues"):
            self.assertEqual(ca.headline(queued, v)[:2], ("ok", "Cleared — queued to merge"))
            self.assertIn("queued at bfd4fae", ca.headline(queued, v)[2])
            self.assertEqual(ca.headline(pr, v, merging=False)[:2], ("ok", "Cleared — watching only"))
            # Queued on GitHub merges whatever the switch says; a draft merges nowhere.
            self.assertEqual(ca.headline(queued, v, merging=False)[1], "Cleared — queued to merge")
            self.assertEqual(ca.headline(dict(queued, isDraft=True), v)[:2], ("warn", "Draft — not merging yet"))
        self.assertEqual(ca.headline(draft, None)[:2], ("none", "Draft — not merging yet"))
        self.assertIn("No go-ahead newer than bfd4fae", ca.headline(pr, None)[2])
        self.assertNotEqual(ca.headline(pr, "👀")[2], ca.headline(pr, "👀", merging=False)[2])
        # A held or conflicting PR says so, queued or draft or not.
        self.assertEqual(ca.headline(dict(queued, isDraft=True), "don't merge")[1], "Held — won't merge")
        self.assertEqual(ca.headline(queued, "merge conflict")[1], "Blocked by a merge conflict")
        self.assertEqual(ca.headline({"number": 7}, "👍")[1], "Cleared — merges on the next poll")

    def test_timeline_is_in_time_order(self):
        mid, late = "2026-10-26T11:20:00Z", "2026-10-26T11:30:00Z"
        pr = {"number": 7, "headRefOid": HEAD, "autoMergeRequest": {"enabledAt": late}}
        detail = {"verdict": "👍", "pushed": PUSHED, "cleared_at": AFTER, "reviewing_at": mid}
        self.assertEqual(ca.timeline(pr, "👍", detail), [
            (PUSHED, "none", "Head bfd4fae pushed"), (mid, "warn", "Codex started reviewing"),
            (AFTER, "ok", "Codex gave a thumbs-up"), (late, "acc", "Auto-merge queued behind checks")])
        self.assertEqual(ca.timeline(pr, "no major issues", dict(detail, verdict="no major issues"))[2],
                         (AFTER, "ok", "Codex: no major issues on bfd4fae"))
        # Auto-merge left queued from an earlier head comes first: order is by time, not by kind.
        early = dict(pr, autoMergeRequest={"enabledAt": BEFORE})
        reviewing = ca.timeline(early, "👀", dict(detail, verdict="👀", cleared_at=None))
        self.assertEqual([text for _, _, text in reviewing],
                         ["Auto-merge queued behind checks", "Head bfd4fae pushed", "Codex started reviewing"])
        # What isn't known is left out: no detail for a held, conflicting or failed PR.
        self.assertEqual(ca.timeline(pr, "don't merge", None), [(late, "acc", "Auto-merge queued behind checks")])
        self.assertEqual(ca.timeline({"number": 7, "headRefOid": HEAD, "autoMergeRequest": None}, "?", None), [])
        self.assertEqual(ca.timeline({"number": 7}, None, {"verdict": None, "pushed": None, "cleared_at": None,
                                                           "reviewing_at": None}), [])

    def test_gh_version(self):
        real = "gh version 2.63.2 (2025-01-10)\nhttps://github.com/cli/cli/releases/tag/v2.63.2\n"
        self.assertEqual(ca.gh_version(real), (2, 63, 2))
        self.assertEqual(ca.gh_version("gh version 2.62.0 (demo)"), (2, 62, 0))
        self.assertEqual(ca.gh_version("gh version 2.48"), (2, 48))
        self.assertEqual(ca.gh_version("gh version 2.90.0-rc.1 (2026-01-01)"), (2, 90, 0))
        for junk in ("", None, "git version 2.47.0", "gh: command not found"):
            self.assertIsNone(ca.gh_version(junk))
        self.assertGreaterEqual((2, 48, 0), ca.GH_MIN)
        self.assertLess((2, 47, 9), ca.GH_MIN)

    def test_setup_checks(self):
        signed_in = "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n  - Active account: true\n"

        def checks(answers):
            fake = self.answering(answers)
            with mock.patch.object(ca, "gh", fake):
                return ca.setup_checks(), fake.calls

        result, calls = checks({"--version": (0, "gh version 2.63.2 (2025-01-10)\n"), "auth status": (0, signed_in)})
        self.assertEqual(result, [(True, "GitHub CLI installed", "gh 2.63.2"), (True, "Signed in", "octocat")])
        self.assertEqual(calls, [("--version",), ("auth", "status")])
        result, _ = checks({"--version": (0, "gh version 2.48.0 (2024-04-17)\n"), "auth status": (0, "")})
        self.assertEqual(result, [(True, "GitHub CLI installed", "gh 2.48.0"), (True, "Signed in", "")])
        result, _ = checks({"--version": (0, "gh version 2.47.0 (2024-04-03)\n"),
                            "auth status": (1, "You are not logged into any GitHub hosts.")})
        self.assertEqual(result, [(False, "GitHub CLI installed", "gh 2.47.0 is too old; prherd needs 2.48 or newer"),
                                  (False, "Signed in", "run `gh auth login` in a terminal")])
        result, _ = checks({"--version": (0, "something else"), "auth status": (0, signed_in)})
        self.assertEqual(result[0], (False, "GitHub CLI installed", "couldn't read gh's version"))
        result, _ = checks({"--version": (1, "gh version 2.63.2")})  # e.g. timed out
        self.assertFalse(result[0][0])
        missing = [(False, "GitHub CLI installed", "gh not found; install it from cli.github.com"),
                   (False, "Signed in", "needs gh")]
        with mock.patch.object(ca, "gh", side_effect=FileNotFoundError(2, "No such file or directory", "gh")):
            self.assertEqual(ca.setup_checks(), missing)
        with mock.patch.object(ca, "GH", os.path.join(tempfile.gettempdir(), "no-such-dir", "gh")):  # the real helper
            self.assertEqual(ca.setup_checks(), missing)

    def test_pr_state(self):
        merged = {"state": "MERGED", "mergedAt": AFTER, "title": "t", "number": 7}
        fake = self.answering({"pr view": (0, json.dumps(merged))})
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.pr_state(REPO, 7), merged)
        self.assertEqual(fake.calls, [("pr", "view", "7", "-R", REPO, "--json", "state,mergedAt,title,number")])
        for broken in ((1, "no pull requests found"), (0, "not json"), (0, "[]")):
            with mock.patch.object(ca, "gh", self.answering({"pr view": broken})):
                self.assertIsNone(ca.pr_state(REPO, 7))
        with mock.patch.object(ca, "gh", side_effect=FileNotFoundError("gh")):
            self.assertIsNone(ca.pr_state(REPO, 7))

    def notice_prs(self, verdicts):
        """{number: verdict} -> poll()-shaped rows, and the same as a `before`."""
        rows = [({"number": n, "title": f"title {n}"}, v) for n, v in verdicts.items()]
        return rows, {pr["number"]: (pr, v) for pr, v in rows}

    def test_notices_nothing_on_first_poll_or_first_sight(self):
        rows, seen = self.notice_prs({7: "👍", 8: "merge conflict", 9: "?"})
        no_state = mock.Mock(side_effect=AssertionError("nothing left the list"))
        streaks = {}
        self.assertEqual(ca.notices(REPO, None, rows, streaks, no_state), [])  # launch: no burst
        self.assertEqual(streaks, {(REPO, 9): 1})  # but the failure counts towards two in a row
        self.assertEqual(ca.notices(REPO, {}, rows, {}, no_state), [])  # PRs opened since the last poll
        self.assertEqual(ca.notices(REPO, seen, rows, {}, no_state), [])  # nothing changed
        self.assertEqual(ca.notices(REPO, seen, None, {}, no_state), [])  # the listing failed: not "all gone"

    def test_notices_conflict_and_cleared(self):
        no_state = mock.Mock(side_effect=AssertionError("nothing left the list"))
        _, before = self.notice_prs({7: None, 8: "👀", 9: "👍", 10: "merge conflict", 11: "don't merge", 12: "?"})
        rows, _ = self.notice_prs({7: "merge conflict", 8: "👍", 9: "no major issues", 10: "merge conflict",
                                   11: "no major issues", 12: "merge conflict"})
        self.assertEqual(ca.notices(REPO, before, rows, {}, no_state), [
            ("conflict", "Merge conflict on #7", "octo/direct-repo · title 7"),
            ("cleared", "Codex cleared #8", "octo/direct-repo · title 8"),  # 9 and 10 were already there
            ("cleared", "Codex cleared #11", "octo/direct-repo · title 11"),
            ("conflict", "Merge conflict on #12", "octo/direct-repo · title 12")])
        # Leaving those states is quiet.
        rows, _ = self.notice_prs({7: None, 8: "👀", 9: "don't merge", 10: None, 11: "👀", 12: None})
        _, before = self.notice_prs({7: "merge conflict", 8: "👍", 9: "👍", 10: "merge conflict", 11: "👍",
                                     12: "👍"})
        self.assertEqual(ca.notices(REPO, before, rows, {}, no_state), [])
        # Another host is named; github.com isn't.
        rows, _ = self.notice_prs({7: "👍"})
        _, before = self.notice_prs({7: None})
        self.assertEqual(ca.notices("github.example/acme/local-repo", before, rows, {}, no_state),
                         [("cleared", "Codex cleared #7", "github.example/acme/local-repo · title 7")])

    def test_notices_failed_lookup_after_two_polls_in_a_row(self):
        failed, seen = self.notice_prs({7: "?"})
        fine, _ = self.notice_prs({7: None})
        streaks = {}
        kinds = lambda rows: [kind for kind, _, _ in ca.notices(REPO, seen, rows, streaks)]
        self.assertEqual(kinds(failed), [])  # one failure is a blip
        self.assertEqual(ca.notices(REPO, seen, failed, streaks),
                         [("failed", "Lookup failed for #7", "octo/direct-repo · title 7")])
        self.assertEqual(kinds(failed), [])  # the third in a row: already said
        self.assertEqual(streaks, {(REPO, 7): 3})
        self.assertEqual(kinds(fine), [])
        self.assertEqual(streaks, {})  # recovered: the count starts over
        # Never two in a row.
        self.assertEqual([kinds(failed), kinds(fine), kinds(failed), kinds(fine)], [[], [], [], []])
        self.assertEqual([kinds(failed), kinds(failed)], [[], ["failed"]])
        # Counted per repo, and dropped with a PR that leaves the list.
        other = "github.com/octo/other"
        self.assertEqual(ca.notices(other, seen, failed, streaks), [])
        self.assertEqual(streaks, {(REPO, 7): 2, (other, 7): 1})
        self.assertEqual(ca.notices(other, seen, [], streaks, lambda repo, number: None), [])
        self.assertEqual(streaks, {(REPO, 7): 2})

    def test_notices_merged_but_not_closed(self):
        _, before = self.notice_prs({7: "👍", 8: None, 9: "👀", 10: None})
        rows, _ = self.notice_prs({10: None})
        states = {7: {"state": "MERGED", "mergedAt": AFTER, "title": "renamed since", "number": 7},
                  8: {"state": "CLOSED", "mergedAt": None, "title": "title 8", "number": 8},
                  9: None}  # the lookup failed
        asked = []
        state = lambda repo, number: asked.append((repo, number)) or states[number]
        self.assertEqual(ca.notices(REPO, before, rows, {}, state),
                         [("merged", "Merged #7", "octo/direct-repo · renamed since")])
        self.assertEqual(asked, [(REPO, 7), (REPO, 8), (REPO, 9)])  # only the PRs that left the list
        # By default it asks gh, and falls back on the title it last saw.
        fake = self.answering({"pr view": (0, json.dumps({"state": "MERGED", "mergedAt": AFTER, "number": 7}))})
        with mock.patch.object(ca, "gh", fake):
            self.assertEqual(ca.notices(REPO, {7: before[7]}, [], {}),
                             [("merged", "Merged #7", "octo/direct-repo · title 7")])
        self.assertEqual(fake.calls[0][:5], ("pr", "view", "7", "-R", REPO))

    def test_checked_notify(self):
        on = dict.fromkeys(ca.NOTIFY_KINDS, True)
        self.assertEqual(ca.NOTIFY_KINDS, ("merged", "conflict", "failed", "cleared"))
        for junk in (None, [], "no", 0, {}, {"other": False}):
            self.assertEqual(ca.checked_notify(junk), on)
        self.assertEqual(ca.checked_notify({"merged": False, "cleared": False, "extra": False}),
                         dict(on, merged=False, cleared=False))
        self.assertEqual(ca.checked_notify({"merged": 0, "conflict": "false", "failed": None}), on)  # only real bools

    def test_notify_passes_text_as_arguments_on_macos(self):
        title, body = 'Merged #7"; do shell script "id', "octo/direct-repo · $(id) `id` \\"
        with mock.patch.object(ca.sys, "platform", "darwin"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as spawn:
            self.assertIs(ca.notify(title, body), True)
        argv = spawn.call_args.args[0]
        # -- ends osascript's options, so a title starting with "-" is still text.
        self.assertEqual(argv, ["/usr/bin/osascript", "-e", ca.NOTIFY_SCRIPT, "--", title, body])
        self.assertEqual(ca.NOTIFY_SCRIPT,
                         "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run")
        how = spawn.call_args.kwargs
        self.assertNotIn("shell", how)  # an argv, never a command line
        self.assertEqual((how["close_fds"], how["creationflags"], bool(how["timeout"])), (False, ca.NO_WINDOW, True))

    def test_notify_never_raises_and_says_when_it_cannot(self):
        with mock.patch.object(ca.sys, "platform", "darwin"):
            with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "nope")):
                self.assertIs(ca.notify("t", "b"), False)
            for error in (FileNotFoundError("osascript"), subprocess.TimeoutExpired("osascript", 10),
                          ValueError("embedded null byte")):
                with mock.patch("subprocess.run", side_effect=error):
                    self.assertIs(ca.notify("t", "b\0"), False)
        for platform in ("linux", "win32"):  # no notify-send; Windows has nothing to call
            with mock.patch.object(ca.sys, "platform", platform), mock.patch("shutil.which", return_value=None), \
                    mock.patch("subprocess.run") as spawn:
                self.assertIs(ca.notify("t", "b"), False)
            spawn.assert_not_called()
        with mock.patch.object(ca.sys, "platform", "linux"), \
                mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as spawn:
            self.assertIs(ca.notify("--help", "<b>body</b>"), True)
        self.assertEqual(spawn.call_args.args[0],
                         ["/usr/bin/notify-send", "--app-name=prherd", "--", "--help", "<b>body</b>"])

    def login_home(self, platform, **env):
        """A throwaway home on the given platform, so no test touches the real login items."""
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        # expanduser reads HOME, or USERPROFILE when the tests themselves run on Windows.
        for patch in (mock.patch.object(ca.sys, "platform", platform),
                      mock.patch.dict(os.environ, {"HOME": home.name, "USERPROFILE": home.name, **env})):
            patch.start()
            self.addCleanup(patch.stop)
        if "XDG_CONFIG_HOME" not in env:
            os.environ.pop("XDG_CONFIG_HOME", None)
        return home.name

    def test_launch_command(self):
        with mock.patch.object(ca.sys, "frozen", True, create=True), \
                mock.patch.object(ca.sys, "executable", "/Apps/prherd"):
            self.assertEqual(ca.launch_command(), ["/Apps/prherd"])
        source = pathlib.Path(ca.__file__).resolve()
        command = ca.launch_command()
        self.assertEqual((command[0], pathlib.Path(command[1]).resolve()), (ca.sys.executable, source))
        self.assertTrue(os.path.isabs(command[1]))

    def test_login_item_on_macos(self):
        import plistlib
        d = self.login_home("darwin")
        path = os.path.join(d, "Library", "LaunchAgents", "com.pacnpal.prherd.plist")
        self.assertEqual(ca.login_item_path(), path)
        self.assertFalse(ca.login_item_enabled())
        self.assertEqual(ca.login_item(False), "no longer opens at login")  # already off, and no folder yet
        with mock.patch.object(ca, "launch_command", return_value=["/Apps/pr herd/prherd", "it's \"quoted\""]):
            self.assertEqual(ca.login_item(True), "opens at login")
            self.assertEqual(ca.login_item(True), "opens at login")  # again: rewritten, not an error
        self.assertTrue(ca.login_item_enabled())
        with open(path, "rb") as f:
            self.assertEqual(plistlib.load(f), {"Label": "com.pacnpal.prherd", "RunAtLoad": True,
                                                "ProgramArguments": ["/Apps/pr herd/prherd", "it's \"quoted\""]})
        self.assertEqual(ca.login_item(False), "no longer opens at login")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(ca.login_item_enabled())

    def test_login_item_on_linux(self):
        d = self.login_home("linux")
        path = os.path.join(d, ".config", "autostart", "prherd.desktop")
        self.assertEqual(ca.login_item_path(), path)
        self.assertFalse(ca.login_item_enabled())
        with mock.patch.object(ca, "launch_command", return_value=["/usr/bin/python3", "/home/o'neil/pr herd/100%.py"]):
            self.assertEqual(ca.login_item(True), "opens at login")
        self.assertTrue(ca.login_item_enabled())
        self.assertEqual(pathlib.Path(path).read_text(encoding="utf-8"),
                         "[Desktop Entry]\nType=Application\nName=prherd\n"
                         "Exec=\"/usr/bin/python3\" \"/home/o'neil/pr herd/100%%.py\"\nTerminal=false\n")
        self.assertEqual(ca.login_item(False), "no longer opens at login")
        self.assertEqual((os.path.exists(path), ca.login_item_enabled()), (False, False))
        # The spec's two rounds of escaping: \" $ ` and \ inside the quotes, then every backslash doubled.
        self.assertEqual(ca.desktop_exec(['a"b', "$HOME", "`id`", "c\\d"]), r'"a\\"b" "\\$HOME" "\\`id\\`" "c\\\\d"')

    def test_login_item_follows_xdg_config_home(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        d = self.login_home("linux", XDG_CONFIG_HOME=os.path.join(elsewhere.name, "cfg"))
        path = os.path.join(elsewhere.name, "cfg", "autostart", "prherd.desktop")
        self.assertEqual(ca.login_item_path(), path)
        self.assertEqual(ca.login_item(True), "opens at login")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(d), [])  # nothing under the home's .config

    def test_login_item_failure_is_reported(self):
        d = self.login_home("darwin")
        pathlib.Path(d, "Library").write_text("a file where the folder should be")
        self.assertRegex(ca.login_item(True), r"^open at login FAILED: .+")
        self.assertFalse(ca.login_item_enabled())
        with mock.patch("os.remove", side_effect=PermissionError(13, "Permission denied")):
            self.assertRegex(ca.login_item(False), r"^turning off open at login FAILED: .*Permission denied")

    def test_login_item_on_windows(self):
        self.login_home("win32")
        winreg = mock.MagicMock(HKEY_CURRENT_USER="HKCU", KEY_SET_VALUE="set", REG_SZ="sz")
        key = winreg.OpenKey.return_value.__enter__.return_value
        with mock.patch.dict("sys.modules", winreg=winreg), \
                mock.patch.object(ca, "launch_command", return_value=[r"C:\Program Files\prherd\prherd.exe"]):
            self.assertIsNone(ca.login_item_path())
            self.assertEqual(ca.login_item(True), "opens at login")
            winreg.OpenKey.assert_called_with("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "set")
            winreg.SetValueEx.assert_called_once_with(key, "prherd", 0, "sz", r'"C:\Program Files\prherd\prherd.exe"')
            self.assertTrue(ca.login_item_enabled())
            winreg.QueryValueEx.assert_called_once_with(key, "prherd")
            self.assertEqual(ca.login_item(False), "no longer opens at login")
            winreg.DeleteValue.assert_called_once_with(key, "prherd")
            winreg.DeleteValue.side_effect = FileNotFoundError(2, "not there")  # already off
            self.assertEqual(ca.login_item(False), "no longer opens at login")
            winreg.QueryValueEx.side_effect = FileNotFoundError(2, "not there")
            self.assertFalse(ca.login_item_enabled())
            winreg.OpenKey.side_effect = PermissionError(13, "Access is denied")
            self.assertEqual(ca.login_item(True), "open at login FAILED: [Errno 13] Access is denied")

    def test_demo_gh_answers_the_new_calls(self):
        demo = str(pathlib.Path(__file__).with_name("scripts") / "demo-gh" / "gh")
        ask = lambda *args: subprocess.run([ca.sys.executable, demo, *args], capture_output=True, text=True,
                                           env=dict(os.environ, DEMO_GH_LOG=os.devnull))
        self.assertGreaterEqual(ca.gh_version(ask("--version").stdout), ca.GH_MIN)
        with mock.patch.object(ca, "gh", lambda *args, cwd=None: ask(*args)):
            self.assertEqual(ca.setup_checks(),
                             [(True, "GitHub CLI installed", "gh 2.62.0"), (True, "Signed in", "demo")])
            repo = "github.com/octo-org/storefront"
            self.assertEqual(ca.pr_state(repo, 214), {"state": "OPEN", "mergedAt": None, "number": 214,
                                                      "title": "Checkout: remember the last shipping address"})
            self.assertEqual(ca.pr_state(repo, 1)["state"], "MERGED")  # not in its open list

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
                                                  "view": "all", "sidebar": 232, "log": True,
                                                  "notify": dict.fromkeys(ca.NOTIFY_KINDS, True)})
            saved = {"repos": [REPO], "mode": "admin", "interval": 5, "merging": False, "geometry": "800x600+0+0",
                     "appearance": "dark", "view": "attention", "sidebar": 300, "log": False,
                     "notify": {"merged": True, "conflict": False, "failed": True, "cleared": False}}
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

    def test_notify_setting_round_trips_and_falls_back(self):
        on = dict.fromkeys(ca.NOTIFY_KINDS, True)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ca, "CONFIG", f"{d}/s.json"):
            ca.save_settings({"notify": dict(on, merged=False, cleared=False)})
            self.assertEqual(ca.load_settings()["notify"], dict(on, merged=False, cleared=False))
            for bad in ("all", None, 1, ["merged"], {"merged": "no"}, {"conflict": 0, "other": False}):
                pathlib.Path(ca.CONFIG).write_text(json.dumps({"mode": "admin", "repos": ["a/b"], "notify": bad}))
                s = ca.load_settings()
                self.assertEqual(s["notify"], on, bad)
                self.assertEqual((s["mode"], s["repos"]), ("admin", ["a/b"]), bad)  # the rest is kept

    def test_can_merge(self):
        def ask(code, out):
            fake = self.answering({"repo view": (code, out)})
            with mock.patch.object(ca, "gh", fake):
                return ca.can_merge(REPO), fake.calls

        answer, calls = ask(0, '{"viewerPermission": "ADMIN"}')
        self.assertIs(answer, True)
        self.assertEqual(calls, [("repo", "view", REPO, "--json", "viewerPermission")])
        for permission, can in (("MAINTAIN", True), ("WRITE", True), ("TRIAGE", False), ("READ", False)):
            self.assertIs(ask(0, json.dumps({"viewerPermission": permission}))[0], can, permission)
        # gh failed, or said something else: unknown, not "can't".
        for code, out in ((1, "GraphQL: Could not resolve to a Repository"), (0, "not json"), (0, "[]"), (0, "{}"),
                          (0, '{"viewerPermission": null}'), (0, '{"viewerPermission": ""}')):
            self.assertIsNone(ask(code, out)[0], out)
        with mock.patch.object(ca, "gh", side_effect=FileNotFoundError("gh")):
            self.assertIsNone(ca.can_merge(REPO))

    def test_demo_gh_answers_merge_rights(self):
        demo = str(pathlib.Path(__file__).with_name("scripts") / "demo-gh" / "gh")
        ask = lambda *args, cwd=None: subprocess.run([ca.sys.executable, demo, *args], capture_output=True, text=True,
                                                     env=dict(os.environ, DEMO_GH_LOG=os.devnull))
        with mock.patch.object(ca, "gh", ask):
            self.assertIs(ca.can_merge("github.com/octo-org/storefront"), True)
            self.assertIs(ca.can_merge("github.com/octo-org/docs"), False)
            self.assertEqual(ca.resolve("octo-org/docs"), "github.com/octo-org/docs")  # the plain view still answers

    def test_can_notify(self):
        with mock.patch.object(ca.sys, "platform", "darwin"), mock.patch("os.access", return_value=True) as access:
            self.assertTrue(ca.can_notify())
        access.assert_called_once_with("/usr/bin/osascript", os.X_OK)
        for platform in ("linux", "win32"):
            with mock.patch.object(ca.sys, "platform", platform):
                with mock.patch("shutil.which", return_value="/usr/bin/notify-send"):
                    self.assertTrue(ca.can_notify())
                with mock.patch("shutil.which", return_value=None):
                    self.assertFalse(ca.can_notify())

    def test_reveal(self):
        done = lambda code, err="": subprocess.CompletedProcess([], code, "", err)
        path = os.path.join(os.sep, "home", "me", ".config", "prherd.json")
        for platform, argv in (("darwin", ["/usr/bin/open", "-R", path]),
                               ("win32", ["explorer", f"/select,{os.path.normpath(path)}"]),
                               ("linux", ["xdg-open", os.path.dirname(path)])):
            with mock.patch.object(ca.sys, "platform", platform), \
                    mock.patch("subprocess.run", return_value=done(0)) as run:
                self.assertEqual(ca.reveal(path), "")
            self.assertEqual(run.call_args.args[0], argv)
        with mock.patch.object(ca.sys, "platform", "linux"):
            with mock.patch("subprocess.run", return_value=done(4, "no method available")):
                self.assertEqual(ca.reveal(path), f"can't show {path}: no method available")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file", "xdg-open")):
                self.assertIn("can't show", ca.reveal(path))
        with mock.patch.object(ca.sys, "platform", "win32"), mock.patch("subprocess.run", return_value=done(1)):
            self.assertEqual(ca.reveal(path), "")  # Explorer exits 1 when it worked

    def bare_window(self, fake, check):
        """window(), launched with no repos: none saved, none passed."""
        gui = ca.gui
        with mock.patch.object(ca, "gui", lambda targets, mode, interval: gui([], mode, interval)):
            self.window(fake, check)

    def until(self, root, done, what):
        """Pump the window until done() is true."""
        deadline = time.time() + 10
        while not done():
            self.assertLess(time.time(), deadline, f"never {what}")
            root.update()
            time.sleep(0.01)

    def kin(self, widget, kind):
        """Every widget of a kind under widget."""
        found = [child for child in widget.winfo_children() if isinstance(child, kind)]
        return found + [w for child in widget.winfo_children() for w in self.kin(child, kind)]

    def saved(self):
        return json.loads(pathlib.Path(ca.CONFIG).read_text())

    def test_first_run_shows_only_while_there_are_no_repos(self):
        import threading
        answer = threading.Event()

        def checks():
            answer.wait(10)
            return [(True, "GitHub CLI installed", "gh 2.63.2"),
                    (False, "Signed in", "run `gh auth login` in a terminal")]

        def check(root, tree, buttons, wait_for):
            app = root.app
            first = app.first_run
            up = lambda: (first.up, bool(first.screen.winfo_manager()), bool(app.table.winfo_manager()),
                          str(app.sidebar) in app.body.panes(), bool(app.heading.winfo_manager()))
            root.update()
            self.assertEqual(up(), (True, True, False, False, False))  # it, and none of what it stands in for
            # The setup checks: asked off the Tk thread, pending until they answer.
            rows = lambda: [(row.title.cget("text"), row.found.cget("text"), row.problem.winfo_manager())
                            for row in first.checks]
            self.assertEqual(rows(), [("GitHub CLI installed", "checking…", ""), ("Signed in", "checking…", "")])
            answer.set()
            self.until(root, lambda: rows()[0][1] != "checking…", "showed the checks")
            self.assertEqual(rows(), [("GitHub CLI installed", "gh 2.63.2", ""), ("Signed in", "", "grid")])
            self.assertEqual(first.checks[1].problem.cget("text"), "run `gh auth login` in a terminal")
            # A repo that can't be added says so here: the log, where it's also said, is put away.
            self.assertTrue(first.entry.bind("<Return>"))
            app.new_repo.set("octo/missing")
            first.add()
            self.assertEqual(first.status.cget("text"), "Adding octo/missing…")
            self.until(root, lambda: str(first.status.cget("style")) == "Problem.TLabel", "said it failed")
            self.assertIn("Can't add octo/missing: boom", first.status.cget("text"))
            self.assertEqual(up(), (True, True, False, False, False))
            # Adding one brings the table and the rest of the window back.
            app.new_repo.set("octo/direct-repo")
            first.add()
            self.until(root, lambda: tree.exists(ROW), "listed the repo")
            self.assertEqual(up(), (False, False, True, True, True))
            self.assertEqual(app.body.panes()[0], str(app.sidebar))
            self.assertEqual(self.saved()["repos"], [REPO])
            # And removing the last one is a first run again.
            app.remove([REPO])
            root.update()
            self.assertEqual(up(), (True, True, False, False, False))
            self.assertEqual(first.status.cget("text"), first.HINT)
            self.assertEqual(self.saved()["repos"], [])

        with mock.patch.object(ca, "setup_checks", checks):
            self.bare_window(FakeGh(reactions=THUMBS, fail=["octo/missing"]), check)

    def test_first_run_stays_away_while_launch_repos_arrive(self):
        def watching(root, tree, buttons, wait_for):
            first = root.app.first_run
            self.assertFalse(first.up)  # the repo passed at launch is still being looked up: not a first run
            wait_for("Thumbs up")
            self.assertEqual((first.up, first.screen), (False, None))  # never built

        self.window(FakeGh(reactions=THUMBS), watching)

        def unresolved(root, tree, buttons, wait_for):
            first = root.app.first_run
            self.until(root, lambda: first.up, "showed the first run")  # the one repo passed can't be added
            self.assertIn("Can't add octo/direct-repo", first.status.cget("text"))

        with mock.patch.object(ca, "setup_checks", lambda: []):
            self.window(FakeGh(fail=["repo view"]), unresolved)

    def test_settings_window_acts_through_the_app(self):
        import tkinter as tk
        from tkinter import ttk

        def check(root, tree, buttons, wait_for):
            app = root.app
            wait_for("Thumbs up")
            gear = next(b for b in self.kin(app.sidebar, ttk.Button)
                        if b.cget("image") and str(b.cget("image")[0]) == str(app.icon("sliders", "text-3", 15)))
            gear.invoke()
            prefs = app.prefs
            gear.invoke()
            app.settings()
            self.assertIs(app.prefs, prefs)  # opened once; after that it's brought forward
            self.assertEqual(len(self.kin(root, tk.Toplevel)), 1)
            switch = lambda pane: self.kin(prefs.panes[pane], ttk.Checkbutton)

            # Appearance: applied at once, to this window too, and saved.
            for look in ("dark", "light"):
                prefs.looks[look][0].event_generate("<Button-1>", x=5, y=5)
                root.update()
                self.assertEqual((app.appearance, app.dark, self.saved()["appearance"]), (look, look == "dark", look))
                self.assertEqual(str(prefs.top.cget("background")).upper(), ca.PALETTE[look]["win"])
                self.assertEqual(str(prefs.canvas.cget("background")).upper(), ca.PALETTE[look]["win"])
                self.assertIn("selected", prefs.looks[look][1].state())

            # The interval is the toolbar's own variable.
            [spin] = self.kin(prefs.panes["general"], ttk.Spinbox)
            self.assertEqual(str(spin.cget("textvariable")), str(app.interval_var))

            # Merge mode, from here and from the toolbar: each shows in the other.
            lit = lambda: {mode for mode, parts in prefs.mode_cards.items() if "selected" in parts[0].state()}
            self.assertEqual(lit(), {""})
            prefs.mode_cards["admin"][1].invoke()  # its radio button
            root.update()
            self.assertEqual((app.mode_var.get(), app.live["mode"], self.saved()["mode"]), ("admin",) * 3)
            self.assertEqual((lit(), bool(app.admin_banner.winfo_manager()), bool(prefs.admin_note.winfo_manager())),
                             ({"admin"}, True, True))
            prefs.mode_cards["force"][3].event_generate("<Button-1>", x=2, y=2)  # anywhere else on a card
            self.assertEqual((app.live["mode"], self.saved()["mode"], lit()), ("force", "force", {"force"}))
            toolbar = next(r for r in self.kin(root, ttk.Radiobutton)
                           if r.winfo_toplevel() is root and r.cget("text") == "Queue")
            toolbar.invoke()
            self.assertEqual((app.live["mode"], lit(), bool(prefs.admin_note.winfo_manager())), ("", {""}, False))

            # Auto-merge: the toolbar's switch and this one are one variable.
            [auto] = switch("merging")
            auto.invoke()
            self.assertEqual((app.merging.get(), app.live["merging"], self.saved()["merging"]), (False,) * 3)
            self.assertTrue(app.paused_banner.winfo_manager())
            auto.invoke()
            self.assertEqual((app.live["merging"], self.saved()["merging"]), (True, True))

            # Notifications: a switch a kind, kept on the App and saved.
            on = dict.fromkeys(ca.NOTIFY_KINDS, True)
            self.assertEqual(app.notify, on)
            switch("notify")[ca.NOTIFY_KINDS.index("cleared")].invoke()
            self.assertEqual(app.notify, dict(on, cleared=False))
            self.assertEqual(self.saved()["notify"], dict(on, cleared=False))

            # Open at login: written off the Tk thread, and the switch then shows what's true.
            [login] = switch("general")
            self.assertFalse(prefs.login.get())
            enabled.return_value = True
            login.invoke()
            self.until(root, lambda: login_item.called and "selected" in login.state(), "wrote the login item")
            login_item.assert_called_once_with(True)
            enabled.return_value = False  # as when writing it failed
            login.invoke()
            self.until(root, lambda: login_item.call_count == 2 and not prefs.login.get(), "re-read the login item")
            self.assertEqual(login_item.call_args.args, (False,))

            # Repositories: gh is asked what its account may do there, and the list is the App's own.
            self.until(root, lambda: prefs.repo_rows[REPO].badge.cget("text") == "Can merge", "showed merge rights")
            self.assertEqual(app.rights, {REPO: True})
            self.assertEqual(prefs.repo_rows[REPO].note.cget("text"), "1 open")
            [trash] = self.kin(prefs.repo_rows[REPO], ttk.Button)
            trash.invoke()
            self.assertEqual((app.repos, list(prefs.repo_rows), list(app.repo_rows), self.saved()["repos"]),
                             ([], [], [], []))
            self.assertTrue(app.first_run.up)
            prefs.new_repo.set("octo/direct-repo")
            prefs.add()
            self.until(root, lambda: REPO in prefs.repo_rows and tree.exists(ROW), "added it back")
            self.assertEqual((app.repos, list(app.repo_rows), app.first_run.up), ([REPO], [REPO], False))

            # Closed, it leaves nothing behind that the App would still call.
            root.tk.call(prefs.top.protocol("WM_DELETE_WINDOW"))  # its close box
            self.assertIsNone(app.prefs)
            self.assertEqual((self.kin(root, tk.Toplevel), app.followers, app.painters), ([], [app.first_run.told], []))
            app.pick_view("ready")  # renders, and tells whoever follows
            app.settings()
            self.assertIsNot(app.prefs, prefs)

        with mock.patch.object(ca, "login_item", return_value="opens at login") as login_item, \
                mock.patch.object(ca, "login_item_enabled", return_value=False) as enabled, \
                mock.patch.object(ca, "can_merge", return_value=True):
            self.window(FakeGh(reactions=THUMBS), check)

    def test_settings_window_says_when_merge_rights_are_unknown_or_missing(self):
        answers = iter([None, False])

        def check(root, tree, buttons, wait_for):
            app = root.app
            wait_for("Thumbs up")
            for badge, rights in (("Unknown", {}), ("Can't merge", {REPO: False})):
                app.settings()
                self.assertEqual(app.prefs.repo_rows[REPO].badge.cget("text"), "Checking…")
                self.until(root, lambda: app.prefs.repo_rows[REPO].badge.cget("text") == badge, f"said {badge}")
                self.assertEqual(app.rights, rights)
                app.prefs.close()

        with mock.patch.object(ca, "can_merge", lambda repo: next(answers)):
            self.window(FakeGh(reactions=THUMBS), check)

    def test_settings_panes_cover_every_mode_and_notification(self):
        self.assertEqual(set(ca.Settings.MODE_HELP), {value for _, value in ca.MODES})
        self.assertEqual(tuple(ca.Settings.NOTIFY_HELP), ca.NOTIFY_KINDS)
        self.assertEqual([look for look, _ in ca.Settings.LOOKS], ["light", "dark", "system"])
        self.assertEqual({look for look, _ in ca.Settings.LOOKS}, set(ca.APPEARANCES))
        for _, _, icon in ca.Settings.PANES:
            self.assertIn(icon, ca.ICONS)
        self.assertEqual(ca.FirstRun.CHECKS, ("GitHub CLI installed", "Signed in"))  # as setup_checks() titles them
        with mock.patch.object(ca, "gh", self.answering({})):
            self.assertEqual(tuple(title for _, title, _ in ca.setup_checks()), ca.FirstRun.CHECKS)

if __name__ == "__main__":
    unittest.main()
