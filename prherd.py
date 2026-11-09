#!/usr/bin/env python3
"""Watch GitHub repos' open PRs; squash-merge each one Codex has cleared.

  ./prherd.py [--force|--admin] [--interval N] [--headless] [repo ...]

Codex (chatgpt-codex-connector) says "reviewed, no findings" with a +1 reaction
on the PR body, or with a "Didn't find any major issues" comment naming the
reviewed commit. Neither is a review or a status check.

--force merges immediately instead of queuing with --auto.
--admin does the same and bypasses branch protection (gh pr merge --admin).

A PR labeled do-not-merge is never merged, whatever Codex says.
"""
import argparse
import base64
import calendar
import functools
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib

# The release workflow writes the version from its trigger here before building.
__version__ = "0.0.0-dev"
# chatgpt-codex-connector[bot]. Its numeric id, not its login: an id can't be renamed or reused.
BOT_ID = int(os.environ.get("BOT_ID", "199175422"))
# Only Codex's review receipt, from its first byte: a free-text Codex reply can't start with this.
CLEAN = re.compile(r"\s*<!-- codex-review-receipt:v1 -->\s*Codex Review: Didn.t find any major issues")  # . matches ' and ’
REVIEWED = re.compile(r"Reviewed commit:\W*([0-9a-f]{10,40})")
# Hidden in the conflict comment so each head commit gets one, across restarts too.
# Keeps the pre-rename name: a new marker would repost on every PR already commented.
CONFLICT = "<!-- codex-automerge:conflict {} -->"
# A PR with this label never merges. A label, not a local list, so it also holds headless,
# on other machines, and when set from GitHub's web UI.
DONT_MERGE = "do-not-merge"
MODES = (("Queue", ""), ("Force", "force"), ("Admin", "admin"))
# The window's repos and controls, restored at launch.
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "prherd.json")
# Where Homebrew (Apple silicon, then Intel) and gh's installers put gh. An app opened from
# Finder gets launchd's PATH, /usr/bin:/bin:/usr/sbin:/sbin, which has none of them.
GH_FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", r"C:\Program Files\GitHub CLI\gh.exe")
# The packaged Windows app has no console, so each gh would flash one open without this.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The window's icon: next to this file, or inside the bundle the release build makes.
ICON = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "assets", "prherd.png")


def find_gh():
    """gh's absolute path: a relative PATH hit breaks under cwd. "gh" if it's nowhere, so the error names it."""
    found = shutil.which("gh") or next((p for p in GH_FALLBACKS if os.access(p, os.X_OK)), None)
    return os.path.abspath(found) if found else "gh"


# A full path, no cwd and close_fds=False let subprocess use posix_spawn instead of fork: macOS libmalloc
# prints "MallocStackLogging: can't turn off..." in every forked child of the Tk process.
GH = find_gh()


def gh(*args, cwd=None):
    try:
        return subprocess.run([GH, *args], cwd=cwd, capture_output=True, text=True, timeout=120,
                              close_fds=cwd is not None, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:  # a hung gh would stall every repo; fail like any gh error
        return subprocess.CompletedProcess(args, 1, "", "gh timed out after 120s")


def output(r):
    return " ".join((r.stdout + r.stderr).split())


def gh_json(*args):
    r = gh(*args)
    if r.returncode:
        raise RuntimeError(output(r) or f"gh exited {r.returncode}")
    return json.loads(r.stdout)


def api(host, path):
    pages = gh_json("api", "--hostname", host, "--paginate", "--slurp", path)
    return [item for page in pages for item in page]


def resolve(target):
    """owner/repo or a local checkout -> "host/owner/repo"."""
    path = os.path.expanduser(target)
    local = os.path.isdir(path)
    r = gh("repo", "view", *([] if local else [target]), "--json", "url", "--jq", ".url",
           cwd=path if local else None)
    if r.returncode:
        raise RuntimeError(output(r) or f"can't resolve {target}")
    return r.stdout.strip().split("://", 1)[1]


def is_bot(item):
    return (item.get("user") or {}).get("id") == BOT_ID


def pushed_at(host, nwo, pr):
    """When the PR head last changed by GitHub's clock (git dates can be faked): the head
    commit's newest check suite or the PR's latest force-push. None if neither exists."""
    pages = gh_json("api", "--hostname", host, "--paginate", "--slurp",
                    f"repos/{nwo}/commits/{pr['headRefOid']}/check-suites?per_page=100")
    times = [s["created_at"] for page in pages for s in page["check_suites"]]
    times += [e["created_at"] for e in api(host, f"repos/{nwo}/issues/{pr['number']}/timeline?per_page=100")
              if e.get("event") == "head_ref_force_pushed"]
    return max(times, default=None)


def verdict_detail(repo, pr):
    """verdict() and the times behind it, for the detail panel: when this head arrived ("pushed"), when
    Codex started reviewing it ("reviewing_at", its 👀) and when it cleared it ("cleared_at").
    Each is None if it didn't happen."""
    host, nwo = repo.split("/", 1)
    issue = f"repos/{nwo}/issues/{pr['number']}"
    pushed = pushed_at(host, nwo, pr)
    d = {"verdict": None, "pushed": pushed, "cleared_at": None, "reviewing_at": None}
    if not pushed:  # can't tell when this head arrived, so no go-ahead can be tied to it
        return d
    # A reaction names no commit and survives pushes, so it only counts if it came after this head arrived.
    reactions = {r["content"]: r["created_at"] for r in api(host, f"{issue}/reactions?per_page=100")
                 if is_bot(r) and r["created_at"] > pushed}
    d["reviewing_at"] = reactions.get("eyes")  # Codex reacts 👀 when it starts a review
    if "+1" in reactions:
        return dict(d, verdict="👍", cleared_at=reactions["+1"])
    for c in api(host, f"{issue}/comments?per_page=100"):
        body = c.get("body") or ""
        m = is_bot(c) and CLEAN.match(body) and REVIEWED.search(body)
        # Counts only for the commit it reviewed, and only if that head didn't arrive later
        # (a pushed commit crafted to share the short hash).
        if m and pr["headRefOid"].startswith(m[1]) and c["created_at"] > pushed:
            return dict(d, verdict="no major issues", cleared_at=c["created_at"])
    return dict(d, verdict="👀" if "eyes" in reactions else None)


def verdict(repo, pr):
    """How Codex cleared this PR's current head, "👀" if it's still reviewing it, or None."""
    return verdict_detail(repo, pr)["verdict"]


def dont_merge(pr):
    return any(label["name"].lower() == DONT_MERGE for label in pr["labels"])


def merge(repo, pr, mode):
    # The poll's list can be a while old by now, and a merge can't be taken back.
    try:
        if dont_merge(gh_json("pr", "view", str(pr["number"]), "-R", repo, "--json", "labels")):
            return "marked don't merge; not merging"
    except (RuntimeError, ValueError) as e:
        return f"failed to re-check labels; not merging, retrying ({e})"
    args = ["pr", "merge", str(pr["number"]), "-R", repo, "--squash",
            "--match-head-commit", pr["headRefOid"]]
    if not mode:
        r = gh(*args, "--auto")
        if not r.returncode:
            return "merge queued behind required checks"
        # Repos without auto-merge allowed reject --auto. Only that error falls back to
        # merging now, so a conflict or a failing required check still stops us.
        if not re.search(r"auto.?merge", output(r), re.I):
            return f"MERGE FAILED: {output(r)}"
    r = gh(*args, *(["--admin"] if mode == "admin" else []))
    if r.returncode:
        return f"MERGE FAILED: {output(r)}"
    # Without --auto gh still enables auto-merge (or the merge queue) when required
    # checks are pending, so success is not proof of a merge. Trust gh's wording.
    if "merged pull request" in output(r).lower():
        return f"merged now ({mode or 'auto-merge unavailable'})"
    return f"not merged — {output(r)}"


def request_review(repo, number):
    r = gh("pr", "comment", str(number), "-R", repo, "--body", "@codex review")
    return "asked Codex for a review" if not r.returncode else f"review request FAILED: {output(r)}"


def mark(repo, number, dont):
    """Add or remove the don't-merge label. A poll then cancels any auto-merge already queued."""
    host, nwo = repo.split("/", 1)
    labels = f"repos/{nwo}/issues/{number}/labels"
    if dont:
        # Without --force this leaves a label the repo already has as it is, and fails; that's fine.
        gh("label", "create", DONT_MERGE, "-R", repo, "--color", "B60205",
           "--description", "prherd won't merge this pull request")
        r = gh("api", "--hostname", host, "-X", "POST", labels, "-f", f"labels[]={DONT_MERGE}")
    else:
        r = gh("api", "--hostname", host, "-X", "DELETE", f"{labels}/{DONT_MERGE}")
    done = "marked don't merge" if dont else "cleared don't merge"
    return done if not r.returncode else f"{done} FAILED: {output(r)}"


def browse(repo, number=None):
    """Open a PR, or the repo if number is None, in the browser."""
    r = gh("browse", *([number] if number else []), "-R", repo)
    return "opened in browser" if not r.returncode else f"open in browser FAILED: {output(r)}"


def update_branch(repo, number):
    r = gh("pr", "update-branch", str(number), "-R", repo)
    return "branch updated" if not r.returncode else f"update branch FAILED: {output(r)}"


def cancel_auto_merge(repo, pr):
    r = gh("pr", "merge", str(pr["number"]), "-R", repo, "--disable-auto")
    return "cancelled queued auto-merge" if not r.returncode else f"cancelling auto-merge FAILED: {output(r)}"


def flag_conflict(repo, pr):
    """Comment once per head commit that the PR has merge conflicts."""
    host, nwo = repo.split("/", 1)
    marker = CONFLICT.format(pr["headRefOid"])
    try:
        me = gh_json("api", "--hostname", host, "user")["id"]
        comments = api(host, f"repos/{nwo}/issues/{pr['number']}/comments?per_page=100")
    except (RuntimeError, ValueError) as e:
        return f"merge conflict; failed to fetch comments, retrying ({e})"
    # Only our own comment counts: anyone else posting the marker would silence the notice.
    if any((c.get("user") or {}).get("id") == me and marker in (c.get("body") or "") for c in comments):
        return "merge conflict; already commented"
    # No @codex here: that would ask Codex for a review.
    r = gh("pr", "comment", str(pr["number"]), "-R", repo, "--body",
           f"This pull request has merge conflicts with `{pr['baseRefName']}`. "
           f"Resolve them so it can be merged.\n\n{marker}")
    return "merge conflict; commented on the PR" if not r.returncode else f"merge conflict; comment FAILED: {output(r)}"


def poll(repo, log, merge_mode, details=None):
    """One pass over repo's open PRs. Returns [(pr, verdict)], or None if listing failed.

    merge_mode() is asked right before each merge: the mode to merge with, or None to skip.
    Conflicting PRs are commented on either way, unless marked don't merge.
    details, if a dict, gets {number: verdict_detail()} for each PR whose verdict was looked up.
    """
    try:
        # shortcut: PRs past the 1000th open one are never seen; page through the API if a repo gets there.
        prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "1000",
                      "--json", "number,title,headRefOid,mergeable,baseRefName,labels,autoMergeRequest,isDraft,author,"
                                "headRefName,updatedAt")
    except (RuntimeError, ValueError) as e:
        log(f"{repo}: failed to list open PRs; retrying ({e})")
        return None
    log(f"{repo}: {len(prs)} open PR(s)")
    rows = []
    for pr in prs:
        tag = f"  {repo}#{pr['number']}"
        if dont_merge(pr):
            rows.append((pr, "don't merge"))
            log(f"{tag}: marked don't merge; skipping")
            if pr["autoMergeRequest"]:  # GitHub would merge it without us
                log(f"{tag}: {cancel_auto_merge(repo, pr)}")
            continue
        # UNKNOWN (GitHub still computing it) falls through; the next poll sees the answer.
        if pr.get("mergeable") == "CONFLICTING":  # a merge would fail; say so on the PR instead
            rows.append((pr, "merge conflict"))
            log(f"{tag}: {flag_conflict(repo, pr)}")
            continue
        try:
            detail = verdict_detail(repo, pr)
        except (RuntimeError, ValueError) as e:
            log(f"{tag}: failed to fetch Codex reactions/comments; retrying ({e})")
            rows.append((pr, "?"))
            continue
        v = detail["verdict"]
        if details is not None:
            details[pr["number"]] = detail
        rows.append((pr, v))
        if v == "👀":
            log(f"{tag}: Codex is reviewing (👀)")
        elif not v:
            log(f"{tag}: no 👍 or clean review yet")
        elif (mode := merge_mode()) is not None:
            log(f"{tag}: {v} — squash merging")
            log(f"{tag}: {merge(repo, pr, mode)}")
    return rows


# What the detail panel, first-run screen, notifications and Open at login need. No window code from here to
# load_settings: all of it is called by the window, and tested without one.
GO_AHEADS = ("👍", "no major issues")


def short_repo(repo):
    """A repo as people read it: without the host when that's github.com."""
    return repo[len("github.com/"):] if repo.startswith("github.com/") else repo


def head7(pr):
    return (pr.get("headRefOid") or "")[:7] or "this head"


def readiness(pr, v):
    """The detail panel's merge checklist for a PR and its verdict from poll(): [(tone, title, note)].

    Always five, in this order: Codex's go-ahead, conflicts, the don't-merge label, draft, queued.
    tone is a VERDICTS tone: "ok" passes, "warn" waits, "bad" and "err" block, "none" has nothing to say.
    """
    sha, base = head7(pr), pr.get("baseRefName") or "the base branch"
    if v in GO_AHEADS:
        go = ("ok", "Codex go-ahead on the current head", f"{'Newer than' if v == '👍' else 'Receipt names'} {sha}")
    elif v == "👀":
        go = ("warn", "Codex is reviewing", "Its verdict lands on the next poll")
    elif v == "?":
        go = ("err", "Couldn't read Codex's verdict", "GitHub returned an error; retrying")
    elif v == "merge conflict":  # poll() doesn't ask about Codex for these two
        go = ("warn", "Not checked while conflicting", "Resolve the conflict first")
    elif v == "don't merge":
        go = ("warn", "Not checked while held", "Allow merge to check again")
    else:
        go = ("warn", "No go-ahead yet", "Request a Codex review to get one")
    if v == "merge conflict":
        conflict = ("bad", f"Conflicts with {base}", f"Commented once on {sha}")
    elif pr.get("mergeable") == "CONFLICTING":  # held as well: poll() leaves it uncommented
        conflict = ("bad", f"Conflicts with {base}", "Not commented on while held")
    elif pr.get("mergeable") == "UNKNOWN":
        conflict = ("warn", f"Checking for conflicts with {base}", "GitHub is still working it out")
    else:
        conflict = ("ok", f"No conflicts with {base}", "Mergeable as of the last poll")
    if v == "don't merge":
        label = ("bad", f"Labeled {DONT_MERGE}", "Allow merge to release it")
    else:
        label = ("ok", f"Not labeled {DONT_MERGE}", "")
    if pr.get("isDraft"):
        draft = ("warn", "Draft pull request", "Mark ready for review on GitHub")
    else:
        draft = ("ok", "Ready for review", "Not a draft")
    if pr.get("autoMergeRequest") and v != "don't merge":  # as row_values: a held PR's is cancelled by the poll
        queue = ("ok", "Auto-merge queued", "GitHub merges it once required checks pass")
    elif v in GO_AHEADS:
        queue = ("warn", "Not queued yet", "prherd merges it on a poll with Auto-merge on")
    else:
        queue = ("none", "Not queued", "")
    return [go, conflict, label, draft, queue]


def headline(pr, v, merging=True):
    """The detail panel's status card for a PR and its verdict: (tone, title, detail).
    merging is the Auto-merge switch: off, a cleared PR is only watched."""
    sha = head7(pr)
    if v == "don't merge":
        return ("hold", "Held — won't merge",
                f"Labeled {DONT_MERGE}. prherd skips it and cancels any queued auto-merge.")
    if v == "merge conflict":
        return ("bad", "Blocked by a merge conflict",
                "Update the branch or resolve locally. Codex will need to review the new head.")
    if v == "?":
        return "err", "Couldn't read Codex's verdict", "GitHub returned an error. prherd retries on the next poll."
    if v == "👀":
        return ("warn", "Codex is reviewing", "If it clears this head, prherd merges on the poll after." if merging
                else "Auto-merge is off, so prherd won't merge it when Codex clears this head.")
    if v not in GO_AHEADS:
        return ("none", "Draft — not merging yet" if pr.get("isDraft") else "Waiting for Codex",
                f"No go-ahead newer than {sha}. Request a review to start one.")
    if pr.get("isDraft"):  # as row_values: draft outranks queued
        return ("warn", "Draft — not merging yet",
                f"Codex cleared {sha}, but GitHub won't merge a draft. Mark it ready for review on GitHub.")
    if pr.get("autoMergeRequest"):  # GitHub merges it by itself, whatever the Auto-merge switch says
        return ("ok", "Cleared — queued to merge",
                f"Squash merge is queued at {sha} and lands once required checks pass.")
    if not merging:
        return ("ok", "Cleared — watching only",
                f"Codex cleared {sha}. Auto-merge is off, so prherd won't merge it. Use Merge now, or resume.")
    return ("ok", "Cleared — merges on the next poll",
            f"Codex cleared {sha}. prherd squash-merges it on the next poll; the log says why if it can't.")


def timeline(pr, v, detail):
    """What's known to have happened to this head, oldest first: [(iso time, tone, text)].

    detail is the PR's verdict_detail() as poll() stored it, or None for a PR it didn't look up.
    tone is a VERDICTS tone, or "acc" (the accent) for the queued auto-merge.
    """
    sha, d = head7(pr), detail or {}
    events = [(d.get("pushed"), "none", f"Head {sha} pushed"),
              (d.get("reviewing_at"), "warn", "Codex started reviewing")]
    if v in GO_AHEADS:
        events.append((d.get("cleared_at"), "ok",
                       "Codex gave a thumbs-up" if v == "👍" else f"Codex: no major issues on {sha}"))
    events.append(((pr.get("autoMergeRequest") or {}).get("enabledAt"), "acc", "Auto-merge queued behind checks"))
    # GitHub's times are all UTC in one format, so they sort as text. Ties keep the order above.
    return sorted((e for e in events if e[0]), key=lambda e: e[0])


# The first gh with `api --slurp`, which every Codex lookup pages with.
GH_MIN = (2, 48)


def gh_version(text):
    """`gh --version` output -> (2, 63, 2), or None if it isn't that."""
    m = re.search(r"gh version (\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(n) for n in m.groups() if n is not None) if m else None


def setup_checks():
    """The first-run screen's checks: [(ok, title, note)], for gh installed and new enough, then signed in."""
    def ask(*args):
        try:
            return gh(*args)
        except OSError:  # no gh to run: that raises, where a gh that fails returns an exit code
            return None

    dotted = lambda version: ".".join(map(str, version))
    title, r = "GitHub CLI installed", ask("--version")
    version = gh_version(r.stdout) if r and not r.returncode else None
    if r is None:
        installed = (False, title, "gh not found; install it from cli.github.com")
    elif not version:
        installed = (False, title, "couldn't read gh's version")
    elif version < GH_MIN:
        installed = (False, title, f"gh {dotted(version)} is too old; prherd needs {dotted(GH_MIN)} or newer")
    else:
        installed = (True, title, f"gh {dotted(version)}")
    title, r = "Signed in", ask("auth", "status")
    if r is None:
        signed = (False, title, "needs gh")
    elif r.returncode:
        signed = (False, title, "run `gh auth login` in a terminal")
    else:  # "✓ Logged in to github.com account octocat (keyring)"; "as octocat" before gh 2.40
        m = re.search(r"Logged in to \S+ (?:account|as) (\S+)", r.stdout + r.stderr)
        signed = (True, title, m[1] if m else "")
    return [installed, signed]


def pr_state(repo, number):
    """{"state": "MERGED" | "CLOSED" | "OPEN", "mergedAt", "title", "number"} for one PR, or None if gh can't say."""
    try:
        state = gh_json("pr", "view", str(number), "-R", repo, "--json", "state,mergedAt,title,number")
    except (RuntimeError, ValueError, OSError):
        return None
    return state if isinstance(state, dict) else None


NOTIFY_KINDS = ("merged", "conflict", "failed", "cleared")


def checked_notify(value):
    """The saved notification switches, each checked: {kind: bool}, on unless saved as false."""
    value = value if isinstance(value, dict) else {}
    return {kind: value[kind] if isinstance(value.get(kind), bool) else True for kind in NOTIFY_KINDS}


def notices(repo, before, rows, streaks, state=pr_state):
    """What one poll of repo is worth notifying about: [(kind, title, body)], kind from NOTIFY_KINDS.

    rows is poll()'s result; before is {number: (pr, verdict)} from the repo's last successful poll.
    before is None on the first poll, which notifies nothing: every launch would fire a burst.
    A PR not in before is silent too, for the same reason; it notifies once its verdict changes.
    streaks is the caller's {(repo, number): failed lookups in a row}, kept current here.
    state() is asked about each PR that left the list, which runs gh: keep this off the Tk thread.
    """
    out = []

    def say(kind, title, number, text):
        out.append((kind, f"{title} #{number}", f"{short_repo(repo)} · {text}"))

    if rows is None:  # the listing failed: that isn't every PR leaving
        return out
    for pr, v in rows:
        number, text = pr["number"], pr.get("title", "")
        if v == "?":
            streaks[repo, number] = streaks.get((repo, number), 0) + 1
            if streaks[repo, number] == 2 and before is not None:  # one failure is a blip; a third is old news
                say("failed", "Lookup failed for", number, text)
        else:
            streaks.pop((repo, number), None)
        if before is None or number not in before:
            continue
        was = before[number][1]
        if v == "merge conflict" and was != v:
            say("conflict", "Merge conflict on", number, text)
        if v in GO_AHEADS and was not in GO_AHEADS:
            say("cleared", "Codex cleared", number, text)
    still_open = {pr["number"] for pr, _ in rows}
    for number, (pr, _) in (before or {}).items():
        if number not in still_open:
            streaks.pop((repo, number), None)
            gone = state(repo, number)
            if gone and gone.get("state") == "MERGED":  # closed without merging, or unknown: say nothing
                say("merged", "Merged", number, gone.get("title") or pr.get("title", ""))
    return out


# PR titles are anyone's text, so they reach AppleScript as arguments, never as part of the script.
NOTIFY_SCRIPT = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"


def notify(title, body):
    """Post a desktop notification. False where there's no way to, or it failed. Never raises."""
    if sys.platform == "darwin":
        argv = ["/usr/bin/osascript", "-e", NOTIFY_SCRIPT, "--", str(title), str(body)]
    elif tool := shutil.which("notify-send"):  # Linux desktops; Windows has nothing built in to call
        argv = [tool, "--app-name=prherd", "--", str(title), str(body)]
    else:
        return False
    try:  # spawned as gh() is: no cwd and close_fds=False, so no fork
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, close_fds=False, creationflags=NO_WINDOW)
    except (OSError, ValueError, subprocess.SubprocessError):  # ValueError: a NUL in the text
        return False
    return not r.returncode


def merged_today(tally, today, repo=None):
    """The merges seen today, as (day, {repo: how many}): tally, with one more for repo if there is one.
    A tally from another day starts over, so the count is of merges since midnight."""
    day, counts = tally
    counts = dict(counts) if day == today else {}
    if repo:
        counts[repo] = counts.get(repo, 0) + 1
    return today, counts


# The LaunchAgent's label (the app's bundle id), and where Windows lists what to run at sign-in.
LOGIN_LABEL = "com.pacnpal.prherd"
LOGIN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    """The argv that starts this app: the packaged app's executable, or python and this file."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def login_item_path():
    """The file that opens this app at login: a LaunchAgent on macOS, an XDG autostart entry on Linux.
    None on Windows, which keeps that in the registry. Worked out when called, so it follows HOME."""
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", f"{LOGIN_LABEL}.plist")
    if sys.platform == "win32":
        return None
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config, "autostart", "prherd.desktop")


def desktop_exec(argv):
    """argv as a .desktop file's Exec value: each word quoted, then backslashes doubled as any string value's are."""
    words = ['"' + re.sub(r'(["`$\\])', r"\\\1", word).replace("%", "%%") + '"' for word in argv]
    return " ".join(words).replace("\\", "\\\\")


def login_item_enabled():
    if sys.platform != "win32":
        return os.path.exists(login_item_path())
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LOGIN_KEY) as key:
            winreg.QueryValueEx(key, "prherd")
    except OSError:
        return False
    return True


def login_item(enable):
    """Open this app at login, or stop. Only changes the next login: nothing is started or quit now."""
    path = login_item_path()
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LOGIN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, "prherd", 0, winreg.REG_SZ, subprocess.list2cmdline(launch_command()))
                else:
                    winreg.DeleteValue(key, "prherd")
        elif not enable:
            os.remove(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if sys.platform == "darwin":
                import plistlib
                with open(path, "wb") as f:
                    plistlib.dump({"Label": LOGIN_LABEL, "ProgramArguments": launch_command(), "RunAtLoad": True}, f)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("[Desktop Entry]\nType=Application\nName=prherd\n"
                            f"Exec={desktop_exec(launch_command())}\nTerminal=false\n")
    except OSError as e:
        if enable or not isinstance(e, FileNotFoundError):  # nothing to remove: it's already off
            return f"{'open at login' if enable else 'turning off open at login'} FAILED: {e}"
    return "opens at login" if enable else "no longer opens at login"


REPO_FORM = r"[^\s#/]+(/[^\s#/]+){2}"  # a resolved repo, as resolve() gives it
MAX_INTERVAL = 86400  # a day: the spinbox's top, and well short of what Tk's timers can count to


def load_settings():
    """Saved window settings, each checked: a bad mode would otherwise merge() as force."""
    old = os.path.join(os.path.dirname(CONFIG), "codex-automerge.json")  # the pre-rename file
    try:
        if os.path.exists(old) and not os.path.exists(CONFIG):
            os.replace(old, CONFIG)  # move, not copy: a stale old file could resurface later
        with open(CONFIG) as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        s = {}
    except (OSError, ValueError) as e:  # exit rather than let the next save overwrite it
        sys.exit(f"can't read saved settings {CONFIG}: {e}; fix or delete it")
    repos, interval, geometry = s.get("repos"), s.get("interval"), s.get("geometry")
    view, sidebar = s.get("view"), s.get("sidebar")
    text = lambda value, pattern: isinstance(value, str) and re.fullmatch(pattern, value)
    return {
        # Saved repos are resolved: host/owner/repo. Anything else would be a row the window can't tell from a PR's.
        "repos": [r for r in repos if text(r, REPO_FORM)] if isinstance(repos, list) else [],
        "mode": s.get("mode") if s.get("mode") in [v for _, v in MODES] else "",
        "interval": interval if type(interval) is int and 0 < interval <= MAX_INTERVAL else 60,
        "merging": s.get("merging", True) is True,  # merging is the one to be sure of: "false" or 0 isn't a yes
        "geometry": geometry if text(geometry, r"\d+x\d+([+-]-?\d+){2}") else None,
        "detail": s["detail"] if type(s.get("detail")) is int and DETAIL[0] <= s["detail"] <= DETAIL[1] else 320,
        "appearance": s.get("appearance") if s.get("appearance") in APPEARANCES else "system",
        "view": view if isinstance(view, str) and view in VIEWS else "all",
        "sidebar": sidebar if type(sidebar) is int and SIDEBAR[0] <= sidebar <= SIDEBAR[1] else 232,
        "log": s.get("log") is not False,
        "notify": checked_notify(s.get("notify")),
        "menubar": s.get("menubar") is not False,
    }


def row_values(pr, v, now=None):
    """The tree's columns for a PR. A marked PR's auto-merge is cancelled during the poll, so it isn't "Queued"."""
    state = "Draft" if pr["isDraft"] else "Queued" if pr["autoMergeRequest"] and v != "don't merge" else "Open"
    return (VERDICTS[v]["label"], (pr["author"] or {}).get("login", ""), state, (pr.get("headRefOid") or "")[:7],
            age(pr.get("updatedAt"), time.time() if now is None else now))


def age(iso, now):
    """How long before now (in epoch seconds) a GitHub timestamp was: "now", "5m", "3h", "2d", "6w" or "1y".
    "" if it's missing or unreadable."""
    try:
        seconds = now - calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return ""
    for unit, size in (("y", 31536000), ("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"  # under a minute, or ahead of this machine's clock


def short(repo):
    """host/owner/repo as the window names it: github.com goes without saying, any other host is kept."""
    return repo[len("github.com/"):] if repo.startswith("github.com/") else repo


def matches(pr, v, view, query):
    """Whether the table shows this PR: its verdict belongs to the view, and each word of the search is somewhere
    in its number, title, branch or author."""
    shown = VIEWS[view]["verdicts"]
    text = f"#{pr['number']} {pr['title']} {pr.get('headRefName') or ''} {(pr.get('author') or {}).get('login', '')}"
    return (shown is None or v in shown) and all(word in text.lower() for word in query.lower().split())


def kind_of(msg):
    """What a log line is about, which picks its color in the activity log: one of KINDS, or "info"."""
    low = msg.lower()
    return next((kind for kind, _, marks in KINDS if any(mark in low for mark in marks)), "info")


def split_log(msg):
    """A log line as (the repo or PR it's about, what happened). poll() puts the first before a ": "."""
    m = re.match(r"\s*(\S+/\S+?): (.*)", msg, re.S)
    return (short(m[1]), m[2]) if m else ("", msg.strip())


def whoami():
    """The login gh is signed in as, or "" if it can't say."""
    try:
        me = gh_json("api", "user")
    except (RuntimeError, ValueError, OSError):
        return ""
    return me.get("login") or "" if isinstance(me, dict) else ""


def save_settings(settings):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = f"{CONFIG}.{os.getpid()}.tmp"  # per process: two open windows mustn't share one tmp file
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, CONFIG)  # a crash mid-write leaves the old file whole


def stamp(msg):
    return f"{time.strftime('%F %T')} {msg}"


APPEARANCES = ("system", "light", "dark")
# The solid color tokens from design/Main.dc.html. Tk has no alpha, so the rgba ones are left out.
PALETTE = {
    "light": {"page": "#E7E7EB", "win": "#FFFFFF", "win-border": "#D9D9DF", "bar": "#FBFBFC", "line": "#E4E4E9",
              "line-mid": "#ECECF0", "line-soft": "#F0F0F3", "side": "#F6F6F8", "hover": "#F1F1F4",
              "side-hover": "#E6E6EA", "side-on": "#E4E4EA", "seg": "#EEEEF2", "seg-on": "#FFFFFF", "field": "#FFFFFF",
              "field-border": "#DEDEE4", "text": "#17181C", "text-2": "#2A2D35", "text-3": "#3D414C", "muted": "#5A5F6B",
              "faint": "#9A9EA8", "head": "#FAFAFB", "row-hover": "#FAFAFB", "chk": "#F7F7FA", "chip-border": "#E8E8ED",
              "knob-off": "#8B8F99", "inv": "#17181C", "inv-fg": "#FFFFFF", "inv-muted": "#C9CAD1", "avatar": "#3F3F46",
              "link": "#4338CA", "ok-fg": "#1D6B36", "ok-bg": "#E5F4EA", "ok-dot": "#1F9D55", "warn-fg": "#7A4F00",
              "warn-bg": "#FFF1CC", "warn-dot": "#D69E2E", "bad-fg": "#A3191F", "bad-bg": "#FDE7E7", "bad-dot": "#D64545",
              "err-fg": "#8F3A00", "err-bg": "#FFE9D9", "err-dot": "#DD6B20", "hold-fg": "#3A4150", "hold-bg": "#ECEEF2",
              "none-fg": "#4F5563", "none-bg": "#F3F3F6", "acc-fg": "#3730A3", "acc-bg": "#EEF0FF", "acc-dot": "#4F46E5",
              "open-dot": "#9A9EA8", "draft-dot": "#C4C6CC", "admin-bg": "#FFF4E5", "admin-line": "#F5D9AE",
              "admin-fg": "#7A4100"},
    "dark": {"page": "#0B0B0E", "win": "#16171B", "win-border": "#2C2D33", "bar": "#1B1C21", "line": "#2A2B31",
             "line-mid": "#25262C", "line-soft": "#202127", "side": "#131418", "hover": "#23242A",
             "side-hover": "#212228", "side-on": "#2A2B33", "seg": "#0F1013", "seg-on": "#2E2F37", "field": "#111215",
             "field-border": "#33343C", "text": "#ECEDF0", "text-2": "#D3D5DB", "text-3": "#B4B7C0", "muted": "#959AA5",
             "faint": "#5F636D", "head": "#191A1F", "row-hover": "#1C1D22", "chk": "#1D1E25", "chip-border": "#2A2B31",
             "knob-off": "#666B77", "inv": "#ECEDF0", "inv-fg": "#16171B", "inv-muted": "#4F5563", "avatar": "#4A4B55",
             "link": "#A5A8FF", "ok-fg": "#7BD9A4", "ok-bg": "#15301F", "ok-dot": "#3FBF77", "warn-fg": "#F2C462",
             "warn-bg": "#33280F", "warn-dot": "#E0A93A", "bad-fg": "#FF9494", "bad-bg": "#3A1A1C", "bad-dot": "#F06363",
             "err-fg": "#FFB27A", "err-bg": "#3A2312", "err-dot": "#F08A3E", "hold-fg": "#C3C7D1", "hold-bg": "#262830",
             "none-fg": "#A9ADB8", "none-bg": "#202127", "acc-fg": "#B4B6FF", "acc-bg": "#23244A", "acc-dot": "#7C77FF",
             "open-dot": "#6B6F79", "draft-dot": "#4A4C55", "admin-bg": "#2E2210", "admin-line": "#4A3615",
             "admin-fg": "#F2C462"},
}
# What the window shows for each verdict poll() returns. tone picks the PALETTE colors: f"{tone}-fg", -bg and -dot.
VERDICTS = {
    "👍": {"label": "Thumbs up", "tone": "ok", "icon": "thumb"},
    "no major issues": {"label": "No major issues", "tone": "ok", "icon": "check"},
    "👀": {"label": "Reviewing", "tone": "warn", "icon": "eye"},
    "merge conflict": {"label": "Merge conflict", "tone": "bad", "icon": "alert"},
    "?": {"label": "Lookup failed", "tone": "err", "icon": "help"},
    "don't merge": {"label": "Don't merge", "tone": "hold", "icon": "stop"},
    None: {"label": "Awaiting Codex", "tone": "none", "icon": "clock"},
}
# The sidebar's views, in its order: the verdicts each one lists (None is every PR), its icon and that icon's color.
VIEWS = {
    "all": {"label": "All open", "verdicts": None, "icon": "inbox", "color": "text-3"},
    "ready": {"label": "Ready to merge", "verdicts": ("👍", "no major issues"), "icon": "check", "color": "ok-dot"},
    "reviewing": {"label": "Codex reviewing", "verdicts": ("👀",), "icon": "eye", "color": "warn-dot"},
    "attention": {"label": "Needs attention", "verdicts": ("merge conflict", "?"), "icon": "alert", "color": "bad-dot"},
    "waiting": {"label": "Awaiting Codex", "verdicts": (None,), "icon": "clock", "color": "muted"},
    "held": {"label": "Don't merge", "verdicts": ("don't merge",), "icon": "stop", "color": "hold-fg"},
}
# The activity log's kinds: each one's tone (as in VERDICTS) and the words that mark a line as it. The first match
# wins, so a conflict comment that failed is an error. A line with none of them is "info".
KINDS = (
    ("notice", "acc", ("notified:",)),  # first: the rest of such a line is a PR's title, which can say anything
    ("error", "err", ("failed", "crashed", "can't")),
    ("conflict", "bad", ("merge conflict",)),
    ("review", "warn", ("asked codex", "codex is reviewing")),
    ("codex", "ok", ("squash merging",)),
    ("merge", "acc", ("merge queued", "merged now", "not merged", "auto-merge")),
    ("poll", "hold", ("open pr(s)", "review yet", "skipping")),
)
SIDEBAR = (160, 480)  # the narrowest and widest sidebar a saved width restores
DETAIL = (280, 520)  # and the same for the detail panel
# The window's icons, after the design's (Lucide): strokes on a 24-unit grid, each a polyline x0, y0, x1, y1, ...
# or a ring ("o", cx, cy, r). A polyline that doesn't move is a dot.
ICONS = {
    "search": (("o", 11, 11, 7.5), (16.5, 16.5, 21, 21)),
    "inbox": ((2, 12, 5.5, 5, 7.2, 4, 16.8, 4, 18.5, 5, 22, 12, 22, 18, 20, 20, 4, 20, 2, 18, 2, 12, 8, 12, 10, 15,
               14, 15, 16, 12, 22, 12),),
    "thumb": ((7, 10, 7, 22), (15, 5.9, 14, 10, 19.8, 10, 21.3, 10.8, 21.8, 12.6, 19.4, 20.6, 18.6, 21.6, 17.5, 22,
                               4, 22, 2.6, 21.4, 2, 20, 2, 12, 2.6, 10.6, 4, 10, 6.8, 10, 8.5, 8.9, 12, 2, 13.8, 2.6,
                               15, 4.3, 15, 5.9)),
    "check": (("o", 12, 12, 10), (8, 12.5, 11, 15.5, 16.5, 9.5)),
    "eye": ((2, 12, 5, 8, 8.5, 5.8, 12, 5, 15.5, 5.8, 19, 8, 22, 12, 19, 16, 15.5, 18.2, 12, 19, 8.5, 18.2, 5, 16,
             2, 12), ("o", 12, 12, 3)),
    "alert": ((12, 3.5, 22, 20.5, 2, 20.5, 12, 3.5), (12, 9.5, 12, 13.5), (12, 17, 12, 17)),
    "help": (("o", 12, 12, 10), (9.2, 9.2, 10, 7.6, 12, 7, 14, 7.8, 14.8, 9.6, 13.8, 11.4, 12, 12.6, 12, 13.6),
             (12, 17, 12, 17)),
    "stop": ((7.9, 2, 16.1, 2, 22, 7.9, 22, 16.1, 16.1, 22, 7.9, 22, 2, 16.1, 2, 7.9, 7.9, 2), (8, 12, 16, 12)),
    "clock": (("o", 12, 12, 10), (12, 6.5, 12, 12, 15.5, 14)),
    "shield": ((12, 22, 6.5, 18.5, 4, 13, 4, 5, 12, 2, 20, 5, 20, 13, 17.5, 18.5, 12, 22),),
    "pause": (("o", 12, 12, 10), (10, 9, 10, 15), (14, 9, 14, 15)),
    "sliders": ((4, 21, 4, 14), (4, 10, 4, 3), (12, 21, 12, 12), (12, 8, 12, 3), (20, 21, 20, 16), (20, 12, 20, 3),
                (1, 14, 7, 14), (9, 8, 15, 8), (17, 16, 23, 16)),
    "gear": (("o", 12, 12, 3), (21.9, 10.6, 21.9, 13.4, 19.1, 13.9, 18.4, 15.7, 20, 18, 18, 20, 15.7, 18.4, 13.9, 19.1,
                                13.4, 21.9, 10.6, 21.9, 10.1, 19.1, 8.3, 18.4, 6, 20, 4, 18, 5.6, 15.7, 4.9, 13.9,
                                2.1, 13.4, 2.1, 10.6, 4.9, 10.1, 5.6, 8.3, 4, 6, 6, 4, 8.3, 5.6, 10.1, 4.9, 10.6, 2.1,
                                13.4, 2.1, 13.9, 4.9, 15.7, 5.6, 18, 4, 20, 6, 18.4, 8.3, 19.1, 10.1, 21.9, 10.6)),
    "book": ((4, 19.5, 4, 4.5, 4.7, 2.7, 6.5, 2, 20, 2, 20, 22, 6.5, 22, 4.7, 21.3, 4, 19.5, 4.7, 17.7, 6.5, 17,
              20, 17),),
    "merging": (("o", 18, 18, 3), ("o", 6, 6, 3), (6, 21, 6, 9, 6.7, 12.4, 8.6, 15.4, 11.6, 17.3, 15, 18)),
    "sparkle": ((12, 3, 13.9, 8.1, 19, 10, 13.9, 11.9, 12, 17, 10.1, 11.9, 5, 10, 10.1, 8.1, 12, 3),),
    "bell": ((3, 17, 4.6, 15.4, 5.6, 12.5, 6, 8, 6.8, 5, 9, 2.8, 12, 2, 15, 2.8, 17.2, 5, 18, 8, 18.4, 12.5,
              19.4, 15.4, 21, 17, 3, 17), (10.3, 21, 11.1, 21.8, 12.9, 21.8, 13.7, 21)),
    "trash": ((3, 6, 21, 6), (8, 6, 8, 4, 10, 2, 14, 2, 16, 4, 16, 6), (19, 6, 18, 20, 16, 22, 8, 22, 6, 20, 5, 6)),
    "plus": ((12, 5, 12, 19), (5, 12, 19, 12)),
    "merge": (("o", 6, 6, 3), ("o", 18, 18, 3), (6, 9, 6, 21), (6, 9, 6.7, 12.4, 8.6, 15.4, 11.6, 17.3, 15, 18)),
    "external": ((15, 3, 21, 3, 21, 9), (10, 14, 21, 3), (18, 13, 18, 19, 17.4, 20.4, 16, 21, 5, 21, 3.6, 20.4, 3, 19,
                                                          3, 8, 3.6, 6.6, 5, 6, 11, 6)),
    "arrow": ((5, 12, 19, 12), (13, 6, 19, 12, 13, 18)),
    "tick": ((20, 6, 9, 17, 4, 12),),
    "bang": ((12, 7, 12, 13), (12, 17, 12, 17)),
    "minus": ((6, 12, 18, 12),),
    "x": ((6, 6, 18, 18), (18, 6, 6, 18)),
    "down": ((6, 9, 12, 15, 18, 9),),
    "right": ((9, 6, 15, 12, 9, 18),),
}


def system_dark(root):
    """Whether macOS is in dark mode. Always False elsewhere."""
    from tkinter import TclError
    root.update_idletasks()  # Tk can't read a window's appearance before it exists
    try:  # Tk 8.6.10+; reports the window's appearance, which follows the system while it's "auto"
        return bool(int(root.tk.call("::tk::unsupported::MacWindowStyle", "isdark", root._w)))
    except (TclError, ValueError):
        pass
    if sys.platform != "darwin":
        return False
    try:  # the key only exists in dark mode
        r = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True, timeout=5, close_fds=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.stdout.strip() == "Dark"


def mix(a, b, t):
    """The color t of the way from a to b. Tk has no alpha, so the design's translucent colors are blended here."""
    return "#" + "".join(f"{round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02X}" for i in (1, 3, 5))


@functools.lru_cache(maxsize=None)
def draw(layers, width, height):
    """A PNG with alpha, drawn smooth: Tk's own canvas doesn't antialias on X11 or Windows.

    layers is ((color, stroke width, shapes), ...), painted in that order. Shapes are as in ICONS, on a grid
    24 units high whatever the height in pixels.
    """
    unit = 24 / height
    pixels = bytearray(width * height * 4)
    for color, stroke, shapes in layers:
        rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        rings = [shape[1:] for shape in shapes if shape[0] == "o"]
        lines = [(shape[i], shape[i + 1], shape[i + 2] - shape[i], shape[i + 3] - shape[i + 1])
                 for shape in shapes if shape[0] != "o" for i in range(0, len(shape) - 2, 2)]
        for at in range(width * height):
            x, y = (at % width + 0.5) * unit, (at // width + 0.5) * unit
            near = min([abs(math.hypot(x - cx, y - cy) - r) for cx, cy, r in rings], default=math.inf)
            for ax, ay, dx, dy in lines:
                t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy or 1)))
                near = min(near, math.hypot(x - ax - t * dx, y - ay - t * dy))
            alpha = max(0, min(1, 0.5 + (stroke / 2 - near) / unit))  # how much of this pixel the stroke covers
            if alpha:
                under = pixels[at * 4 + 3] / 255 * (1 - alpha)
                for i in range(3):
                    pixels[at * 4 + i] = round((rgb[i] * alpha + pixels[at * 4 + i] * under) / (alpha + under))
                pixels[at * 4 + 3] = round((alpha + under) * 255)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def fonts(root):
    """The window's fonts, each cut from one of the system's: its UI font (TkDefaultFont) or its monospace one."""
    from tkinter import font
    if not hasattr(root, "fonts"):
        def cut(base, larger=0, weight="normal"):
            f = font.Font(root=root, name=base, exists=True).copy()
            size = f.actual("size")
            f.configure(size=size + (larger if size > 0 else -larger), weight=weight)  # a negative size is in pixels
            return f

        root.fonts = {"bold": cut("TkDefaultFont", weight="bold"), "small": cut("TkDefaultFont", -2),
                      "head": cut("TkDefaultFont", 3, "bold"),
                      "caps": cut("TkDefaultFont", -2, "bold"), "title": cut("TkDefaultFont", 6, "bold"),
                      "mono": cut("TkFixedFont")}
        root.fonts.update(hero=cut("TkDefaultFont", 13, "bold"), lead=cut("TkDefaultFont", 1))  # the first run's
    return root.fonts


def theme(root, dark, appearance="system"):
    """Restyle every widget under root in the light or dark palette, on ttk's clam theme."""
    import tkinter as tk
    from tkinter import font, ttk
    p = PALETTE["dark" if dark else "light"]
    f = fonts(root)
    root.update_idletasks()  # Tk can't set a window's appearance before it exists
    s = ttk.Style(root)
    s.theme_use("clam")  # the one built-in theme whose colors ttk.Style can fully set
    flat = {"bordercolor": p["field-border"], "lightcolor": p["field"], "darkcolor": p["field"]}
    s.configure(".", background=p["win"], foreground=p["text"], fieldbackground=p["field"],
                troughcolor=p["seg"], selectbackground=p["acc-bg"], selectforeground=p["acc-fg"],
                insertcolor=p["text"], focuscolor=p["acc-dot"], **flat)
    s.map(".", foreground=[("disabled", p["faint"])])
    s.configure("TFrame", background=p["win"])
    s.configure("TLabel", background=p["win"], foreground=p["text"])
    s.configure("TSeparator", background=p["line"])
    focus = [("focus", p["acc-dot"])]
    for field in ("TEntry", "TSpinbox"):
        s.configure(field, fieldbackground=p["field"], foreground=p["text"], background=p["field"],
                    arrowcolor=p["muted"], padding=4, **flat)
        s.map(field, bordercolor=focus, lightcolor=focus, arrowcolor=[("active", p["text"])])

    def button(name, bg, fg, edge, hover, pressed=None, **more):
        """A button style with every state's colors set, so none is looked up on a parent style."""
        over = [("pressed", pressed or hover), ("active", hover)]
        s.configure(name, background=bg, foreground=fg, bordercolor=edge, lightcolor=bg, darkcolor=bg, **more)
        s.map(name, background=over, lightcolor=over, darkcolor=over, foreground=[("disabled", mix(fg, bg, 0.6))],
              bordercolor=over if edge == bg else focus)  # a borderless one shows focus with its dashed ring alone

    # width 0: as wide as the text, as aqua's are, rather than clam's 11-character minimum
    button("TButton", p["field"], p["text"], p["field-border"], p["hover"], p["side-on"], padding=(10, 4), width=0)
    # The design's primary button is ink on paper: near-black in light, near-white in dark.
    button("Primary.TButton", p["inv"], p["inv-fg"], p["inv"], p["text-2"], p["text-3"], font=f["bold"])
    # Radiobuttons drawn as one segmented control: the chosen segment lifts out of the track.
    seg = {"bordercolor": p["seg"], "lightcolor": p["seg"], "darkcolor": p["seg"]}
    s.configure("Toolbutton", background=p["seg"], foreground=p["muted"], padding=(11, 4), relief="flat", **seg)
    s.map("Toolbutton", background=[("selected", p["seg-on"]), ("active", p["hover"])],
          foreground=[("selected", p["text"]), ("active", p["text"])],
          bordercolor=[("selected", p["field-border"])],
          lightcolor=[("selected", p["seg-on"])], darkcolor=[("selected", p["seg-on"])])
    for check in ("TCheckbutton", "TRadiobutton"):
        s.configure(check, background=p["win"], foreground=p["text"], indicatorbackground=p["field"],
                    indicatorforeground=p["inv-fg"], upperbordercolor=p["field-border"],
                    lowerbordercolor=p["field-border"], indicatorsize=14, indicatormargin=(0, 0, 6, 0))
        s.map(check, background=[("active", p["win"])],
              indicatorbackground=[("selected", p["inv"]), ("pressed", p["hover"])])
    rows = font.nametofont("TkDefaultFont").metrics("linespace") + 16
    s.configure("Treeview", background=p["win"], fieldbackground=p["win"], foreground=p["text"],
                bordercolor=p["win"], lightcolor=p["win"], darkcolor=p["win"], rowheight=rows, indent=18)
    s.map("Treeview", background=[("selected", p["acc-bg"])], foreground=[("selected", p["acc-fg"])])
    s.configure("Treeview.Heading", background=p["head"], foreground=p["muted"], relief="flat", font=f["caps"],
                bordercolor=p["line-mid"], lightcolor=p["head"], darkcolor=p["head"], padding=(8, 6))
    s.map("Treeview.Heading", background=[("active", p["hover"])])
    s.configure("Treeview.Item", padding=(2, 0, 10, 0))  # so a title cut short stops shy of the next column
    s.configure("Treeview.Cell", padding=(4, 0, 10, 0))  # and the right-aligned ages shy of the edge
    # The detail panel and the Merge now sheet. The panel's status card comes in every verdict's tone.
    for tone in {look["tone"] for look in VERDICTS.values()}:
        s.configure(f"{tone}.Card.TFrame", background=p[f"{tone}-bg"])
        s.configure(f"{tone}.Card.TLabel", background=p[f"{tone}-bg"], foreground=p["text-2"], font=f["small"])
        s.configure(f"Title.{tone}.Card.TLabel", foreground=p[f"{tone}-fg"], font=f["bold"])
    s.configure("Head.TLabel", font=f["head"])
    s.configure("Caps.TLabel", foreground=p["muted"], font=f["caps"])
    s.configure("Note.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Mono.Note.TLabel", font=f["mono"])
    s.configure("Chip.TLabel", background=p["hover"], foreground=p["text-3"], font=f["mono"], padding=(6, 2))
    # A PR the sheet lists as skipped: a muted row, with why in the conflict color.
    s.configure("Skip.TFrame", background=p["row-hover"])
    s.configure("Skip.TLabel", background=p["row-hover"], foreground=p["none-fg"])
    s.configure("Why.Skip.TLabel", foreground=p["bad-fg"], font=f["small"])
    s.configure("Mono.Skip.TLabel", foreground=p["muted"], font=f["mono"])
    button("Pane.TButton", p["field"], p["text"], p["field-border"], p["hover"], p["side-on"], padding=(10, 7))
    button("Icon.TButton", p["win"], p["text-3"], p["win"], p["hover"], padding=4)
    # Merge now is the design's accent with white on it, in either appearance. Its focus ring is white too: the
    # sheet's Return key goes by which button has it, so it has to show.
    accent = PALETTE["light"]["acc-dot"]
    button("Accent.TButton", accent, "#FFFFFF", accent, accent, font=f["bold"], padding=(14, 7), focuscolor="#FFFFFF")
    faded = mix(accent, p["win"], 0.65)
    over = [("disabled", faded), ("pressed", mix(accent, "#000000", 0.26)), ("active", mix(accent, "#000000", 0.14))]
    s.map("Accent.TButton", background=over, lightcolor=over, darkcolor=over, bordercolor=over,
          foreground=[("disabled", mix("#FFFFFF", faded, 0.6 if dark else 0.3))])
    # The heading's stat chips: a hairline box around a small label with its number under it.
    s.configure("Stat.TFrame", background=p["chip-border"])
    s.configure("Stat.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Value.Stat.TLabel", foreground=p["text"], font=f["head"])
    # A scrollbar is a thumb and nothing else: no arrows, no visible trough.
    thumb = mix(p["win"], p["faint"], 0.5)
    s.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"sticky": "nswe"})]})])
    s.configure("Vertical.TScrollbar", background=thumb, troughcolor=p["win"], bordercolor=p["win"],
                lightcolor=thumb, darkcolor=thumb, arrowsize=9, gripcount=0)
    s.map("Vertical.TScrollbar", background=[("active", p["faint"])], lightcolor=[("active", p["faint"])],
          darkcolor=[("active", p["faint"])])
    s.configure("TPanedwindow", background=p["win"])
    s.configure("Sash", sashthickness=5, gripcount=0)

    # The window's own styles. Each surface the design puts things on gets a frame style and label styles.
    for name, bg, fg in (("Bar", p["bar"], p["text-3"]), ("Side", p["side"], p["text"]),
                         ("Field", p["field"], p["text"]), ("Admin", p["admin-bg"], p["admin-fg"]),
                         ("Paused", p["hover"], p["text-3"]), ("Sel", p["inv"], p["inv-fg"])):
        s.configure(f"{name}.TFrame", background=bg)
        s.configure(f"{name}.TLabel", background=bg, foreground=fg)
        s.configure(f"Bold.{name}.TLabel", font=f["bold"])
        s.configure(f"Muted.{name}.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Title.TLabel", font=f["title"])
    s.configure("Bold.TLabel", font=f["bold"])
    s.configure("Muted.TLabel", foreground=p["muted"])
    for name, color in (("Line", p["line"]), ("Rule", p["line-mid"]), ("AdminLine", p["admin-line"]),
                        ("PausedLine", p["line"]), ("Seg", p["seg"])):
        s.configure(f"{name}.TFrame", background=color)
    # The search field: a bordered frame around an icon and an entry with no border of its own.
    s.configure("Field.TFrame", relief="solid", borderwidth=1, bordercolor=p["field-border"])
    s.map("Field.TFrame", bordercolor=focus)
    bare = [("focus", p["field"])]
    s.configure("Search.TEntry", padding=(2, 3), bordercolor=p["field"], lightcolor=p["field"], darkcolor=p["field"])
    s.map("Search.TEntry", bordercolor=bare, lightcolor=bare)
    s.configure("Hint.Field.TLabel", foreground=p["faint"])
    # Auto-merge's switch: the checkbutton's image is the switch, so its own indicator is left out.
    s.layout("Switch.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})])
    s.configure("Switch.TCheckbutton", background=p["bar"], padding=(2, 3), space=8)
    s.map("Switch.TCheckbutton", background=[("active", p["bar"])])
    button("Link.TButton", p["hover"], p["link"], p["hover"], p["side-on"], font=f["bold"], padding=(8, 2))
    # Sidebar rows light up under the pointer (active) and while chosen (selected).
    lit = [("selected", p["side-on"]), ("active", p["side-hover"])]
    s.map("Row.Side.TFrame", background=lit)
    s.map("Row.Side.TLabel", background=lit)
    s.map("Name.Row.Side.TLabel", font=[("selected", f["bold"])])
    s.configure("Sub.Row.Side.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Count.Row.Side.TLabel", foreground=p["muted"], font=f["mono"])
    s.configure("Caps.Side.TLabel", foreground=p["muted"], font=f["caps"])
    s.configure("Avatar.Side.TLabel", foreground="#FFFFFF", font=f["caps"])
    button("Side.TButton", p["side"], p["text-3"], p["side"], p["side-hover"], padding=4)
    # The selection bar is the primary button's ink, with buttons outlined in a faint version of its paper.
    button("Sel.TButton", p["inv"], p["inv-fg"], mix(p["inv"], p["inv-fg"], 0.25), mix(p["inv"], p["inv-fg"], 0.14),
           padding=(8, 4))
    button("Flat.Sel.TButton", p["inv"], p["inv-fg"], p["inv"], mix(p["inv"], p["inv-fg"], 0.14), padding=6)
    button("Primary.Sel.TButton", p["inv-fg"], p["inv"], p["inv-fg"], mix(p["inv-fg"], p["inv"], 0.12), font=f["bold"])
    s.map("Primary.Sel.TButton", background=[("disabled", mix(p["inv"], p["inv-fg"], 0.3)), ("pressed", p["hover"]),
                                             ("active", p["hover"])])
    # The activity log's header.
    button("Bar.TButton", p["bar"], p["text"], p["bar"], p["hover"], font=f["bold"], padding=(6, 3))
    for name, tone in (("Live", "ok"), ("Idle", "hold")):
        s.configure(f"{name}.TLabel", background=p[f"{tone}-bg"], foreground=p[f"{tone}-fg"], font=f["small"],
                    padding=(7, 1, 8, 1))
    # The settings window and the first run. A card is a bordered group of rows, each a title over smaller help.
    s.configure("Group.TFrame", background=p["chk"])
    s.configure("Edge.Group.TFrame", relief="solid", borderwidth=1, bordercolor=p["line"])
    s.configure("GroupRule.TFrame", background=p["line-mid"])
    s.configure("Group.TLabel", background=p["chk"], foreground=p["text"])
    s.configure("Bold.Group.TLabel", font=f["bold"])
    s.configure("Muted.Group.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Value.Group.TLabel", foreground=p["text-3"])
    s.configure("Mono.Group.TLabel", font=f["mono"])
    s.configure("Code.Group.TLabel", background=p["hover"], font=f["mono"], padding=(6, 2))
    s.configure("Tile.Group.TLabel", foreground="#FFFFFF", font=f["caps"])
    s.configure("Group.Switch.TCheckbutton", background=p["chk"], padding=0)
    s.map("Group.Switch.TCheckbutton", background=[("active", p["chk"])])
    button("Group.TButton", p["chk"], p["muted"], p["chk"], p["hover"], padding=5)
    for name, tone in (("Ok", "ok"), ("Bad", "bad"), ("Wait", "hold")):  # a repo's merge rights
        s.configure(f"{name}.Rights.TLabel", background=p[f"{tone}-bg"], foreground=p[f"{tone}-fg"], font=f["small"],
                    padding=(7, 1, 8, 1))
    s.configure("Section.TLabel", foreground=p["muted"], font=f["caps"])
    s.configure("Fineprint.TLabel", foreground=p["muted"], font=f["small"])
    s.configure("Problem.TLabel", foreground=p["bad-fg"], font=f["small"])
    # The settings window's tabs: an icon over a name, the chosen one on a darker patch.
    tab = [("selected", p["side-on"]), ("active", p["side-hover"])]
    s.configure("Tab.Toolbutton", background=p["side"], foreground=p["text-3"], font=f["small"], padding=(8, 6, 8, 5),
                bordercolor=p["side"], lightcolor=p["side"], darkcolor=p["side"])
    s.map("Tab.Toolbutton", background=tab, bordercolor=tab, lightcolor=tab, darkcolor=tab,
          foreground=[("selected", p["acc-fg"])])
    # One of a few choices, lit while it's the one chosen: an appearance's name, a merge mode's card.
    s.map("Choice.TLabel", font=[("selected", f["bold"])])
    lit = [("selected", p["acc-bg"])]
    s.configure("Pick.TFrame", background=p["chk"], relief="solid", borderwidth=1, bordercolor=p["line"],
                lightcolor=p["chk"], darkcolor=p["chk"])
    s.map("Pick.TFrame", background=lit, lightcolor=lit, darkcolor=lit, bordercolor=[("selected", p["acc-dot"])])
    s.configure("Pick.TLabel", background=p["chk"], foreground=p["text-3"], font=f["small"])
    s.map("Pick.TLabel", background=lit)
    s.configure("Flag.Pick.TLabel", foreground=p["muted"], font=f["mono"])
    # The card's radio button draws its own dot, as the switch does.
    s.layout("Pick.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
        ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})]})])
    s.configure("Pick.TRadiobutton", background=p["chk"], font=f["bold"], padding=0, space=12)
    s.map("Pick.TRadiobutton", background=[("selected", p["acc-bg"]), ("active", p["chk"])])
    # The first run: a large heading and a taller field on the window, the checks and the explainer on the side.
    s.configure("Hero.TLabel", font=f["hero"])
    s.configure("Lead.TLabel", foreground=p["none-fg"], font=f["lead"])
    s.configure("Big.TEntry", padding=(10, 8))
    s.configure("Big.Primary.TButton", padding=(18, 8))
    s.configure("Problem.Side.TLabel", foreground=p["bad-fg"], font=f["small"])
    s.configure("Step.Side.TLabel", font=f["caps"])
    button("Link.Side.TButton", p["side"], p["link"], p["side"], p["side-hover"], font=f["small"], padding=(6, 2))

    def plain(widget):  # tk widgets ttk.Style doesn't reach
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            try:  # the title bar, native menus and dialogs; "auto" keeps following the system
                root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", widget._w,
                             {"light": "aqua", "dark": "darkaqua"}.get(appearance, "auto"))
            except tk.TclError:
                pass
            widget.configure(background=p["win"])
        elif isinstance(widget, tk.Menu):
            widget.configure(background=p["win"], foreground=p["text"], activebackground=p["acc-dot"],
                             activeforeground="#FFFFFF", disabledforeground=p["faint"])
        elif isinstance(widget, tk.Text):
            widget.configure(background=p["field"], foreground=p["text"], insertbackground=p["text"],
                             selectbackground=p["acc-bg"], selectforeground=p["acc-fg"],
                             highlightthickness=1, highlightbackground=p["line"], highlightcolor=p["line"],
                             borderwidth=0, padx=6, pady=4)
        for child in widget.winfo_children():
            plain(child)

    plain(root)


def watch(repos, mode, interval):
    log = lambda msg: print(stamp(msg), flush=True)
    log(f"watching {', '.join(repos)} every {interval}s for a go-ahead from bot id {BOT_ID}")
    while True:
        for repo in repos:
            poll(repo, log, lambda: mode)
        time.sleep(interval)


def when(iso, now):
    """A GitHub timestamp as the detail panel's timeline says it: the local time of day if that was today, else how
    long ago, as age() counts it. "" if it's missing or unreadable."""
    try:
        at = time.localtime(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return ""
    if at[:3] == time.localtime(now)[:3]:
        return time.strftime("%H:%M", at)
    ago = age(iso, now)
    return ago if ago == "now" else f"{ago} ago"


def inset(shapes, scale):
    """ICONS shapes shrunk towards the middle of the grid, to sit inside a badge."""
    at = lambda c: 12 + (c - 12) * scale
    return tuple(("o", at(s[1]), at(s[2]), s[3] * scale) if s[0] == "o" else tuple(map(at, s)) for s in shapes)


def elide(text, font, width):
    """text, cut short with an ellipsis if it's wider than width pixels in font. A label only clips."""
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def badge(app, tone, mark, size=18):
    """A disc in a tone's colors with one of ICONS inside: a readiness check's, or a PR's on the Merge now sheet."""
    return app.image(("badge", tone, mark, size), lambda p: draw(
        ((p[f"{tone}-bg"], 24, ((12, 12, 12, 12),)), (p[f"{tone}-fg"], 2.6, inset(ICONS[mark], 0.5))), size, size))


def scroller(app, parent):
    """A frame that scrolls up and down in parent when it's the taller, by the wheel or by a scrollbar that's there
    only then. Returns the canvas it scrolls in, which the caller colors, and the frame to fill."""
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, yscrollincrement=16)
    canvas.grid(row=0, column=0, sticky="nsew")
    inner = ttk.Frame(canvas)
    held = canvas.create_window(0, 0, anchor="nw", window=inner)
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(held, width=event.width), add="+")
    inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=(0, 0, event.width, event.height)))
    app.scroll(canvas)

    def wheel(event):  # bound to the whole window, so act only with the pointer over this
        try:
            over = str(canvas.winfo_containing(event.x_root, event.y_root) or "")
        except KeyError:  # a window Tkinter didn't make, like a menu
            return
        if over.startswith(str(canvas)) and inner.winfo_height() > canvas.winfo_height():
            canvas.yview_scroll(-1 if event.num == 4 or event.delta > 0 else 1, "units")

    x11 = canvas.tk.call("tk", "windowingsystem") == "x11"  # which sends the wheel as buttons 4 and 5
    for gesture in ("<Button-4>", "<Button-5>") if x11 else ("<MouseWheel>",):
        canvas.winfo_toplevel().bind(gesture, wheel, add="+")
    return canvas, inner


class Detail:
    """The detail panel: a pane to the right of the table about the one selected PR, with the actions for it.

    It's there only while exactly one PR is selected (the selection bar has the actions for several) and the
    window is wide enough to keep the PR column beside it.
    """

    MARKS = {"ok": "tick", "warn": "bang", "bad": "x", "err": "x", "none": "minus"}  # a readiness check's badge
    # What the timeline says when it knows no times. poll() looks nothing up for a held or conflicting PR.
    BLANK = {"don't merge": "Not looked up while the pull request is held.",
             "merge conflict": "Not looked up while the pull request conflicts.",
             "?": "The lookup failed. prherd retries on the next poll."}

    def __init__(self, app, width):
        self.app, self.width = app, width
        self.shown = None  # what fill() last showed, so a poll that changed nothing redraws nothing
        self.branch_name = ""
        frame = self.frame = ttk.Frame(app.body, width=width)
        frame.pack_propagate(False)  # the saved width, not whatever its text asks for
        # Lowest of the body's children, so anything walking the window's widgets meets these buttons before the
        # selection bar's, which have the same labels: the tests find buttons by label, and mean the bar's.
        frame.lower()
        ttk.Frame(frame, style="Line.TFrame", width=1).pack(side="left", fill="y")
        self.build_actions()  # packed first, so they keep the foot of the pane however short the window
        holder = ttk.Frame(frame)
        holder.pack(fill="both", expand=True)
        self.canvas, inner = scroller(app, holder)
        self.build(inner)
        app.painters.append(lambda p: self.canvas.configure(background=p["win"]))
        self.canvas.bind("<Configure>", self.wrap, add="+")
        app.body.bind("<Configure>", lambda _: self.show(), add="+")  # the window was resized: is there room now?

    def build(self, inner):
        """All but the actions, top to bottom. It scrolls when the pane is too short for it."""
        app = self.app
        head = ttk.Frame(inner, padding=(20, 14, 15, 16))
        head.pack(fill="x")
        top = ttk.Frame(head)
        top.pack(fill="x")
        # Packed before the repo, so a long name is cut short and not these. Each keeps its name as its text,
        # though only the icon shows.
        for text, icon, command in (("Close", "x", lambda: app.tree.selection_set(())),
                                    ("Open on GitHub", "external", lambda: app.open_in_browser(app.selected_prs()))):
            ttk.Button(top, text=text, image=app.icon(icon, "text-3", 14), style="Icon.TButton", takefocus=False,
                       command=command).pack(side="right")
        self.repo = ttk.Label(top, style="Note.TLabel")
        self.repo.pack(side="left")
        self.number = ttk.Label(top, style="Mono.Note.TLabel")
        self.number.pack(side="left", padx=(6, 0))
        self.title = ttk.Label(head, style="Head.TLabel")
        self.title.pack(anchor="w", pady=(6, 10))
        branches = ttk.Frame(head)
        branches.pack(fill="x")
        self.branch = ttk.Label(branches, style="Chip.TLabel")
        self.arrow = ttk.Label(branches, image=app.icon("arrow", "muted", 12))
        self.base = ttk.Label(branches, style="Chip.TLabel")
        ttk.Frame(inner, style="Rule.TFrame", height=1).pack(fill="x")

        body = ttk.Frame(inner, padding=(20, 16, 20, 8))
        body.pack(fill="x")
        card = self.card = ttk.Frame(body, padding=(14, 12))
        card.pack(fill="x")
        self.card_icon = ttk.Label(card)
        self.card_icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10), pady=(1, 0))
        self.card_title = ttk.Label(card)
        self.card_title.grid(row=0, column=1, sticky="w")
        self.card_text = ttk.Label(card)
        self.card_text.grid(row=1, column=1, sticky="w", pady=(2, 0))

        ttk.Label(body, text="MERGE READINESS", style="Caps.TLabel").pack(anchor="w", pady=(18, 8))
        self.checks = []
        for _ in range(5):  # readiness() always has five
            row = ttk.Frame(body)
            row.pack(fill="x", pady=(0, 8))
            mark, name, note = ttk.Label(row), ttk.Label(row), ttk.Label(row, style="Note.TLabel")
            mark.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
            name.grid(row=0, column=1, sticky="w")
            note.grid(row=1, column=1, sticky="w")
            self.checks.append((mark, name, note))

        ttk.Label(body, text="TIMELINE", style="Caps.TLabel").pack(anchor="w", pady=(10, 10))
        self.blank = ttk.Label(body, style="Note.TLabel")
        self.story = ttk.Frame(body)
        self.story.pack(fill="x")
        self.events = []  # a row for each event, made as fill() first needs it

    def build_actions(self):
        app = self.app
        foot = ttk.Frame(self.frame, padding=(20, 14, 20, 18))
        foot.pack(side="bottom", fill="x")
        ttk.Frame(self.frame, style="Rule.TFrame", height=1).pack(side="bottom", fill="x")
        for column in (0, 1):
            foot.columnconfigure(column, weight=1, uniform="half")
        white = app.image("merge, white", lambda p: draw((("#FFFFFF", 2.2, ICONS["merge"]),), 15, 15))
        self.merge = ttk.Button(foot, text="Merge now…", image=white, compound="left", style="Accent.TButton",
                                command=app.merge_now)
        self.merge.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.hold = ttk.Button(foot, text="Don't merge", style="Pane.TButton", command=self.toggle_hold)
        others = (ttk.Button(foot, text="Request review", style="Pane.TButton", command=app.ask_codex), self.hold,
                  ttk.Button(foot, text="Update branch", style="Pane.TButton", command=app.update_branches),
                  ttk.Button(foot, text="Copy link", style="Pane.TButton",
                             command=lambda: app.copy_links(app.selected_prs())))
        for at, button in enumerate(others):
            button.grid(row=1 + at // 2, column=at % 2, sticky="ew", padx=(0, 4) if at % 2 == 0 else (4, 0),
                        pady=(0, 8) if at < 2 else 0)

    def toggle_hold(self):
        """Don't merge, or Allow merge for a held PR: by its verdict now, whatever the button last said."""
        app = self.app
        app.set_dont_merge(not all(app.verdicts.get(iid) == "don't merge" for iid in app.selected_prs()))

    def measure(self):
        """The pane's width: as it is now if it's showing, else as it last was. save() keeps this."""
        if self.frame.winfo_ismapped():
            self.width = min(max(self.frame.winfo_width(), DETAIL[0]), DETAIL[1])
        return self.width

    def show(self):
        """Show the one selected PR as the app now knows it, or take the pane away."""
        app, body = self.app, self.app.body
        chosen = app.selected_prs()
        iid = chosen[0] if len(chosen) == 1 else None
        up = str(self.frame) in map(str, body.panes())
        room = body.winfo_width() - app.sidebar.winfo_width() - self.measure()
        if iid not in app.prs or room < app.tree.column("#0", "minwidth"):
            if up:
                body.forget(self.frame)
            return
        pr, v = app.prs[iid], app.verdicts[iid]
        detail = app.details.get(iid.rsplit("#", 1)[0], {}).get(pr["number"])
        state = (iid, pr, v, detail, app.live["merging"], time.strftime("%F"))  # the day: when() goes by it
        if state != self.shown:
            if not self.shown or self.shown[0] != iid:
                self.canvas.yview_moveto(0)
            self.fill(*state[:5])
            self.shown = state
        if not up:
            self.frame.configure(width=self.width)
            body.add(self.frame, weight=0)

    def fill(self, iid, pr, v, detail, merging):
        app, now = self.app, time.time()
        self.repo.configure(text=short(iid.rsplit("#", 1)[0]))
        self.number.configure(text=f"#{pr['number']}")
        self.title.configure(text=pr["title"])
        self.branch_name = pr.get("headRefName") or ""
        self.base.configure(text=pr.get("baseRefName") or "")
        for part in (self.branch, self.arrow, self.base):
            part.pack_forget()
        if self.branch_name:
            self.branch.pack(side="left")
            self.arrow.pack(side="left", padx=6)
        if pr.get("baseRefName"):
            self.base.pack(side="left")

        tone, title, text = headline(pr, v, merging)
        self.card.configure(style=f"{tone}.Card.TFrame")
        self.card_icon.configure(image=app.icon(VERDICTS[v]["icon"], f"{tone}-fg", 18), style=f"{tone}.Card.TLabel")
        self.card_title.configure(text=title, style=f"Title.{tone}.Card.TLabel")
        self.card_text.configure(text=text, style=f"{tone}.Card.TLabel")
        for (mark, name, note), (tone, title, text) in zip(self.checks, readiness(pr, v)):
            mark.configure(image=badge(app, tone, self.MARKS[tone]))
            name.configure(text=title)
            note.configure(text=text)
            note.grid() if text else note.grid_remove()

        events = timeline(pr, v, detail)
        while len(self.events) < len(events):
            self.events.append(self.event_row())
        for at, (row, dot, line, what, time_) in enumerate(self.events):
            row.pack_forget()
            line.pack_forget()
            if at < len(events):
                iso, tone, text = events[at]
                color = f"{tone}-dot"
                dot.configure(image=app.dot(color if color in app.palette else "open-dot", 9))
                what.configure(text=text)
                time_.configure(text=when(iso, now))
                if at < len(events) - 1:  # joined to the next one
                    line.pack(fill="y", expand=True, pady=(4, 0))
                row.pack(fill="x")
        self.blank.pack_forget()
        if not events:
            self.blank.configure(text=self.BLANK.get(v, "No times known for this head yet."))
            self.blank.pack(anchor="w", pady=(0, 10), before=self.story)

        self.hold.configure(text="Allow merge" if v == "don't merge" else "Don't merge")
        self.merge.state(["!disabled" if app.mergeable_now() else "disabled"])
        self.wrap()

    def event_row(self):
        """One timeline row: a dot on a rail, with a line down to the next row's, then what happened and when."""
        row = ttk.Frame(self.story)
        row.columnconfigure(1, weight=1)
        rail = ttk.Frame(row)
        rail.grid(row=0, column=0, sticky="ns", padx=(4, 15))  # the dot sits under the readiness badges' middles
        dot = ttk.Label(rail)
        dot.pack(pady=(4, 0))
        line = ttk.Frame(rail, style="Line.TFrame", width=1)
        what = ttk.Label(row)
        what.grid(row=0, column=1, sticky="nw", pady=(0, 12))
        time_ = ttk.Label(row, style="Mono.Note.TLabel")
        time_.grid(row=0, column=2, sticky="ne", padx=(8, 0), pady=(1, 0))
        return row, dot, line, what, time_

    def wrap(self, _event=None):
        """Wrap the text to the pane's width, and cut the branch short to fit on one line with its base."""
        width = self.canvas.winfo_width() - 44  # less the gutters, and the 2px clam keeps around a label's text
        if width < 100:  # not drawn yet
            return
        mono = fonts(self.app.root)["mono"]
        self.title.configure(wraplength=width)
        # Beside the branch: the arrow and the base, and both chips' padding.
        self.branch.configure(text=elide(self.branch_name, mono, width - mono.measure(self.base.cget("text")) - 56))
        for label in (self.card_title, self.card_text):
            label.configure(wraplength=width - 60)  # inside the card's padding, past its icon
        for _, name, note in self.checks:
            name.configure(wraplength=width - 32)
            note.configure(wraplength=width - 32)
        for _, _, _, what, time_ in self.events:
            what.configure(wraplength=width - 44 - mono.measure(time_.cget("text")))
        self.blank.configure(wraplength=width)


class MergeSheet:
    """Merge now's question: a window over the main one listing what will merge and at which commit, what's skipped
    and why, and how the mode treats branch protection.

    Nothing waits on it: merge() is called if Merge is chosen, and otherwise it just closes. A merge can't be
    undone, so Cancel has the focus, and Return and Escape cancel unless the focus was moved to Merge.
    """

    WIDTH = 432  # of what's inside its margins
    ARMED = 0.6  # seconds before Merge answers, so a double click that opened the sheet can't also confirm it
    NOTES = {"👀": "Codex still reviewing", "?": "Codex's verdict unknown", None: "no Codex go-ahead yet"}
    SKIPS = {"don't merge": "marked don't merge", "merge conflict": "merge conflict"}

    def __init__(self, app, chosen, mode, merge):
        """chosen is {tree row: its PR} to merge with mode. The rest of the selection is listed as skipped."""
        from tkinter import font
        if app.sheet:
            app.sheet.close()
        self.app, self.merge, self.opened = app, merge, time.monotonic()
        app.sheet = self
        f, plain, one = fonts(app.root), font.nametofont("TkDefaultFont"), len(chosen) == 1
        top = self.top = tk.Toplevel(app.root)
        top.withdraw()  # until it's themed and in place
        top.title("Merge now")
        top.transient(app.root)
        top.resizable(False, False)
        page = ttk.Frame(top, padding=(24, 22, 24, 20))
        page.pack(fill="both", expand=True)

        head = ttk.Frame(page)
        head.pack(fill="x")
        tile = app.image("merge, tiled", lambda p: draw(
            ((p["acc-bg"], 12, ((6, 6, 18, 6, 18, 18, 6, 18, 6, 6), (6, 12, 18, 12))),
             (p["acc-fg"], 1.2, inset(ICONS["merge"], 0.5))), 40, 40))
        ttk.Label(head, image=tile).pack(side="left", anchor="n", padx=(0, 14))
        ttk.Label(head, text="Merge this pull request now?" if one else f"Merge {len(chosen)} pull requests now?",
                  style="Head.TLabel").pack(anchor="w")
        listed = [(iid, pr, None) for iid, pr in chosen.items()]
        listed += [(iid, app.prs[iid], self.SKIPS.get(app.verdicts.get(iid), "can't be merged"))
                   for iid in app.selected_prs() if iid not in chosen]
        skipped = len(listed) - len(chosen)  # said up here too: in a long list they're below the fold
        ttk.Label(head, style="Muted.TLabel", wraplength=self.WIDTH - 58, justify="left", text=(
            "It squash-merges right away, without waiting for Codex. The merge is pinned to the commit shown, so "
            "GitHub refuses it if someone has pushed since." if one else
            "These squash-merge right away, without waiting for Codex. Each merge is pinned to the commit shown, so "
            "GitHub refuses it if someone has pushed since.") + (
            f" {skipped} more {'is' if skipped == 1 else 'are'} skipped, and listed last with why." if skipped else "")
        ).pack(anchor="w", pady=(4, 0))

        box = ttk.Frame(page, style="Line.TFrame", padding=1)  # the list's border
        box.pack(fill="x", pady=(18, 0))
        self.canvas, rows = scroller(app, box)
        fold = 4.5  # the rows it shows before it scrolls: the half says there are more
        for at, (iid, pr, why) in enumerate(listed):
            if at:
                ttk.Frame(rows, style="Rule.TFrame", height=1).pack(fill="x")
            on = "Skip." if why else ""
            row = ttk.Frame(rows, style=f"{on}TFrame", padding=(12, 9))
            row.pack(fill="x")
            row.columnconfigure(1, weight=1)
            # A PR that merges has the commit it merges at on its right; a skipped one has its number there.
            side = f"#{pr['number']}" if why else head7(pr)
            # What's left for the title: less the row's padding, the badge, the side and a scrollbar if there is one.
            room = self.WIDTH - 90 - f["mono"].measure(side) - (12 if len(listed) > fold else 0)
            sub = f"Skipped — {why}" if why else " · ".join(filter(None, (
                f"{short(iid.rsplit('#', 1)[0])} #{pr['number']}", self.NOTES.get(app.verdicts.get(iid)),
                "draft" if pr.get("isDraft") else "")))
            ttk.Label(row, image=badge(app, "bad" if why else "ok", "x" if why else "tick"),
                      style=f"{on}TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 10))
            ttk.Label(row, text=elide(pr["title"], plain, room), style=f"{on}TLabel").grid(row=0, column=1, sticky="w")
            ttk.Label(row, text=elide(sub, f["small"], room),
                      style="Why.Skip.TLabel" if why else "Note.TLabel").grid(row=1, column=1, sticky="w")
            ttk.Label(row, text=side, style="Mono.Skip.TLabel" if why else "Chip.TLabel").grid(
                row=0, column=2, rowspan=2, padx=(10, 0))

        admin = mode == "admin"
        name = "Admin" if admin else next(label for label, value in MODES if value == app.live["mode"])
        look = "Admin" if admin else "Paused"  # the banners' surfaces: Admin's is the warning one
        note = ttk.Frame(page, style=f"{look}.TFrame", padding=(12, 10))
        note.pack(fill="x", pady=(14, 0))
        ttk.Label(note, image=app.icon("shield", "admin-fg" if admin else "text-3", 15),
                  style=f"{look}.TLabel").pack(side="left", anchor="n", padx=(0, 10), pady=(1, 0))
        ttk.Label(note, text=f"{name} mode", style=f"Bold.{look}.TLabel").pack(anchor="w")
        ttk.Label(note, style=f"{look}.TLabel", wraplength=self.WIDTH - 58, justify="left", text=(
            ("This merge bypasses branch protection." if one else "These merges bypass branch protection.") if admin
            else ("Merge now doesn't queue: it merges like Force. " if name == "Queue" else "")
            + "Branch protection still applies. Switch to Admin to bypass it.")).pack(anchor="w", pady=(1, 0))

        buttons = ttk.Frame(page)
        buttons.pack(fill="x", pady=(20, 0))
        self.cancel_button = ttk.Button(buttons, text="Cancel", style="Pane.TButton", default="active",
                                        command=self.close)
        self.merge_button = ttk.Button(buttons, text="Merge now" if one else f"Merge {len(chosen)} now",
                                       style="Accent.TButton", command=self.confirm)
        self.merge_button.pack(side="right")
        self.cancel_button.pack(side="right", padx=(0, 8))

        app.painters.append(self.paint)
        app.retheme(force=True)
        top.update_idletasks()  # now the rows know their heights
        tall = rows.winfo_children()[0].winfo_reqheight() + 1
        self.canvas.configure(width=self.WIDTH - 2, height=min(rows.winfo_reqheight(), int(fold * tall)))
        top.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - top.winfo_reqheight()) // 2
        top.geometry(f"+{x}+{y}")  # negative on a display left of or above the main one, and that's right
        top.deiconify()
        self.opened = time.monotonic()  # armed from when it shows, however long it took to build

        top.protocol("WM_DELETE_WINDOW", self.close)
        mac = ("<Command-period>", "<Command-w>") if sys.platform == "darwin" else ()
        for key in ("<Escape>", *mac):
            top.bind(key, lambda _: self.close())
        for key in ("<Return>", "<KP_Enter>"):  # the default is Cancel: only Merge itself, focused, confirms
            top.bind(key, lambda event: self.confirm() if event.widget is self.merge_button else self.close())
        self.cancel_button.focus_set()

        def grab(event=None):
            if event is None or event.widget is top:
                try:
                    top.grab_set()  # the main window waits behind it
                except tk.TclError:  # X11 can't grab a window that isn't on screen yet; <Map> comes back here
                    pass

        top.bind("<Map>", grab)
        grab()

    def paint(self, p):
        self.canvas.configure(background=p["win"])

    def confirm(self):
        if time.monotonic() - self.opened >= self.ARMED:
            self.close()
            self.merge()

    def close(self):
        """Take the sheet away, merging nothing. Destroying it lets go of the grab."""
        app = self.app
        if app.sheet is not self:  # already closed
            return
        app.painters.remove(self.paint)
        app.sheet = None
        self.top.destroy()
        app.tree.focus_set()


class MenuBar:
    """The macOS menu bar item: the app's mark beside how many PRs are ready to merge and how many need attention,
    over a menu with the same counts and the few things worth doing without the window.

    Tk can't make one. But on macOS Tk already runs an NSApplication in this process, and ctypes reaches the
    Objective-C runtime, so this makes an NSStatusItem by hand. Whatever goes wrong raises, from __init__ on: the
    caller then goes without a menu bar item. Call it on the Tk thread only, which is Cocoa's main thread.
    """

    # What the menu's items do. An item's tag is its action's place in these.
    ACTIONS = ("ready", "attention", "poll", "merging", "open", "settings", "quit")
    # The app's mark, after assets/prherd.png: two PRs joining one branch. Black, as a template image: macOS colors
    # those for a light or a dark menu bar.
    GLYPH = (("#000000", 1.9, ((6.9, 8.1, 16.7, 12), (6.9, 15.9, 16.7, 12))),
             ("#000000", 4.6, ((6.9, 8.1, 6.9, 8.1), (6.9, 15.9, 6.9, 15.9))),
             ("#000000", 5.8, ((16.7, 12, 16.7, 12),)))
    c = None  # ctypes, once load() has what it needs; then objc is libobjc and Target the class the menu's items call
    live = {}  # each item's Objective-C target -> its MenuBar, for chose()

    @classmethod
    def load(cls):
        """Load libobjc and make the Objective-C class the menu's items call. Once: a class can be registered only
        once, and its method has to outlive every item that could still call it."""
        if cls.c:
            return
        import ctypes as c
        import ctypes.util
        objc = c.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        for name, gives, takes in (("objc_getClass", c.c_void_p, [c.c_char_p]),
                                   ("sel_registerName", c.c_void_p, [c.c_char_p]),
                                   ("objc_allocateClassPair", c.c_void_p, [c.c_void_p, c.c_char_p, c.c_size_t]),
                                   ("class_addMethod", c.c_bool, [c.c_void_p, c.c_void_p, c.c_void_p, c.c_char_p]),
                                   ("objc_registerClassPair", None, [c.c_void_p])):
            getattr(objc, name).restype, getattr(objc, name).argtypes = gives, takes

        def chosen(target, _selector, sender):  # Cocoa calls this, so nothing may be raised out of it
            try:
                cls.chose(target, cls.send(c.c_long, sender, "tag"))
            except Exception:
                pass

        class Size(c.Structure):  # an NSSize
            _fields_ = [("width", c.c_double), ("height", c.c_double)]

        method = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_void_p)(chosen)
        # Named for this Python class, so a second copy of this module in the process would get its own.
        made = objc.objc_allocateClassPair(objc.objc_getClass(b"NSObject"), f"PrherdMenuTarget{id(cls):x}".encode(), 0)
        if not made or not objc.class_addMethod(made, objc.sel_registerName(b"chose:"), method, b"v@:@"):
            raise RuntimeError("can't make the Objective-C class for the menu")
        objc.objc_registerClassPair(made)
        cls.objc, cls.Target, cls.Size, cls.method, cls.c = objc, made, Size, method, c  # method: kept, or it's freed

    @classmethod
    def send(cls, restype, obj, selector, *args):
        """The Objective-C message [obj selector], returning the ctypes type restype. objc_msgSend has to be called
        as a C function of exactly the method's signature, so it's cast to one each time: a bool, a float, bytes
        and a struct go as what they are, and anything else (an object, a selector, an integer) as a pointer."""
        c = cls.c
        kinds = {bool: c.c_bool, float: c.c_double, bytes: c.c_char_p}
        takes = [type(arg) if isinstance(arg, c.Structure) else kinds.get(type(arg), c.c_void_p) for arg in args]
        function = c.CFUNCTYPE(restype, c.c_void_p, c.c_void_p, *takes)(("objc_msgSend", cls.objc))
        return function(obj, cls.objc.sel_registerName(selector.encode()), *args)

    @classmethod
    def msg(cls, obj, selector, *args):
        """A message that answers with an object. Never with none: a message to nothing answers nothing, quietly."""
        if isinstance(obj, str):
            obj = cls.objc.objc_getClass(obj.encode())  # a class, by name
        found = cls.send(cls.c.c_void_p, obj, selector, *args) if obj else None
        if not found:
            raise RuntimeError(f"Cocoa has no {selector}")
        return found

    @classmethod
    def tell(cls, obj, selector, *args):
        """A message with no answer."""
        cls.send(None, obj, selector, *args)

    @classmethod
    def chose(cls, target, tag):
        """A menu item was chosen: tell its MenuBar which. This is inside Cocoa's event handling, where Tk mustn't
        be called, so post() can only take note."""
        bar = cls.live.get(target)
        if bar and 0 <= tag < len(cls.ACTIONS):
            bar.post(cls.ACTIONS[tag])

    @staticmethod
    def words(ready, attention, merged):
        """What the counts are shown as: beside the mark, when the pointer rests on it, and as the menu's lines."""
        lines = [f"{ready} ready to merge", f"{attention} need{'s' if attention == 1 else ''} attention",
                 f"{merged} merged today"]
        # U+FE0E: the warning sign as text, in the menu bar's color, not as an emoji
        title = "  ".join(filter(None, (str(ready) if ready else "", f"\u26a0\ufe0e {attention}" if attention else "")))
        return title, f"prherd: {', '.join(lines)}", lines

    def __init__(self, post, settings=False):
        """post(action) is told which of ACTIONS was chosen from the menu, which has Settings… only if asked."""
        self.post, self.shown, self.item, self.target, self.held = post, None, None, None, []
        self.load()
        try:
            self.pooled(self.build, settings)
        except Exception:
            self.close()
            raise

    def pooled(self, work, *args):
        """work(*args) inside an autorelease pool of its own, so what Cocoa hands out along the way is freed after."""
        pool = self.msg(self.msg("NSAutoreleasePool", "alloc"), "init")
        try:
            work(*args)
        finally:
            self.tell(pool, "drain")

    def text(self, string):
        return self.msg("NSString", "stringWithUTF8String:", string.encode())

    def hold(self, obj):
        """obj, which is this object's to release: close() does."""
        self.held.append(obj)
        return obj

    def build(self, settings):
        msg, tell = self.msg, self.tell
        self.target = self.hold(msg(msg(self.Target, "alloc"), "init"))
        self.live[self.target] = self
        # -1.0: as wide as what it shows. The status bar doesn't keep the item alive, so this does.
        self.item = self.hold(msg(msg(msg("NSStatusBar", "systemStatusBar"), "statusItemWithLength:", -1.0), "retain"))
        self.button = msg(self.item, "button")
        png = draw(self.GLYPH, 36, 36)  # twice the size it's shown at, for a Retina screen
        image = self.hold(msg(msg("NSImage", "alloc"), "initWithData:",
                              msg("NSData", "dataWithBytes:length:", png, len(png))))
        tell(image, "setSize:", self.Size(18, 18))
        tell(image, "setTemplate:", True)
        tell(self.button, "setImage:", image)
        tell(self.button, "setImagePosition:", 2)  # NSImageLeft: the counts come after the mark
        menu = self.hold(msg(msg("NSMenu", "alloc"), "init"))
        tell(menu, "setAutoenablesItems:", False)  # or Cocoa would switch on the lines that are only there to be read

        def add(title, action=None):
            chose = self.objc.sel_registerName(b"chose:") if action else None
            item = self.hold(msg(msg("NSMenuItem", "alloc"), "initWithTitle:action:keyEquivalent:", self.text(title),
                                 chose, self.text("")))
            if action:
                tell(item, "setTarget:", self.target)
                tell(item, "setTag:", self.ACTIONS.index(action))
            tell(item, "setEnabled:", bool(action))
            tell(menu, "addItem:", item)
            return item

        rule = lambda: tell(menu, "addItem:", msg("NSMenuItem", "separatorItem"))
        # The counts, which show() words. The first two open the window on the view they count.
        self.lines = [add("", "ready"), add("", "attention"), add("")]
        rule()
        add("Poll now", "poll")
        self.switch = add("Auto-merge", "merging")
        rule()
        add("Open prherd", "open")
        if settings:
            add("Settings…", "settings")
        rule()
        add("Quit prherd", "quit")
        tell(self.item, "setMenu:", menu)

    def show(self, ready, attention, merged, merging):
        """Say the counts, and whether Auto-merge is on. It's called often, and Cocoa is told only of a change."""
        if (ready, attention, merged, merging) != self.shown:
            self.pooled(self.say, ready, attention, merged, merging)
            self.shown = ready, attention, merged, merging

    def say(self, ready, attention, merged, merging):
        title, tip, lines = self.words(ready, attention, merged)
        self.tell(self.button, "setTitle:", self.text(f" {title}" if title else ""))
        self.tell(self.button, "setToolTip:", self.text(tip))
        for line, words in zip(self.lines, lines):
            self.tell(line, "setTitle:", self.text(words))
        self.tell(self.switch, "setState:", int(merging))  # a tick beside it while it's on

    def close(self):
        """Take the item out of the menu bar. Fine to call twice, or on one that was never finished."""
        self.live.pop(self.target, None)
        if self.item:
            self.tell(self.msg("NSStatusBar", "systemStatusBar"), "removeStatusItem:", self.item)
        for obj in self.held:
            self.tell(obj, "release")
        self.item, self.held = None, []


class App:
    """The window: a toolbar and banners over a sidebar, the PR table and the activity log.

    Worker threads never touch Tk. They post to self.events, which drain() empties on the Tk thread.
    """

    # Right-click is Button-3, except on macOS Tk 8.6, which calls it Button-2. Control-click is a right-click only on
    # macOS: on Windows and Linux it adds a row to the selection.
    MENU_GESTURES = ("<Button-2>", "<Button-3>", *(("<Control-Button-1>",) if sys.platform == "darwin" else ()))

    def __init__(self, targets, mode, interval):
        global tk, ttk, messagebox  # imported here, not at the top: --headless runs where Tk isn't installed
        import tkinter as tk
        from tkinter import messagebox, ttk

        saved = load_settings()
        # A CLI flag wins for this run only: until its control is changed, the saved value is what's saved again.
        # Else one `--admin` would make every later launch bypass branch protection.
        self.flagged = {key: saved[key] for key, flag in (("mode", mode), ("interval", interval)) if flag is not None}
        mode = saved["mode"] if mode is None else mode
        self.interval = interval or saved["interval"]  # the seconds between polls, as last read from the spinbox
        self.root = root = tk.Tk()
        self.x11 = root.tk.call("tk", "windowingsystem") == "x11"
        root.app = self  # the way in for anything that's handed only the window
        root.title("prherd")
        if os.path.exists(ICON):  # run from source, Tk would show Python's icon in the Dock
            root.iconphoto(True, tk.PhotoImage(file=ICON))
        root.geometry(saved["geometry"] or "1180x720")
        root.minsize(780, 480)
        self.events = queue.Queue()  # worker threads never touch tk; they post here
        self.repos = []
        self.rows = {}  # repo -> [(pr, verdict)] from its last poll that could list them
        self.failed = set()  # the repos whose latest poll couldn't
        self.prs = {}  # tree row -> its PR as of the last poll
        self.verdicts = {}  # tree row -> its verdict as poll() returned it; the cell shows a label, so guards read this
        self.details = {}  # repo -> {PR number: its verdict_detail()} from that poll, for the detail panel's timeline
        self.sheet = None  # the Merge now sheet, while it's open
        self.appearance = saved["appearance"]
        # What notice() compares each poll with. Workers keep these, one at a time: whoever holds self.noticing.
        self.seen, self.streaks, self.newest, self.noticing = {}, {}, {}, threading.Lock()
        self.merged = ("", {})  # see merged_today()
        self.menubar, self.status = saved["menubar"], None  # the setting, and the menu bar item while there is one
        self.dark = None  # the palette in use; None until the first theme()
        self.busy = False
        self.timer = None  # the tick that's waiting
        self.due = time.time()  # when it fires
        self.view, self.only = saved["view"], None  # the sidebar's view, and the one repo picked there, if any
        self.adding = False  # whether + has opened the sidebar's add-a-repo entry
        self.notify = saved["notify"]  # NOTIFY_KINDS kind -> whether to notify of it; the settings window's switches
        self.rights = {}  # repo -> whether gh's account can merge there, as far as the settings window has asked
        self.prefs = None  # the settings window, while it's open
        self.followers = []  # see tell()
        self.log_open = saved["log"]
        self.images = {}  # see image()
        self.painters = []  # see recolor()
        self.mode_var = tk.StringVar(value=mode)
        self.interval_var = tk.IntVar(value=self.interval)
        self.merging = tk.BooleanVar(value=saved["merging"])
        self.query = tk.StringVar()
        # Mirrors the controls for worker threads, which can't read tk vars. Read at each merge,
        # so removing a repo or unticking Auto-merge stops a poll that's already running.
        self.live = {"mode": mode, "merging": saved["merging"]}

        self.build_toolbar()
        self.build_banners()
        # Side by side: the sidebar, the main column and whatever is added to their right.
        self.body = ttk.PanedWindow(root, orient="horizontal")
        self.body.pack(fill="both", expand=True)
        self.build_sidebar(saved["sidebar"])
        self.main = ttk.Frame(self.body)
        self.body.add(self.main, weight=1)
        self.build_log()  # packed first, so it keeps the foot of the column however short the window
        self.build_heading()
        self.build_selection_bar()
        self.content = ttk.Frame(self.main)  # holds the table; swap() shows another screen in its place
        self.content.pack(fill="both", expand=True)
        self.build_table()
        self.detail = Detail(self, saved["detail"])
        self.build_menus()
        self.query.trace_add("write", lambda *_: self.render())
        root.bind("<Command-k>" if sys.platform == "darwin" else "<Control-k>", lambda _: self.search.focus_set())

        for repo in saved["repos"]:
            self.bg(self.show, repo)  # already resolved; re-resolving offline would drop it from the next save
        for target in targets:
            self.bg(self.add_repo, target)
        self.bg(lambda: self.events.put(("account", whoami())))
        self.first_run = FirstRun(self, waiting=bool(saved["repos"] or targets))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.set_menubar(self.menubar)
        self.retheme()
        self.show_state()
        self.render()
        self.drain()
        self.schedule()
        self.clock()

    def run(self):
        self.root.mainloop()

    # Building the window, top to bottom.

    def build_toolbar(self):
        bar = ttk.Frame(self.root, style="Bar.TFrame", padding=(16, 10))
        bar.pack(fill="x")
        self.rule = ttk.Frame(self.root, style="Line.TFrame", height=1)  # show_state() packs the banners under this
        self.rule.pack(fill="x")
        bar.columnconfigure(0, weight=3)  # spare width goes to the search field, and to a gap after it
        bar.columnconfigure(1, weight=2)

        field = ttk.Frame(bar, style="Field.TFrame", padding=(9, 2, 4, 2))
        field.grid(row=0, column=0, sticky="ew")
        ttk.Label(field, image=self.icon("search", "muted", 14), style="Field.TLabel").pack(side="left", padx=(0, 4))
        self.search = ttk.Entry(field, textvariable=self.query, style="Search.TEntry", width=12)
        self.search.pack(side="left", fill="x", expand=True)
        self.hint(self.search, self.query, "Search pull requests, branches, authors")
        self.search.bind("<FocusIn>", lambda _: field.state(["focus"]))
        self.search.bind("<FocusOut>", lambda _: field.state(["!focus"]))
        self.search.bind("<Escape>", lambda _: self.query.set(""))

        modes = ttk.Frame(bar, style="Seg.TFrame", padding=2)
        modes.grid(row=0, column=2, padx=(0, 14))
        for label, value in MODES:
            ttk.Radiobutton(modes, text=label, variable=self.mode_var, value=value, style="Toolbutton",
                            command=lambda: self.control(mode=self.mode_var.get())).pack(side="left")

        every = ttk.Frame(bar, style="Bar.TFrame")
        every.grid(row=0, column=3, padx=(0, 14))
        ttk.Label(every, text="Every", style="Bar.TLabel").pack(side="left", padx=(0, 6))
        spin = ttk.Spinbox(every, from_=1, to=MAX_INTERVAL, width=5, textvariable=self.interval_var, command=self.retime)
        spin.pack(side="left")
        for event in ("<Return>", "<FocusOut>"):  # typed in, not clicked up or down
            spin.bind(event, self.retime)
        ttk.Label(every, text="s", style="Bar.TLabel").pack(side="left", padx=(4, 0))

        ttk.Checkbutton(bar, text="Auto-merge", variable=self.merging, style="Switch.TCheckbutton", compound="left",
                        image=(self.switch(False), "selected", self.switch(True)),
                        command=lambda: self.control(merging=self.merging.get())).grid(row=0, column=4, padx=(0, 14))
        # A fixed width: the countdown in its label changes every second, and mustn't nudge the toolbar.
        self.poll_button = ttk.Button(bar, text="Poll now", style="Primary.TButton", width=15, command=self.poll_now)
        self.poll_button.grid(row=0, column=5)

    def build_banners(self):
        def banner(style, icon, lead, text, action=None):
            frame = ttk.Frame(self.root, style=f"{style}.TFrame")
            ttk.Frame(frame, style=f"{style}Line.TFrame", height=1).pack(side="bottom", fill="x")
            row = ttk.Frame(frame, style=f"{style}.TFrame", padding=(16, 6))
            row.pack(fill="x")
            if action:  # packed before the text, so a narrow window cuts the text short and not the button
                ttk.Button(row, text=action[0], style="Link.TButton", command=action[1]).pack(side="right")
            ttk.Label(row, image=icon, style=f"{style}.TLabel").pack(side="left", padx=(0, 10))
            bold = ttk.Label(row, text=lead, style=f"Bold.{style}.TLabel")
            bold.pack(side="left")
            rest = ttk.Label(row, text=text, style=f"{style}.TLabel")
            rest.pack(side="left", fill="x", expand=True)

            def fit(_event):  # wrap rather than run under the button when the window is narrow
                width = max(rest.winfo_width(), 80)
                if rest.cget("wraplength") != width:  # only on a change: setting it resizes the row, and calls this
                    rest.configure(wraplength=width)
                wrapped = rest.winfo_reqheight() > bold.winfo_reqheight() + 4
                bold.pack_configure(anchor="n" if wrapped else "center")  # level with the first line

            rest.bind("<Configure>", fit)
            return frame

        def resume():
            self.merging.set(True)
            self.control(merging=True)

        self.admin_banner = banner("Admin", self.icon("shield", "admin-fg", 14), "Admin mode.",
                                   " Merges bypass branch protection on every watched repo.")
        self.paused_banner = banner("Paused", self.icon("pause", "text-3", 14), "Watching only.",
                                    " prherd keeps polling and still flags conflicts, but won't merge until"
                                    " Auto-merge is back on.", ("Resume merging", resume))

    def build_sidebar(self, width):
        self.sidebar_width = width  # as last seen; save() reads the real one once the window is drawn
        side = self.sidebar = ttk.Frame(self.body, style="Side.TFrame", width=width)
        side.pack_propagate(False)  # the saved width, not whatever its rows ask for
        self.body.add(side, weight=0)
        ttk.Frame(side, style="Line.TFrame", width=1).pack(side="right", fill="y")

        # The foot: the way into the settings, and who gh is signed in as once it has said (see show_account()).
        foot = ttk.Frame(side, style="Side.TFrame")
        foot.pack(side="bottom", fill="x")
        ttk.Frame(foot, style="Line.TFrame", height=1).pack(fill="x", padx=10)
        row = ttk.Frame(foot, style="Side.TFrame", padding=(18, 10, 10, 10))
        row.pack(fill="x")
        ttk.Button(row, image=self.icon("sliders", "text-3", 15), style="Side.TButton", takefocus=False,
                   command=self.settings).pack(side="right")
        self.root.bind("<Command-comma>" if sys.platform == "darwin" else "<Control-comma>", self.settings)
        if self.root.tk.call("tk", "windowingsystem") == "aqua":  # the app menu's Settings… item
            self.root.createcommand("::tk::mac::ShowPreferences", self.settings)
        who = self.account = ttk.Frame(row, style="Side.TFrame")
        self.avatar = ttk.Label(who, image=self.dot("avatar", 28), compound="center", style="Avatar.Side.TLabel")
        self.avatar.pack(side="left", padx=(0, 10))
        self.login = ttk.Label(who, style="Bold.Side.TLabel")
        self.login.pack(anchor="w")
        ttk.Label(who, text="signed in to gh", image=self.dot("ok-dot", 6), compound="left",
                  style="Muted.Side.TLabel").pack(anchor="w")

        # The views and repos scroll as one: there can be more repos than the window is tall.
        canvas = self.side_canvas = tk.Canvas(side, highlightthickness=0, borderwidth=0, yscrollincrement=16)
        canvas.pack(fill="both", expand=True)
        inner = ttk.Frame(canvas, style="Side.TFrame", padding=(8, 14, 8, 8))
        held = canvas.create_window(0, 0, anchor="nw", window=inner)
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(held, width=event.width))
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=(0, 0, event.width, event.height)))

        def wheel(event):  # bound to every widget, so act only with the pointer over the sidebar
            try:
                over = str(self.root.winfo_containing(event.x_root, event.y_root) or "")
            except KeyError:  # a window Tkinter didn't make, like a menu
                return
            if over.startswith(str(canvas)) and inner.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-1 if event.num == 4 or event.delta > 0 else 1, "units")

        x11 = self.root.tk.call("tk", "windowingsystem") == "x11"  # which sends the wheel as buttons 4 and 5
        for gesture in ("<Button-4>", "<Button-5>") if x11 else ("<MouseWheel>",):
            self.root.bind_all(gesture, wheel, add="+")

        ttk.Label(inner, text="VIEWS", style="Caps.Side.TLabel").pack(anchor="w", padx=8, pady=(0, 6))
        self.view_rows = {key: self.side_row(inner, self.icon(view["icon"], view["color"]), view["label"],
                                             click=lambda key=key: self.pick_view(key))
                          for key, view in VIEWS.items()}
        head = self.repo_head = ttk.Frame(inner, style="Side.TFrame")
        head.pack(fill="x", pady=(18, 4))
        ttk.Label(head, text="REPOSITORIES", style="Caps.Side.TLabel").pack(side="left", padx=8)
        ttk.Button(head, image=self.icon("plus", "text-3", 14), style="Side.TButton", takefocus=False,
                   command=self.toggle_add).pack(side="right")
        self.new_repo = tk.StringVar()
        self.add_entry = ttk.Entry(inner, textvariable=self.new_repo)  # render() shows it: after +, or with no repos
        self.hint(self.add_entry, self.new_repo, "owner/repo or a path")
        self.add_entry.bind("<Return>", self.add)
        self.add_entry.bind("<Escape>", lambda _: self.toggle_add(False))
        self.repo_list = ttk.Frame(inner, style="Side.TFrame")
        self.repo_list.pack(fill="x")
        self.repo_rows = {}  # repo -> its row; render() keeps them in step with self.repos

    def side_row(self, parent, image, text, sub=None, click=None, menu=None):
        """A sidebar row: an icon, a name (over a smaller line, given sub) and a count. Its style lights it while
        the pointer is over it, and while it's the one chosen (see lit())."""
        row = ttk.Frame(parent, style="Row.Side.TFrame", padding=(8, 6 if sub is None else 4))
        row.pack(fill="x", pady=1)
        row.columnconfigure(1, weight=1)
        row.icon = ttk.Label(row, image=image, style="Row.Side.TLabel")
        row.icon.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        ttk.Label(row, text=text, style="Name.Row.Side.TLabel").grid(row=0, column=1, sticky="ew")
        if sub is not None:
            ttk.Label(row, text=sub, style="Sub.Row.Side.TLabel").grid(row=1, column=1, sticky="ew")
        row.count = ttk.Label(row, style="Count.Row.Side.TLabel")
        row.count.grid(row=0, column=2, rowspan=2, padx=(8, 2))
        row.parts = (row, *row.winfo_children())

        def hover(_event):  # leaving the row for one of its own labels isn't leaving it, so go by where the pointer is
            x, y = row.winfo_pointerx() - row.winfo_rootx(), row.winfo_pointery() - row.winfo_rooty()
            over = 0 <= x < row.winfo_width() and 0 <= y < row.winfo_height()
            for part in row.parts:
                part.state(["active" if over else "!active"])

        for part in row.parts:
            part.bind("<Enter>", hover)
            part.bind("<Leave>", hover)
            part.bind("<Button-1>", lambda _: click())
            for gesture in self.MENU_GESTURES if menu else ():
                part.bind(gesture, menu)
        return row

    @staticmethod
    def lit(row, on):
        """Mark a sidebar row as the chosen one, or not."""
        for part in row.parts:
            part.state(["selected" if on else "!selected"])

    def build_heading(self):
        head = self.heading = ttk.Frame(self.main, padding=(24, 16, 24, 14))
        head.pack(fill="x")
        ttk.Frame(self.main, style="Rule.TFrame", height=1).pack(fill="x")
        # The stat chips, on the right. Packed before the text, so they keep their width; fit_stats() leaves out
        # those that would crowd it.
        self.stats = ttk.Frame(head)
        self.chips, self.fitted = {}, None
        for key, label, color in (("ready", "Ready", "ok-dot"), ("reviewing", "Reviewing", "warn-dot"),
                                  ("attention", "Attention", "bad-dot"), ("merged", "Merged today", "acc-dot")):
            chip = ttk.Frame(self.stats, style="Stat.TFrame", padding=1)
            box = ttk.Frame(chip, padding=(11, 5, 12, 5))
            box.pack(fill="both", expand=True)
            box.columnconfigure(0, minsize=62)
            square = self.image(("stat", color), lambda p, color=color: draw(
                ((p[color], 12, ((6, 6, 18, 6, 18, 18, 6, 18, 6, 6), (6, 12, 18, 12))),), 7, 7))
            ttk.Label(box, text=label, image=square, compound="left", style="Stat.TLabel").grid(sticky="w")
            value = ttk.Label(box, text="0", style="Value.Stat.TLabel")
            value.grid(sticky="w")
            self.chips[key] = chip, value
        head.bind("<Configure>", self.fit_stats)
        self.title = ttk.Label(head, style="Title.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(head, style="Muted.TLabel")
        self.subtitle.pack(anchor="w", pady=(2, 0))

    def build_selection_bar(self):
        """What to do with the selected PRs. show_selection() lays it over the heading while two or more are
        selected: there, the rows under the pointer stay where they are."""
        bar = self.selection_bar = ttk.Frame(self.heading, style="Sel.TFrame", padding=(16, 0, 8, 0))
        self.selected = ttk.Label(bar, style="Bold.Sel.TLabel")
        ttk.Button(bar, image=self.icon("x", "inv-fg", 14), style="Flat.Sel.TButton", takefocus=False,
                   command=lambda: self.tree.selection_set(())).pack(side="right")
        self.merge_button = ttk.Button(bar, text="Merge now…", style="Primary.Sel.TButton", command=self.merge_now)
        # Right to left, as they're packed. Don't merge and Allow merge are each shown only when they'd do something.
        self.bar_buttons = [
            self.merge_button,
            ttk.Button(bar, text="Update branch", style="Sel.TButton", command=self.update_branches),
            ttk.Button(bar, text="Allow merge", style="Sel.TButton", command=lambda: self.set_dont_merge(False)),
            ttk.Button(bar, text="Don't merge", style="Sel.TButton", command=lambda: self.set_dont_merge(True)),
            ttk.Button(bar, text="Request Codex review", style="Sel.TButton", command=self.ask_codex),
        ]

    def build_table(self):
        table = self.table = ttk.Frame(self.content)
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        tree = self.tree = ttk.Treeview(table, columns=("codex", "author", "state", "head", "age"), height=8)
        # As wide as what each holds, measured: the system font is a good deal wider on Linux than on macOS.
        measure = fonts(self.root)["bold"].measure
        widths = {column: measure(widest) + 26 for column, widest in (
            ("#0", "#000 a title of about this length"), ("codex", "No major issues"), ("author", "dependabot[bot]"),
            ("state", "Queued"), ("head", "0000000"), ("age", "00m"))}
        for column, text in (("#0", "Pull request"), ("codex", "Codex"), ("author", "Author"), ("state", "State"),
                             ("head", "Head"), ("age", "Age")):
            anchor = "e" if column == "age" else "w"
            tree.heading(column, text=text.upper(), anchor=anchor)
            tree.column(column, width=widths[column], minwidth=widths[column], anchor=anchor, stretch=column == "#0")
        tree.grid(row=0, column=0, sticky="nsew")
        self.scroll(tree)

        def fit(event):
            """Treeview hands spare width to the PR column, but with too little it lets the last columns run off
            the edge. Leave out Head, then Age, while they won't fit beside a readable PR column: then, squeezed
            further by the detail panel, Author, State and Codex, whose verdict is still the row's icon."""
            shown = list(tree["columns"])
            for column in ("head", "age", "author", "state", "codex"):
                if sum(widths[c] for c in ("#0", *shown)) > event.width:
                    shown.remove(column)
            if shown != list(tree["displaycolumns"]):
                tree.configure(displaycolumns=shown)
            tree.column("#0", width=max(widths["#0"], event.width - sum(widths[c] for c in shown)))

        tree.bind("<Configure>", fit)
        # Said in place of the rows when there are none to show.
        self.empty = ttk.Frame(table)
        ttk.Label(self.empty, image=self.icon("check", "faint", 28)).pack(pady=(0, 6))
        self.empty_title = ttk.Label(self.empty, style="Bold.TLabel")
        self.empty_title.pack()
        self.empty_text = ttk.Label(self.empty, style="Muted.TLabel")
        self.empty_text.pack(pady=(2, 0))

        tree.bind("<<TreeviewSelect>>", self.show_selection)
        # A repo row keeps double-click for expanding and collapsing.
        tree.bind("<Double-Button-1>",
                  lambda event: self.open_in_browser(tree.selection()) if "#" in tree.identify_row(event.y) else None)
        for gesture in self.MENU_GESTURES:
            tree.bind(gesture, self.context_menu)

    def build_log(self):
        box = ttk.Frame(self.main, style="Bar.TFrame")
        box.pack(side="bottom", fill="x")
        ttk.Frame(box, style="Line.TFrame", height=1).pack(fill="x")
        head = ttk.Frame(box, style="Bar.TFrame", padding=(10, 5, 16, 5))
        head.pack(fill="x")
        self.disclosure = ttk.Button(head, text="Activity", compound="left", style="Bar.TButton",
                                     command=self.toggle_log)
        self.disclosure.pack(side="left")
        self.badge = ttk.Label(head, compound="left")  # Live, or Watching with Auto-merge off: see show_state()
        self.badge.pack(side="left", padx=6)
        self.next_poll = ttk.Label(head, style="Muted.Bar.TLabel")
        self.next_poll.pack(side="right")
        # Six lines tall, in pixels: a Text sizes itself in lines of its font, not counting the space between them.
        line = fonts(self.root)["bold"].metrics("linespace") + 6
        self.log_body = ttk.Frame(box, style="Bar.TFrame", height=6 * line)
        self.log_body.grid_propagate(False)
        self.log_body.columnconfigure(0, weight=1)
        self.log_body.rowconfigure(0, weight=1)
        self.logbox = tk.Text(self.log_body, height=1, state="disabled", wrap="word", font="TkDefaultFont",
                              cursor="arrow")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        self.scroll(self.logbox)
        self.toggle_log(self.log_open)

    def build_menus(self):
        root, tree = self.root, self.tree
        self.pr_menu = menu = tk.Menu(root, tearoff=False)
        menu.add_command(label="Open in browser", command=lambda: self.open_in_browser(tree.selection()))
        menu.add_command(label="Copy link", command=lambda: self.copy_links(tree.selection()))
        menu.add_separator()
        menu.add_command(label="Merge now…", command=self.merge_now)
        menu.add_command(label="Don't merge", command=lambda: self.set_dont_merge(True))
        menu.add_command(label="Allow merge", command=lambda: self.set_dont_merge(False))
        menu.add_separator()
        menu.add_command(label="Update branch", command=self.update_branches)
        menu.add_command(label="Request Codex review", command=self.ask_codex)

        self.menu_repos = []  # the repos the repo menu was opened on: the tree's selected ones, or one sidebar row's
        self.repo_menu = menu = tk.Menu(root, tearoff=False)
        menu.add_command(label="Open in browser", command=lambda: self.open_in_browser(self.menu_repos))
        menu.add_command(label="Copy link", command=lambda: self.copy_links(self.menu_repos))
        menu.add_command(label="Refresh", command=lambda: [self.bg(self.refresh, repo) for repo in self.menu_repos])
        menu.add_separator()
        menu.add_command(label="Remove repo", command=lambda: self.remove(self.menu_repos))

    # Small parts the builders share.

    def hint(self, entry, var, text, x=5):
        """Grey text in an entry while it's empty, saying what goes there. Tk 8.6's entries have no placeholder."""
        label = ttk.Label(entry, text=text, style="Hint.Field.TLabel")
        label.bind("<Button-1>", lambda _: entry.focus_set())

        def show(*_):
            label.place_forget() if var.get() else label.place(x=x, rely=0.5, anchor="w")

        var.trace_add("write", show)
        show()

    def scroll(self, widget):
        """Give widget, gridded at row 0, column 0, a scrollbar beside it that's there only while there's more
        than fits."""
        bar = ttk.Scrollbar(widget.master, orient="vertical", command=widget.yview)

        def span(first, last):
            bar.set(first, last)
            if float(first) > 0 or float(last) < 1:
                bar.grid(row=0, column=1, sticky="ns")
            else:
                bar.grid_remove()

        widget.configure(yscrollcommand=span)

    @property
    def palette(self):
        return PALETTE["dark" if self.dark else "light"]

    def image(self, key, make):
        """A drawn image, kept under key. make(palette) returns its PNG. recolor() asks again and redraws the same
        image in place, so whatever shows it follows the theme without being told."""
        if key not in self.images:
            self.images[key] = tk.PhotoImage(master=self.root, data=base64.b64encode(make(self.palette))), make
        return self.images[key][0]

    def icon(self, name, color, size=16):
        """One of ICONS, in a PALETTE color."""
        return self.image((name, color, size), lambda p: draw(((p[color], 2, ICONS[name]),), size, size))

    def verdict_icon(self, v, size=16):
        look = VERDICTS[v]
        dot = f"{look['tone']}-dot"  # the stronger of a tone's colors, for the tones that have one
        return self.icon(look["icon"], dot if dot in self.palette else f"{look['tone']}-fg", size)

    def dot(self, color, size):
        return self.image(("dot", color, size), lambda p: draw(((p[color], 24, ((12, 12, 12, 12),)),), size, size))

    def switch(self, on):
        """Auto-merge's switch: a pill, in the accent with its knob to the right when on."""
        pill, knob = ("acc-dot", 28) if on else ("knob-off", 12)
        return self.image(("switch", on), lambda p: draw(((p[pill], 24, ((12, 12, 28, 12),)),
                                                          ("#FFFFFF", 18.6, ((knob, 12, knob, 12),))), 30, 18))

    def swap(self, screen=None):
        """Show screen, a child of self.content, where the table is. With none, the table again."""
        for shown in self.content.pack_slaves():
            shown.pack_forget()
        (screen or self.table).pack(fill="both", expand=True)

    # Theming.

    def retheme(self, force=False):
        """Theme the window, again whenever the system appearance has changed since. force it after adding a
        Toplevel, or anything else theme() has to reach."""
        now = self.appearance == "dark" or self.appearance == "system" and system_dark(self.root)
        if now != self.dark or force:
            self.dark = now
            theme(self.root, self.dark, self.appearance)
            self.recolor()

    def recolor(self):
        """Color what ttk.Style can't, in the palette now in use. Runs after every theme().

        That's every drawn image, the table's row tags, the log's text tags and the sidebar's canvas. Anything
        added later that colors itself appends a function taking the palette to self.painters.
        """
        p, f = self.palette, fonts(self.root)
        for image, make in self.images.values():
            image.configure(data=base64.b64encode(make(p)))
        self.side_canvas.configure(background=p["side"])
        # Treeview can't color one cell, so a row that needs attention is tinted whole.
        self.tree.tag_configure("repo", font=f["bold"])
        self.tree.tag_configure("bad", background=p["bad-bg"])
        self.tree.tag_configure("err", background=p["err-bg"])
        box = self.logbox
        box.configure(background=p["bar"], foreground=p["text-2"], highlightthickness=0, padx=16, pady=0)
        box.tag_configure("line", spacing1=3, spacing3=3, lmargin2=f["mono"].measure("0" * 22))
        box.tag_configure("time", font=f["mono"], foreground=p["muted"])
        box.tag_configure("subject", font=f["bold"], foreground=p["text"])
        for kind, tone, _ in (*KINDS, ("info", "none", ())):
            box.tag_configure(kind, font=f["mono"], foreground=p[f"{tone}-fg"], background=p[f"{tone}-bg"])
        for paint in self.painters:
            paint(p)

    # Showing what's known.

    def render(self):
        """Refill the table from self.rows, through the view, the one repo picked in the sidebar (if any) and the
        search. Then the heading and the sidebar, which count what it found."""
        tree, query, now = self.tree, self.query.get(), time.time()
        filtering = self.view != "all" or self.only is not None or bool(query.split())
        selected, top = tree.selection(), tree.yview()[0]
        closed = {repo for repo in tree.get_children() if not tree.item(repo, "open")}
        tree.delete(*tree.get_children())
        self.prs, self.verdicts = {}, {}
        shown = 0
        for repo in self.repos:
            rows = self.rows.get(repo, [])
            for pr, v in rows:
                self.prs[f"{repo}#{pr['number']}"], self.verdicts[f"{repo}#{pr['number']}"] = pr, v
            hits = [(pr, v) for pr, v in rows if matches(pr, v, self.view, query)] if self.only in (None, repo) else []
            if filtering and not hits:
                continue  # a filter lists what it found, not every repo it looked in
            count = (f"{len(hits)} of {len(rows)}" if filtering else f"{len(rows)} open" if repo in self.rows else
                     "couldn't list its pull requests" if repo in self.failed else "")
            tree.insert("", "end", iid=repo, text=f"{short(repo)}  ·  {count}" if count else short(repo),
                        open=repo not in closed, tags=("repo",))
            for pr, v in hits:
                branch = pr.get("headRefName")
                tree.insert(repo, "end", iid=f"{repo}#{pr['number']}", image=self.verdict_icon(v),
                            text=f" #{pr['number']}  {pr['title']}" + (f"  ·  {branch}" if branch else ""),
                            values=row_values(pr, v, now), tags={"merge conflict": ("bad",), "?": ("err",)}.get(v, ()))
            shown += len(hits)
        tree.selection_set([iid for iid in selected if tree.exists(iid)])
        tree.yview_moveto(top)
        self.show_selection()

        groups = len(tree.get_children())
        view = VIEWS[self.view]["label"]  # the heading: the view, the picked repo, or both
        self.title.configure(text=view if not self.only else short(self.only) if self.view == "all" else
                             f"{short(self.only)} · {view}")
        self.subtitle.configure(text=(
            "No repositories yet" if not self.repos else "No open pull requests" if not shown else
            f"{shown} open in {groups} repositor{'y' if groups == 1 else 'ies'}")
            + (f" · matching “{query.strip()}”" if query.split() else ""))
        if groups or not self.repos:  # with no repos, the first run is shown where the table is
            self.empty.place_forget()
        else:
            self.empty_title.configure(text="Nothing here")
            self.empty_text.configure(text="No open pull requests match this view.")
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.show_sidebar()
        self.tell("repos")
        self.show_stats()

    def show_sidebar(self):
        """The sidebar's counts, its repo rows and which of its rows are chosen."""
        scope = [v for repo in self.repos if self.only in (None, repo) for _, v in self.rows.get(repo, [])]
        for key, row in self.view_rows.items():
            listed = VIEWS[key]["verdicts"]
            row.count.configure(text=sum(listed is None or v in listed for v in scope))
            self.lit(row, key == self.view)
        if list(self.repo_rows) != self.repos:  # a repo came or went: make its rows afresh
            for row in self.repo_rows.values():
                row.destroy()
            self.repo_rows = {}
            for repo in self.repos:
                owner, _, name = short(repo).rpartition("/")
                self.repo_rows[repo] = self.side_row(
                    self.repo_list, self.dot("open-dot", 8), name, sub=owner,
                    click=lambda repo=repo: self.pick_repo(repo),
                    menu=lambda event, repo=repo: self.pop_repo_menu([repo], event))
        for repo, row in self.repo_rows.items():
            rows = self.rows.get(repo)
            # Its health: grey until first polled, orange if the last poll failed, red if a PR needs attention.
            health = ("err-dot" if repo in self.failed else "open-dot" if rows is None else
                      "bad-dot" if any(v in VIEWS["attention"]["verdicts"] for _, v in rows) else "ok-dot")
            row.icon.configure(image=self.dot(health, 8))
            row.count.configure(text="" if rows is None else len(rows))
            self.lit(row, repo == self.only)
        if self.adding or not self.repos:
            self.add_entry.pack(fill="x", padx=8, pady=(0, 6), after=self.repo_head)
        else:
            self.add_entry.pack_forget()

    def show_selection(self, _event=None):
        """Lay the selection bar over the heading while two or more PRs are selected, with the actions that apply.
        One PR has the detail panel."""
        self.detail.show()
        chosen = self.selected_prs()
        if len(chosen) < 2:
            return self.selection_bar.place_forget()
        marked = [self.verdicts.get(iid) == "don't merge" for iid in chosen]
        offered = {"Don't merge": not all(marked), "Allow merge": any(marked)}
        self.selected.pack_forget()
        for button in self.bar_buttons:
            button.pack_forget()
            if offered.get(button.cget("text"), True):
                button.pack(side="right", padx=(4, 0), pady=8)
        self.selected.configure(text=f"{len(chosen)} selected")
        self.selected.pack(side="left")
        self.merge_button.state(["!disabled" if self.mergeable_now() else "disabled"])
        # place() measures from inside the heading's padding, so this reaches a little way back out into it
        self.selection_bar.place(x=-8, y=-6, relwidth=1, width=16, relheight=1, height=10)

    def show_state(self):
        """The banners that apply under the toolbar, and the log's badge."""
        after = self.rule
        for banner, on in ((self.admin_banner, self.live["mode"] == "admin"),
                           (self.paused_banner, not self.live["merging"])):
            banner.pack_forget()
            if on:
                banner.pack(fill="x", after=after)
                after = banner
        merging = self.live["merging"]
        self.badge.configure(text="Live" if merging else "Watching", style="Live.TLabel" if merging else "Idle.TLabel",
                             image=self.dot("ok-dot" if merging else "open-dot", 6))
        self.show_stats()  # the menu bar item says whether Auto-merge is on

    @staticmethod
    def today():
        return time.strftime("%F")

    def show_stats(self):
        """The heading's stat chips, counting what the sidebar's views count, and the menu bar item, which counts
        every repo's."""
        self.merged = merged_today(self.merged, self.today())  # so the count starts over at midnight
        counts = lambda key, vs: sum(v in VIEWS[key]["verdicts"] for v in vs)
        everywhere = [v for repo in self.repos for _, v in self.rows.get(repo, [])]
        here = everywhere if self.only is None else [v for _, v in self.rows.get(self.only, [])]
        for key, (_, value) in self.chips.items():
            value.configure(text=counts(key, here) if key != "merged" else sum(
                count for repo, count in self.merged[1].items() if self.only in (None, repo)))
        self.fit_stats()
        self.status_do("show", counts("ready", everywhere), counts("attention", everywhere),
                       sum(self.merged[1].values()), self.live["merging"])

    def fit_stats(self, _event=None):
        """Show the stat chips there's room for beside the heading's text. Merged today is the last to go: it's the
        one the sidebar doesn't also say."""
        gap = 8
        room = self.heading.winfo_width() - 48 - 2 * gap - max(self.title.winfo_reqwidth(),
                                                                self.subtitle.winfo_reqwidth())
        shown = list(self.chips)
        for key in ("reviewing", "attention", "ready", "merged"):
            if sum(self.chips[chip][0].winfo_reqwidth() + gap for chip in shown) > room:
                shown.remove(key)
        if shown != self.fitted:
            self.fitted = shown
            for chip, _ in self.chips.values():
                chip.pack_forget()
            for key in shown:
                self.chips[key][0].pack(side="left", padx=(gap, 0))
            self.stats.pack_forget()  # a frame left with nothing packed in it keeps its last size
            if shown:
                self.stats.pack(side="right", before=self.title)

    def show_log(self, at, msg):
        box, kind = self.logbox, kind_of(msg)
        subject, text = split_log(msg)
        if self.x11:  # Tk on X11 can't draw emoji, and shows a box where poll()'s 👍 and 👀 would be
            text = text.replace("👍", "thumbs-up").replace("👀", "eyes")
        follow = box.yview()[0] <= 0  # keep the newest in view, unless the reader has scrolled down to older ones
        box.configure(state="normal")
        box.insert("1.0", f"{at}  ", ("line", "time"), f" {kind} ", ("line", kind),  # the newest goes on top
                   " " * (10 - len(kind)), ("line", "time"), f"{subject} " if subject else "", ("line", "subject"),
                   f"{text}\n", "line")
        if int(box.index("end-1c").split(".")[0]) > 5000:
            box.delete("4001.0", "end")
        if follow:
            box.yview_moveto(0)
        box.configure(state="disabled")

    def show_account(self, login):
        if login:
            self.login.configure(text=login)
            self.avatar.configure(text=login[:2].upper())
            self.account.pack(side="left")

    # The sidebar's and the toolbar's controls.

    def pick_view(self, key):
        self.view = key
        self.render()
        self.save()

    def pick_repo(self, repo):
        self.only = None if self.only == repo else repo  # a second click shows every repo again
        self.render()

    def toggle_add(self, on=None):
        self.adding = not self.adding if on is None else on
        self.render()
        if self.adding:
            self.add_entry.focus_set()

    def toggle_log(self, on=None):
        self.log_open = not self.log_open if on is None else on
        self.disclosure.configure(image=self.icon("down" if self.log_open else "right", "text", 12))
        if self.log_open:
            self.log_body.pack(fill="x", pady=(0, 10))
        else:
            self.log_body.pack_forget()
        if on is None:
            self.save()

    def settings(self, _event=None):
        """Open the settings window, or bring it forward if it's open already."""
        if not self.prefs:
            self.prefs = Settings(self)
        self.prefs.top.deiconify()
        self.prefs.top.lift()
        self.prefs.top.focus_set()

    def control(self, **change):
        """A merge control changed: tell the workers, show what it means, save it."""
        self.live.update(change)
        if "mode" in change:  # chosen now, so it's what's saved
            self.flagged.pop("mode", None)
        self.show_state()
        self.detail.show()  # its status card says what Auto-merge does next
        self.save()

    def seconds(self):
        try:  # IntVar.get() turns "1e400" into OverflowError and "inf" into ValueError, not TclError
            return min(max(1, int(self.interval_var.get())), MAX_INTERVAL)
        except (tk.TclError, ValueError, OverflowError):  # the spinbox holds what isn't a number
            return self.interval

    def retime(self, _event=None):
        """The interval changed: count down from it afresh. Not on every key, or typing 120 would poll after 1."""
        if self.seconds() != self.interval:
            self.flagged.pop("interval", None)
            self.schedule()
            self.save()

    # Polling.

    def log(self, msg):
        self.events.put(("log", time.strftime("%T"), msg))

    def bg(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:  # e.g. gh gone: say so, rather than the thread dying quietly
                self.log(f"failed: {e!r}")

        threading.Thread(target=run, daemon=True).start()

    def add_repo(self, target):
        try:
            repo = resolve(target)
        except Exception as e:
            self.events.put(("tell", "unadded", target, e))
            return self.log(f"can't add {target}: {e!r}")
        self.show(repo)

    def refresh(self, repo):
        details, started = {}, time.monotonic()
        try:  # show it now; merge on the next tick
            rows = poll(repo, self.log, lambda: None, details)
            self.events.put(("prs", repo, rows, details))
            self.notice(repo, started, rows)
        except Exception as e:
            self.log(f"{repo}: poll crashed; retrying next interval ({e!r})")

    def show(self, repo):
        self.events.put(("repo", repo))
        with self.noticing:  # newly watched, or watched again: what its PRs did while it wasn't is no news
            self.seen.pop(repo, None)
        self.refresh(repo)

    def notice(self, repo, started, rows):
        """Post the notifications a poll of repo is worth, and count its merges. The poll began at started, by
        time.monotonic(). This runs gh (notices() asks about each PR that left the list), so on a worker thread."""
        with self.noticing:  # a tick's poll and a refresh can have the same repo at once
            # A list older than the one last compared would bring back a PR that merged in between, to be
            # announced again when it next went.
            if started < self.newest.get(repo, 0):
                return
            found = notices(repo, self.seen.get(repo), rows, self.streaks, state=pr_state)
            if rows is not None:  # a listing that failed is no list to compare the next one with
                self.newest[repo] = started
                self.seen[repo] = {pr["number"]: (pr, v) for pr, v in rows}
        for kind, title, body in found:
            if kind == "merged":  # counted whether or not it's announced
                self.events.put(("merged", repo))
            if self.notify.get(kind) and repo in self.repos:
                how = "notified" if notify(title, body) else "can't notify"
                self.log(f"{repo}: {how}: {title} · {body.partition(' · ')[2]}")

    def poll_all(self, snapshot):
        try:
            for repo in snapshot:
                mode = lambda repo=repo: self.live["mode"] if self.live["merging"] and repo in self.repos else None
                details, started = {}, time.monotonic()
                rows = poll(repo, self.log, mode, details)
                self.events.put(("prs", repo, rows, details))
                self.notice(repo, started, rows)
        except Exception as e:  # e.g. gh missing; log it rather than kill the thread silently
            self.log(f"poll crashed; retrying next interval ({e!r})")
        finally:
            self.events.put(("done",))  # else busy stays set and polling stops for good

    def poll(self):
        if self.repos and not self.busy:
            self.busy = True
            self.bg(self.poll_all, list(self.repos))

    def poll_now(self):
        self.poll()
        self.schedule()  # the next one is a whole interval from now

    def schedule(self):
        """Start the wait for the next tick over, from the interval in the spinbox."""
        if self.timer:
            self.root.after_cancel(self.timer)
        self.interval = self.seconds()
        self.due = time.time() + self.interval
        self.timer = self.root.after(self.interval * 1000, self.tick)
        self.show_countdown()

    def tick(self):
        self.retheme()
        self.poll()
        self.schedule()

    def show_countdown(self):
        """How long until the next poll, on Poll now and in the log's header."""
        left = max(0, round(self.due - time.time()))
        left = f"{left}s" if left < 120 else f"{-(-left // 60)}m"
        self.poll_button.configure(text=f"Poll now · {left}")
        self.next_poll.configure(text="Polling…" if self.busy else f"Next poll in {left}")

    def clock(self):
        self.show_countdown()
        self.root.after(1000, self.clock)

    def drain(self):
        changed = False
        while not self.events.empty():
            kind, *rest = self.events.get()
            if kind == "log":
                self.show_log(*rest)
            elif kind == "repo" and rest[0] not in self.repos:
                self.repos.append(rest[0])
                self.save()
                changed = True
            elif kind == "prs" and rest[0] in self.repos:
                repo, rows, details = rest
                if rows is None:  # couldn't list them: keep showing the last list that could
                    self.failed.add(repo)
                else:
                    self.failed.discard(repo)
                    self.rows[repo], self.details[repo] = rows, details
                changed = True
            elif kind == "done":
                self.busy = False
            elif kind == "account":
                self.show_account(rest[0])
            elif kind == "tell":
                self.tell(*rest)
            elif kind == "merged":
                self.merged = merged_today(self.merged, self.today(), rest[0])
                self.show_stats()
            elif kind == "menu":
                self.menu_chose(rest[0])
        if changed:
            self.render()
        self.root.after(200, self.drain)

    def tell(self, what, *args):
        """Pass news on to whatever else shows it: the first run and the settings window. render() tells them
        "repos" when the list or its counts may have changed, and a worker thread posts ("tell", what, ...) to
        self.events to reach them from off the Tk thread. Each follower is a function appended to self.followers."""
        for follow in list(self.followers):
            follow(what, *args)

    # What the buttons and menus do.

    def selected_prs(self):
        return [iid for iid in self.tree.selection() if "#" in iid]

    def add(self, _event=None):
        target = self.new_repo.get().strip()
        if target:
            self.new_repo.set("")
            self.bg(self.add_repo, target)

    def remove(self, repos):
        for repo in repos:
            if repo in self.repos:
                self.repos.remove(repo)
                self.rows.pop(repo, None)
                self.details.pop(repo, None)
                self.failed.discard(repo)
        if self.only not in self.repos:
            self.only = None
        self.render()
        self.save()

    def act_on(self, iids, action):
        """action(repo, number) for each PR row, off the Tk thread; then update their repos."""
        def run():
            for iid in iids:
                repo, number = iid.rsplit("#", 1)
                self.log(f"{iid}: {action(repo, number)}")
            for repo in {iid.rsplit("#", 1)[0] for iid in iids}:
                self.refresh(repo)  # also cancels a queued auto-merge without waiting for a tick

        if iids:
            self.bg(run)

    def ask_codex(self):
        for iid in self.selected_prs():
            repo, number = iid.rsplit("#", 1)
            self.bg(lambda repo=repo, number=number: self.log(f"{repo}#{number}: {request_review(repo, number)}"))

    def set_dont_merge(self, dont):
        self.act_on(self.selected_prs(), lambda repo, number: mark(repo, number, dont))

    def update_branches(self):
        self.act_on(self.selected_prs(), update_branch)

    def mergeable_now(self):
        return [iid for iid in self.selected_prs() if self.verdicts.get(iid) not in ("don't merge", "merge conflict")]

    def merge_now(self):
        # The PRs as shown when asked, so a push after that makes GitHub refuse (--match-head-commit).
        chosen = {iid: self.prs[iid] for iid in self.mergeable_now()}
        mode = "admin" if self.live["mode"] == "admin" else "force"
        if chosen:  # the sheet asks; nothing merges unless its Merge button is chosen
            MergeSheet(self, chosen, mode, lambda: self.act_on(
                list(chosen), lambda repo, number: merge(repo, chosen[f"{repo}#{number}"], mode)))

    def open_in_browser(self, iids):
        for iid in iids:
            repo, _, number = iid.partition("#")
            self.bg(lambda iid=iid, repo=repo, number=number: self.log(f"{iid}: {browse(repo, number)}"))

    def copy_links(self, iids):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(f"https://{iid.replace('#', '/pull/')}" for iid in iids))

    def context_menu(self, event):
        tree = self.tree
        row = tree.identify_row(event.y)
        if not row:
            return None
        if row not in tree.selection():
            tree.selection_set(row)
        if "#" not in row:
            return self.pop_repo_menu([iid for iid in tree.selection() if "#" not in iid], event)
        marked = [self.verdicts.get(iid) == "don't merge" for iid in self.selected_prs()]
        self.pr_menu.entryconfigure("Merge now…", state="normal" if self.mergeable_now() else "disabled")
        self.pr_menu.entryconfigure("Don't merge", state="disabled" if all(marked) else "normal")
        self.pr_menu.entryconfigure("Allow merge", state="normal" if any(marked) else "disabled")
        self.pr_menu.tk_popup(event.x_root, event.y_root)
        return "break"  # else Control-click also toggles the row's selection

    def pop_repo_menu(self, repos, event):
        self.menu_repos = repos
        self.repo_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    # The menu bar item.

    def set_menubar(self, on):
        """Show the menu bar item, or take it away, and keep that as the setting. Only macOS has one. If it can't
        be made there, the window goes without it and the log says why."""
        changed, self.menubar = on != self.menubar, on
        self.status_do("close")
        self.status = None
        if on and sys.platform == "darwin":
            try:  # whatever went wrong, it's an extra: the window isn't
                self.status = MenuBar(lambda action: self.events.put(("menu", action)), settings=True)
            except Exception as e:
                self.log(f"no menu bar item: {e!r}")
        self.show_stats()
        if changed:
            self.save()

    def status_do(self, what, *args):
        """Call the menu bar item, if there is one. If it fails it's taken away, and the window carries on."""
        if self.status:
            try:
                getattr(self.status, what)(*args)
            except Exception as e:
                broken, self.status = self.status, None
                self.log(f"menu bar item failed, so it's off: {e!r}")
                try:  # better gone than left saying counts that are no longer so
                    broken.close()
                except Exception:
                    pass

    def menu_chose(self, action):
        """Do what was chosen from the menu bar item's menu. From drain(): the choice itself came inside Cocoa's
        event handling, where Tk can't be called."""
        if action == "poll":
            self.poll_now()
        elif action == "merging":
            self.merging.set(not self.merging.get())
            self.control(merging=self.merging.get())
        elif action in ("open", "settings", "ready", "attention"):
            if action in VIEWS:  # one of the menu's counts: list what it counted, which was in every repo
                self.only = None
                self.query.set("")
                self.pick_view(action)
            self.root.deiconify()  # wherever it is: minimised, or behind other windows
            self.root.lift()
            self.root.focus_force()  # and behind another app: on macOS this makes prherd the active one
            if action == "settings":
                self.settings()
        elif action == "quit":
            self.root.after_idle(self.close)  # once drain() is done with the window

    def save(self):
        if self.sidebar.winfo_width() > 1:  # it's 1 until the window is drawn
            self.sidebar_width = min(max(self.sidebar.winfo_width(), SIDEBAR[0]), SIDEBAR[1])
        try:
            save_settings({"repos": self.repos, "mode": self.mode_var.get(), "interval": self.seconds(), **self.flagged,
                           "merging": self.merging.get(), "geometry": self.root.geometry(),
                           "detail": self.detail.measure(),
                           "appearance": self.appearance, "view": self.view, "sidebar": self.sidebar_width,
                           "log": self.log_open, "notify": self.notify, "menubar": self.menubar})
        except OSError as e:
            self.log(f"can't save settings to {CONFIG}: {e}")

    def close(self):
        self.save()
        self.status_do("close")
        self.root.destroy()


# What the settings window and the first run ask and draw. The asking is tested without a window.
MERGERS = ("ADMIN", "MAINTAIN", "WRITE")  # the repo permissions GitHub lets merge a pull request


def can_merge(repo):
    """Whether the account gh is signed in as may merge in repo. None if gh can't say."""
    try:
        answer = gh_json("repo", "view", repo, "--json", "viewerPermission")
    except (RuntimeError, ValueError, OSError):
        return None
    permission = answer.get("viewerPermission") if isinstance(answer, dict) else None
    return permission in MERGERS if permission and isinstance(permission, str) else None


def can_notify():
    """Whether notify() has anything to post with here."""
    if sys.platform == "darwin":
        return os.access("/usr/bin/osascript", os.X_OK)
    return bool(shutil.which("notify-send"))


# What the button that shows a file is called: only the first two can point at the file itself.
REVEAL = {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(sys.platform, "Open folder")


def reveal(path):
    """Show path in the file manager, selected where that can be asked for. Returns "" or what went wrong."""
    if sys.platform == "darwin":
        argv = ["/usr/bin/open", "-R", path]
    elif sys.platform == "win32":
        argv = ["explorer", f"/select,{os.path.normpath(path)}"]
    else:
        argv = ["xdg-open", os.path.dirname(path)]
    try:  # spawned as gh() is
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, close_fds=False, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return f"can't show {path}: {e}"
    # Explorer exits 1 even when it opened.
    return f"can't show {path}: {output(r)}" if r.returncode and sys.platform != "win32" else ""


SQUARE = (6, 6, 18, 6, 18, 18, 6, 18, 6, 6)  # stroked 12 wide, a rounded square that fills draw()'s grid
CHECK_BULLETS = {  # a bullet's tone and its mark, drawn as ICONS are: passed, failed, still asking, nothing to check
    True: ("ok", ((7, 12.5, 10.5, 16, 17, 8.5),)),
    False: ("bad", ((8.5, 8.5, 15.5, 15.5), (15.5, 8.5, 8.5, 15.5))),
    None: ("hold", ((7, 12, 7, 12), (12, 12, 12, 12), (17, 12, 17, 12))),
    "": ("hold", ((8, 12, 16, 12),)),
}


def check_bullet(app, ok, size=20):
    """A round bullet for how a check went, from CHECK_BULLETS: the tick in a green disc beside one that passed."""
    tone, mark = CHECK_BULLETS[ok]
    return app.image(("bullet", ok, size), lambda p: draw(((p[f"{tone}-bg"], 24, ((12, 12, 12, 12),)),
                                                           (p[f"{tone}-fg"], 2.6, mark)), size, size))


def tile(app, color, size, icon=None, ink="#FFFFFF"):
    """A rounded square, with one of ICONS small in its middle if given. color and ink are PALETTE names or colors."""
    at = lambda v: 12 + (v - 12) * 0.55
    mark = tuple(("o", at(s[1]), at(s[2]), s[3] * 0.55) if s[0] == "o" else tuple(map(at, s))
                 for s in ICONS.get(icon, ()))
    return app.image(("tile", color, size, icon, ink), lambda p: draw(
        ((p.get(color, color), 12, (SQUARE,)), (p.get(ink, ink), 1.3, mark)), size, size))


class Settings:
    """The settings window, after design/Settings.dc.html: five panes under a row of tabs.

    It keeps nothing of its own. Each control is one of the App's variables or calls one of its methods, and
    acts at once, so there's no OK or Apply and the toolbar and sidebar always agree with it.
    """

    PANES = (("general", "General", "gear"), ("repos", "Repositories", "book"), ("merging", "Merging", "merging"),
             ("codex", "Codex", "sparkle"), ("notify", "Notifications", "bell"))
    WIDTH = 600  # of the column of cards, as in the design
    LOOKS = (("light", "Light"), ("dark", "Dark"), ("system", "Match system"))
    # The appearance previews are always in their own colors, whatever the window's: from the design.
    PREVIEW = {"light": {"pane": "#FFFFFF", "side": "#F6F6F8", "line": "#E4E4E9", "bar": "#DCDCE2", "head": "#C9CAD1",
                         "row": "#F1F1F4"},
               "dark": {"pane": "#16171B", "side": "#131418", "line": "#2A2B31", "bar": "#2E2F37", "head": "#3A3B43",
                        "row": "#202127"}}
    # Each merge mode's flag and what it means, by its value in MODES.
    MODE_HELP = {"": ("the default", "Queues the merge behind required checks. The safest."),
                 "force": ("--force", "Merges right away without auto-merge. Branch protection still applies."),
                 "admin": ("--admin", "Merges with admin rights and bypasses branch protection.")}
    # Each notification's switch, by its kind in NOTIFY_KINDS.
    NOTIFY_HELP = {"merged": ("A pull request merges", "One notification per merge, naming its repo."),
                   "conflict": ("A pull request starts conflicting", "Sent when prherd posts its conflict comment."),
                   "failed": ("A verdict lookup fails", "After two failed polls in a row."),
                   "cleared": ("Codex clears a pull request", "Useful while Auto-merge is off.")}
    TINTS = ("#4F46E5", "#0F766E", "#C2410C", "#7C3AED")  # a repo's tile is one of these, by its name
    REPO_NOTE = "Repos passed on the command line are added to this list. GitHub Enterprise hosts work too."

    def __init__(self, app):
        self.app, root = app, app.root
        top = self.top = tk.Toplevel(root)
        top.withdraw()  # until it's themed and placed
        top.resizable(False, True)
        strip = self.strip = ttk.Frame(top, style="Side.TFrame", padding=(14, 8, 14, 7))
        strip.pack(fill="x")
        ttk.Frame(top, style="Line.TFrame", height=1).pack(fill="x")
        tabs = ttk.Frame(strip, style="Side.TFrame")
        tabs.pack()
        self.pane = tk.StringVar(value="general")
        for key, label, icon in self.PANES:
            ttk.Radiobutton(tabs, text=label, variable=self.pane, value=key, style="Tab.Toolbutton", compound="top",
                            width=-11, takefocus=False, command=self.show_pane,
                            image=(app.icon(icon, "text-3", 20), "selected", app.icon(icon, "acc-fg", 20))
                            ).pack(side="left", padx=2)

        # The window is as tall as its pane (see fit()), but a pane taller than the screen has to scroll.
        canvas = self.canvas = tk.Canvas(top, highlightthickness=0, borderwidth=0, yscrollincrement=16,
                                         width=self.WIDTH + 120, height=1)
        canvas.pack(fill="both", expand=True)
        bar = ttk.Scrollbar(canvas, orient="vertical", command=canvas.yview)

        def span(first, last):  # laid over the edge, so the column of cards stays put when it comes and goes
            bar.set(first, last)
            if float(first) > 0 or float(last) < 1:
                bar.place(relx=1, rely=0, anchor="ne", relheight=1)
            else:
                bar.place_forget()

        canvas.configure(yscrollcommand=span)
        column = self.column = ttk.Frame(canvas, padding=(0, 22, 0, 6))
        held = canvas.create_window(0, 0, anchor="n", window=column, width=self.WIDTH)
        canvas.bind("<Configure>", lambda event: canvas.coords(held, event.width // 2, 0))
        column.bind("<Configure>", lambda event: canvas.configure(scrollregion=(0, 0, 1, event.height)))

        def wheel(event):
            if column.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-1 if event.num == 4 or event.delta > 0 else 1, "units")

        for gesture in ("<MouseWheel>", "<Button-4>", "<Button-5>"):  # X11 sends the wheel as buttons 4 and 5
            top.bind(gesture, wheel)

        self.traces = []  # (var, trace) for each of the App's variables this window follows; close() drops them
        self.panes = {}
        for key, _, _ in self.PANES:
            self.panes[key] = ttk.Frame(column)
            getattr(self, f"build_{key}")(self.panes[key])
        app.painters.append(self.paint)
        app.followers.append(self.told)
        top.protocol("WM_DELETE_WINDOW", self.close)
        for keys in ("<Escape>", "<Command-w>" if sys.platform == "darwin" else "<Control-w>"):
            top.bind(keys, lambda _: self.close())

        top.minsize(self.WIDTH + 120, 240)
        top.geometry(f"+{root.winfo_rootx() + max(0, (root.winfo_width() - self.WIDTH - 120) // 2)}"
                     f"+{root.winfo_rooty() + 40}")  # over the main window
        self.show_pane()
        app.retheme(force=True)  # reaches the new window, and runs paint()
        top.deiconify()

    def close(self):
        app = self.app
        app.painters.remove(self.paint)
        app.followers.remove(self.told)
        for var, trace in self.traces:
            var.trace_remove("write", trace)
        app.prefs = None
        self.top.destroy()

    def follow(self, var, changed):
        """Call changed() whenever one of the App's variables is set, here or in the main window."""
        self.traces.append((var, var.trace_add("write", lambda *_: changed())))

    def told(self, what, *args):
        """News from the App: see App.tell()."""
        if what == "repos":
            self.show_repos()
        elif what == "rights":
            repo, answer = args
            if answer is None:
                self.unknown.add(repo)  # keeps any earlier answer
            else:
                self.app.rights[repo] = answer
            self.show_repos()
        elif what == "unadded":
            self.repo_note.configure(text=f"Can't add {args[0]}: {args[1]}", style="Problem.TLabel")
        elif what == "login":
            self.login.set(args[0])

    def paint(self, p):
        """Color what ttk.Style can't: the canvases."""
        self.canvas.configure(background=p["win"])
        self.show_look()

    def show_pane(self):
        key = self.pane.get()
        for name, pane in self.panes.items():
            if name != key:  # not the one that's up: macOS Tk leaves a pane blank that's taken down and put back
                pane.pack_forget()
        self.panes[key].pack(fill="x")
        self.canvas.yview_moveto(0)
        self.top.title(next(label for name, label, _ in self.PANES if name == key))  # as macOS settings windows do
        self.fit()

    def fit(self):
        """Make the window as tall as the pane that's up, as far as the screen has room."""
        top = self.top
        top.update_idletasks()  # so the pane has asked for its height
        height = self.strip.winfo_reqheight() + 1 + self.column.winfo_reqheight()
        top.geometry(f"{self.WIDTH + 120}x{min(height, top.winfo_screenheight() - 160)}")

    # What the panes are made of.

    def section(self, pane, title=None):
        """A group of cards in a pane, under its title if it has one."""
        box = ttk.Frame(pane)
        box.pack(fill="x", pady=(0, 22))
        if title:
            ttk.Label(box, text=title, style="Section.TLabel").pack(anchor="w", padx=4, pady=(0, 8))
        return box

    def card(self, parent):
        card = ttk.Frame(parent, style="Edge.Group.TFrame", padding=1)
        card.pack(fill="x")
        return card

    def row(self, card, title, help=None, style="Group.TLabel", image=None):
        """A row of a card: its title over smaller help text, under a rule unless it's the card's first.
        Returns the row. Its control or value goes on its right, with right()."""
        if card.winfo_children():
            ttk.Frame(card, style="GroupRule.TFrame", height=1).pack(fill="x")
        row = ttk.Frame(card, style="Group.TFrame", padding=(14, 11))
        row.pack(fill="x")
        row.columnconfigure(1, weight=1)
        if image:
            ttk.Label(row, image=image, style="Group.TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(row, text=title, style=style, wraplength=400).grid(row=0, column=1, sticky="w")  # a long path
        if help:
            ttk.Label(row, text=help, style="Muted.Group.TLabel", wraplength=400, justify="left").grid(
                row=1, column=1, sticky="w", pady=(1, 0))
        return row

    @staticmethod
    def right(row, widget):
        widget.grid(row=0, column=2, rowspan=2, padx=(16, 0))
        return widget

    def switch(self, row, var, command):
        return self.right(row, ttk.Checkbutton(
            row, variable=var, style="Group.Switch.TCheckbutton", command=command,
            image=(self.app.switch(False), "selected", self.app.switch(True))))

    # The panes.

    def build_general(self, pane):
        app = self.app
        looks = ttk.Frame(self.section(pane, "Appearance"))
        looks.pack(fill="x")
        self.looks = {}  # appearance -> (its preview, its name)
        for at, (value, label) in enumerate(self.LOOKS):
            looks.columnconfigure(at, weight=1, uniform="looks")
            cell = ttk.Frame(looks)
            cell.grid(row=0, column=at)
            preview = tk.Canvas(cell, width=188, height=98, highlightthickness=0, borderwidth=0, cursor="hand2")
            preview.pack()
            name = ttk.Label(cell, text=label, style="Choice.TLabel", cursor="hand2")
            name.pack(pady=(6, 0))
            for part in (preview, name):
                part.bind("<Button-1>", lambda _, value=value: self.pick_look(value))
            self.looks[value] = preview, name

        polling = self.card(self.section(pane, "Polling"))
        row = self.row(polling, "Poll every", "How often prherd lists open pull requests on each repo.")
        every = self.right(row, ttk.Frame(row, style="Group.TFrame"))
        # The toolbar's own variable and handlers, so the two fields are one setting.
        spin = ttk.Spinbox(every, from_=1, to=MAX_INTERVAL, width=6, textvariable=app.interval_var, command=app.retime,
                           justify="right")
        spin.pack(side="left")
        for event in ("<Return>", "<FocusOut>"):
            spin.bind(event, app.retime)
        ttk.Label(every, text="seconds", style="Value.Group.TLabel").pack(side="left", padx=(6, 0))
        self.login = tk.BooleanVar(value=login_item_enabled())
        row = self.row(polling, "Open at login", "Start watching as soon as you sign in.")
        self.switch(row, self.login, self.set_login)
        if sys.platform == "darwin":  # only macOS has a menu bar item to show
            self.in_menubar = tk.BooleanVar(value=app.menubar)
            row = self.row(polling, "Show in menu bar", "Counts of ready and blocked PRs, and Poll now, from any app.")
            self.switch(row, self.in_menubar, lambda: app.set_menubar(self.in_menubar.get()))

        saved = self.card(self.section(pane, "Saved settings"))
        home = os.path.expanduser("~")
        path = "~" + CONFIG[len(home):] if CONFIG.startswith(home + os.sep) else CONFIG
        row = self.row(saved, path, "Repos, merge mode, interval, Auto-merge, notifications and the window's layout."
                                    " Saved on every change.", style="Mono.Group.TLabel")
        self.right(row, ttk.Button(row, text=REVEAL, command=self.reveal))

    def show_look(self):
        """Draw the three appearance previews, with a ring around the one in use."""
        p = self.app.palette
        for value, (canvas, name) in self.looks.items():
            on = value == self.app.appearance
            name.state(["selected" if on else "!selected"])
            canvas.delete("all")
            canvas.configure(background=p["win"])
            x0, y0, x1, y1 = 4, 4, int(canvas.cget("width")) - 4, int(canvas.cget("height")) - 4
            middle = (x0 + x1) // 2

            def box(left, top, right, bottom, color):
                """A rectangle of the preview in its own colors. Match system's is light to the left, dark to
                the right."""
                sides = (("light", x0, middle), ("dark", middle, x1)) if value == "system" else ((value, x0, x1),)
                for look, start, end in sides:
                    if max(left, start) < min(right, end):
                        canvas.create_rectangle(max(left, start), top, min(right, end), bottom, width=0,
                                                fill=self.PREVIEW[look].get(color, color))

            side = x0 + (x1 - x0) * 34 // 100
            box(x0, y0, x1, y1, "pane")
            box(x0, y0, side, y1, "side")
            box(side, y0, side + 1, y1, "line")
            for at, part in enumerate((70, 90, 60)):
                box(x0 + 8, y0 + 10 + at * 12, x0 + 8 + (side - x0 - 16) * part // 100, y0 + 16 + at * 12, "bar")
            left, right = side + 11, x1 - 10
            box(left, y0 + 10, left + (right - left) * 55 // 100, y0 + 18, "head")
            for at in range(2):
                box(left, y0 + 25 + at * 19, right, y0 + 37 + at * 19, "row")
            box(left, y0 + 63, left + (right - left) * 40 // 100, y0 + 75, "#4F46E5")
            canvas.create_rectangle(x0 - 1, y0 - 1, x1, y1, outline=p["line"])
            if on:
                canvas.create_rectangle(x0 - 3, y0 - 3, x1 + 2, y1 + 2, outline=p["acc-dot"], width=2)

    def pick_look(self, value):
        app = self.app
        app.appearance = value
        app.retheme(force=True)
        app.save()

    def set_login(self):
        """Write or remove the login item off the Tk thread, then set the switch to what's true: it can fail."""
        def run(enable):
            self.app.log(login_item(enable))
            self.app.events.put(("tell", "login", login_item_enabled()))

        self.app.bg(run, self.login.get())

    def reveal(self):
        def run():
            if problem := reveal(CONFIG):
                self.app.log(problem)

        self.app.save()  # so there's a file to show, on a first launch too
        self.app.bg(run)

    def build_repos(self, pane):
        app = self.app
        box = self.section(pane, "Watched repositories")
        card = self.card(box)
        self.repo_list = ttk.Frame(card, style="Group.TFrame")
        self.repo_list.pack(fill="x")
        self.repo_rows = {}  # repo -> its row; show_repos() keeps them in step with the App's repos
        self.trashed = 0  # when trash() last removed a repo, by time.monotonic()
        self.asked, self.unknown = set(), set()  # the repos whose merge rights gh was asked for, and couldn't say
        form = ttk.Frame(card, style="Group.TFrame", padding=10)
        form.pack(fill="x")
        ttk.Button(form, text="Add", style="Primary.TButton", command=self.add).pack(side="right", padx=(8, 0))
        self.new_repo = tk.StringVar()
        entry = ttk.Entry(form, textvariable=self.new_repo)
        entry.pack(side="left", fill="x", expand=True)
        app.hint(entry, self.new_repo, "owner/repo or a local checkout")
        entry.bind("<Return>", self.add)
        self.repo_note = ttk.Label(box, text=self.REPO_NOTE, style="Fineprint.TLabel", wraplength=self.WIDTH - 8,
                                   justify="left")
        self.repo_note.pack(anchor="w", padx=4, pady=(8, 0))
        self.show_repos()

    def add(self, _event=None):
        """Add the repo typed here, the way the sidebar's field does."""
        self.repo_note.configure(text=self.REPO_NOTE, style="Fineprint.TLabel")
        self.app.new_repo.set(self.new_repo.get())
        self.new_repo.set("")
        self.app.add()

    def show_repos(self):
        """The repo list as the App has it now: each repo's open PRs, and whether gh's account can merge there."""
        app = self.app
        if list(self.repo_rows) != app.repos:  # a repo came or went: make the rows afresh
            for row in self.repo_rows.values():
                row.destroy()
            self.repo_rows = {repo: self.repo_row(repo) for repo in app.repos}
            if self.repo_list.winfo_ismapped():
                self.fit()
        for repo, row in self.repo_rows.items():
            rows = app.rows.get(repo)
            row.note.configure(text="couldn't list its pull requests" if repo in app.failed else
                               "not polled yet" if rows is None else f"{len(rows)} open")
            if repo in app.rights:
                text, style, dot = (("Can merge", "Ok", "ok-dot") if app.rights[repo] else
                                    ("Can't merge", "Bad", "bad-dot"))
            else:
                text, style, dot = "Unknown" if repo in self.unknown else "Checking…", "Wait", "open-dot"
            row.badge.configure(text=text, style=f"{style}.Rights.TLabel", image=app.dot(dot, 6))
            if repo not in self.asked:  # once per window: a cached answer shows meanwhile, and is checked again
                self.asked.add(repo)
                app.bg(lambda repo=repo: app.events.put(("tell", "rights", repo, can_merge(repo))))

    def trash(self, repo):
        """Remove repo. Not within a moment of the last removal: the rows close up, so a double click's second click
        would land on the next repo's button and take that too."""
        if time.monotonic() - self.trashed >= 0.6:
            self.trashed = time.monotonic()
            self.app.remove([repo])

    def repo_row(self, repo):
        app = self.app
        row = ttk.Frame(self.repo_list, style="Group.TFrame")
        row.pack(fill="x")
        ttk.Frame(row, style="GroupRule.TFrame", height=1).pack(side="bottom", fill="x")
        line = ttk.Frame(row, style="Group.TFrame", padding=(14, 9, 9, 9))
        line.pack(fill="x")
        line.columnconfigure(1, weight=1)
        name = short(repo)
        tint = self.TINTS[zlib.crc32(name.encode()) % len(self.TINTS)]
        ttk.Label(line, text=name.rpartition("/")[2][:1].upper(), image=tile(app, tint, 26), compound="center",
                  style="Tile.Group.TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(line, text=name, style="Mono.Group.TLabel").grid(row=0, column=1, sticky="w")
        row.note = ttk.Label(line, style="Muted.Group.TLabel")
        row.note.grid(row=1, column=1, sticky="w")
        row.badge = ttk.Label(line, compound="left")
        row.badge.grid(row=0, column=2, rowspan=2, padx=(12, 6))
        ttk.Button(line, image=app.icon("trash", "muted", 14), style="Group.TButton", takefocus=False,
                   command=lambda: self.trash(repo)).grid(row=0, column=3, rowspan=2)
        return row

    def build_merging(self, pane):
        app = self.app
        row = self.row(self.card(self.section(pane)), "Auto-merge",
                       "Off means watch only. Conflict comments are still posted.", style="Bold.Group.TLabel")
        self.switch(row, app.merging, lambda: app.control(merging=app.merging.get()))

        modes = self.section(pane, "Merge mode")
        self.mode_cards = {}  # mode -> the parts of its card that light up while it's the mode
        for label, value in MODES:
            flag, text = self.MODE_HELP[value]
            card = ttk.Frame(modes, style="Pick.TFrame", padding=(14, 11))
            card.pack(fill="x", pady=(0, 8))
            card.columnconfigure(1, weight=1)
            # The toolbar's variable and handler: this is the same control, drawn as cards.
            ttk.Radiobutton(card, text=label, variable=app.mode_var, value=value, style="Pick.TRadiobutton",
                            compound="left", image=(self.radio(False), "selected", self.radio(True)),
                            command=lambda: app.control(mode=app.mode_var.get())).grid(row=0, column=0, sticky="w")
            ttk.Label(card, text=flag, style="Flag.Pick.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
            ttk.Label(card, text=text, style="Pick.TLabel").grid(row=1, column=0, columnspan=2, sticky="w",
                                                                  padx=(28, 0), pady=(2, 0))
            self.mode_cards[value] = (card, *card.winfo_children())
            for part in self.mode_cards[value][:1] + self.mode_cards[value][2:]:  # the radio button clicks itself
                part.bind("<Button-1>", lambda _, value=value: self.pick_mode(value))
        self.admin_note = ttk.Frame(modes, style="Admin.TFrame", padding=(12, 9))
        ttk.Label(self.admin_note, style="Admin.TLabel", text=(
            "Admin bypasses branch protection on every watched repo. Merge now uses it too.")).pack(anchor="w")
        self.follow(app.mode_var, self.show_mode)
        self.show_mode()

        rules = self.card(self.section(pane, "Rules"))
        row = self.row(rules, "Merge method")
        self.right(row, ttk.Label(row, text="Squash, pinned to the head commit", style="Value.Group.TLabel"))
        row = self.row(rules, "Hold label")
        self.right(row, ttk.Label(row, text=DONT_MERGE, style="Code.Group.TLabel"))
        row = self.row(rules, "Conflicts", "Comments once per head commit, then waits.")
        self.right(row, ttk.Label(row, text="Comment and skip", style="Value.Group.TLabel"))

    def radio(self, on):
        """A mode card's dot: a ring, thick and in the accent when it's the one chosen."""
        edge, hole = ("acc-dot", 9) if on else ("field-border", 20)
        return self.app.image(("radio", on), lambda p: draw(((p[edge], 24, ((12, 12, 12, 12),)),
                                                             (p["field"], hole, ((12, 12, 12, 12),))), 16, 16))

    def pick_mode(self, value):
        self.app.mode_var.set(value)
        self.app.control(mode=value)

    def show_mode(self):
        mode = self.app.mode_var.get()
        for value, parts in self.mode_cards.items():
            for part in parts:
                part.state(["selected" if value == mode else "!selected"])
        if mode == "admin":
            self.admin_note.pack(fill="x")
        else:
            self.admin_note.pack_forget()
        if self.panes["merging"].winfo_manager():  # it's the pane that's up, and now a different height
            self.fit()

    def build_codex(self, pane):
        app = self.app
        known = BOT_ID == 199175422  # else it's whoever BOT_ID names
        row = self.row(self.card(self.section(pane, "Reviewer")),
                       "chatgpt-codex-connector[bot]" if known else "The account set by BOT_ID",
                       "Matched by user id, so no other account can pass as Codex.", style="Bold.Group.TLabel",
                       image=tile(app, "inv", 34, "sparkle", "inv-fg"))
        who = self.right(row, ttk.Frame(row, style="Group.TFrame"))
        ttk.Label(who, text="User id", style="Muted.Group.TLabel").pack(anchor="e")
        ttk.Label(who, text=BOT_ID, style="Mono.Group.TLabel").pack(anchor="e")

        counts = self.card(self.section(pane, "Counts as a go-ahead"))
        self.row(counts, "Thumbs-up reaction on the pull request",
                 "Must be newer than the latest push or check suite.", image=check_bullet(app, True))
        self.row(counts, "“Didn't find any major issues” review receipt",
                 "Only when its reviewed commit matches the current head.", image=check_bullet(app, True))

        row = self.row(self.card(self.section(pane, "Requesting a review")), "Comment posted")
        self.right(row, ttk.Label(row, text="@codex review", style="Code.Group.TLabel"))

    def build_notify(self, pane):
        app = self.app
        box = self.section(pane, "Notify me when")
        card = self.card(box)
        self.notify = {}  # kind -> its switch's variable
        for kind in NOTIFY_KINDS:
            title, text = self.NOTIFY_HELP[kind]
            self.notify[kind] = tk.BooleanVar(value=app.notify[kind])
            self.switch(self.row(card, title, text), self.notify[kind], lambda kind=kind: self.set_notify(kind))
        if not can_notify():
            note = ttk.Frame(box, style="Admin.TFrame", padding=(12, 9))
            note.pack(fill="x", pady=(8, 0))
            ttk.Label(note, style="Admin.TLabel", wraplength=self.WIDTH - 24, justify="left", text=(
                "Notifications can't be shown on this computer: Windows has nothing built in to post them with."
                if sys.platform == "win32" else
                "Notifications can't be shown on this computer: install notify-send (libnotify) to get them."
                if sys.platform != "darwin" else
                "Notifications can't be shown on this computer: osascript is missing.")).pack(anchor="w")

    def set_notify(self, kind):
        self.app.notify[kind] = self.notify[kind].get()
        self.app.save()


class FirstRun:
    """The first-run screen, after design/FirstRun.dc.html: shown where the table is while no repo is watched.

    With it up, the window is the toolbar and this: the sidebar, the heading and the log have nothing to show
    yet, so they're put away until the first repo is added.
    """

    CHECKS = ("GitHub CLI installed", "Signed in")  # setup_checks()'s titles, to show while it's still asking
    STEPS = (("A thumbs-up from Codex", "on the pull request, or its review receipt saying no major issues for the"
                                        " current head."),
             ("Newer than the latest commit.", "A push or re-run check after the go-ahead voids it, using GitHub's"
                                               " own timestamps."),
             ("No conflicts, no hold.", "Conflicting pull requests get one comment. Anything labeled "
                                        f"{DONT_MERGE.replace('-', chr(0x2011))} is left alone."))  # unbroken
    HINT = "GitHub Enterprise hosts work too. Press Return to add."

    def __init__(self, app, waiting):
        self.app = app
        self.waiting = waiting  # the repos saved or passed at launch haven't arrived yet: no repos isn't news
        self.screen, self.up = None, False  # built when first shown; most launches never show it
        self.put_away = []  # the chrome hidden while this is up, and how to put each piece back
        self.fitting = None  # the fit() that's waiting
        app.followers.append(self.told)

    def told(self, what, *args):
        """News from the App: see App.tell()."""
        if what == "repos":
            self.show()
        elif what == "unadded":
            self.waiting = False  # a repo passed at launch that can't be added isn't coming
            self.show()
            if self.up:
                self.say(f"Can't add {args[0]}: {args[1]}", "Problem.TLabel")
        elif what == "checks" and self.screen:
            for row, result in zip(self.checks, args[0]):
                self.show_check(row, *result)

    def show(self):
        """Put the screen up, or take it down, to match the repo list."""
        app = self.app
        if app.repos:
            self.waiting = False
        on = not app.repos and not self.waiting
        if on == self.up:
            return
        self.up = on
        if on:
            if not self.screen:
                self.build()
            app.swap(self.screen)
            self.say()
            self.check()
            self.entry.focus_set()
        else:
            app.swap()
        self.chrome(not on)

    def chrome(self, on):
        """Put away the sidebar and anything else beside the main column, and the heading and log in it, or put
        them back as they were."""
        root, body, main = self.app.root, self.app.body, self.app.main
        if on:
            self.app.sidebar.configure(width=self.app.sidebar_width)  # as wide as it was dragged, not as first made
            self.app.content.pack_forget()
            for put_back in self.put_away:
                put_back()
            self.app.content.pack(fill="both", expand=True)
            self.put_away = []
        elif not self.put_away:
            self.least = root.minsize()  # fit() raises it while this is up
            self.put_away.append(lambda: root.minsize(*self.least))
            panes = [str(pane) for pane in body.panes()]
            for at, pane in enumerate(panes):
                if pane != str(main):
                    self.put_away.append(lambda at=at, pane=pane, weight=body.pane(pane, "weight"):
                                         body.insert(at, pane, weight=weight))
            for widget in main.pack_slaves():
                if widget is not self.app.content:
                    self.put_away.append(lambda widget=widget, how=widget.pack_info(): widget.pack(**how))
            for pane in panes:
                if pane != str(main):
                    body.forget(pane)
            for widget in main.pack_slaves():
                if widget is not self.app.content:
                    widget.pack_forget()

    def fit(self):
        """Keep the window from being made too short to show all of this: unlike the table, it doesn't scroll."""
        root, self.fitting = self.app.root, None
        # What the window asks for is this under the toolbar and any banner: nothing else is packed, and none of
        # it depends on the height the window has, so raising its least height can't feed back.
        least = self.least[0], max(self.least[1], root.winfo_reqheight())
        if self.up and least != root.minsize():
            root.minsize(*least)

    def refit(self):
        """fit() once the layout has settled: a paragraph that rewraps changes what the window asks for."""
        if self.fitting:
            self.app.root.after_cancel(self.fitting)
        self.fitting = self.app.root.after(50, self.fit)

    def build(self):
        app = self.app
        screen = self.screen = ttk.Frame(app.content)
        screen.columnconfigure(0, weight=5, uniform="halves")
        screen.columnconfigure(2, weight=4, uniform="halves")
        screen.rowconfigure(0, weight=1)
        left = ttk.Frame(screen, padding=(48, 40, 40, 28))
        left.grid(row=0, column=0, sticky="nsew")
        ttk.Frame(screen, style="Line.TFrame", width=1).grid(row=0, column=1, sticky="ns")
        aside = ttk.Frame(screen, style="Side.TFrame", padding=(32, 40, 32, 28))
        aside.grid(row=0, column=2, sticky="nsew")
        wrapped = []  # (label, the container it fills, the width beside it that isn't its own, its widest)

        logo = app.image("logo", lambda p: draw((("#4F46E5", 12, (SQUARE,)), ("#FFFFFF", 1.8, (
            (6.9, 8.1, 16.7, 12, 6.9, 15.9),)), ("#FFFFFF", 4.6, ((6.9, 8.1, 6.9, 8.1), (6.9, 15.9, 6.9, 15.9))),
            ("#FFFFFF", 5.6, ((16.7, 12, 16.7, 12),))), 40, 40))
        ttk.Label(left, text="  prherd", image=logo, compound="left", style="Title.TLabel").pack(anchor="w")
        hero = ttk.Label(left, text="Merge what Codex clears.\nSkip what it doesn't.", style="Hero.TLabel",
                         justify="left")
        hero.pack(anchor="w", pady=(24, 0))
        lead = ttk.Label(left, style="Lead.TLabel", justify="left", text=(
            "Add the repositories to watch. prherd polls their open pull requests and squash-merges each one once"
            " Codex signs off on its latest commit."))
        lead.pack(anchor="w", pady=(10, 0))
        ttk.Label(left, text="Repository", style="Section.TLabel").pack(anchor="w", pady=(26, 6))
        form = ttk.Frame(left)
        form.pack(fill="x")
        ttk.Button(form, text="Add", style="Big.Primary.TButton", command=self.add).pack(side="right", fill="y",
                                                                                         padx=(8, 0))
        # The sidebar's own variable, so Return here is the sidebar's add.
        self.entry = ttk.Entry(form, textvariable=app.new_repo, style="Big.TEntry")
        self.entry.pack(side="left", fill="x", expand=True)
        app.hint(self.entry, app.new_repo, "owner/repo or ~/path/to/checkout", x=11)
        self.entry.bind("<Return>", self.add)
        self.status = ttk.Label(left, justify="left")  # the hint, then how the adding is going
        self.status.pack(anchor="w", pady=(8, 0))
        foot = ttk.Frame(left)
        foot.pack(side="bottom", fill="x", pady=(20, 0))
        settings = ttk.Button(foot, text="Settings…", command=app.settings)
        settings.pack(side="left")
        self.note = ttk.Label(foot, style="Fineprint.TLabel", justify="left")
        self.note.pack(side="left", padx=(12, 0))
        for var in (app.merging, app.mode_var):  # never dropped: this screen lasts as long as the window
            var.trace_add("write", self.show_note)
        self.show_note()
        wrapped += [(hero, left, 0, 9999), (lead, left, 0, 460), (self.status, left, 0, 9999),
                    (self.note, left, settings.winfo_reqwidth() + 16, 9999)]

        head = ttk.Frame(aside, style="Side.TFrame")
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="SETUP", style="Caps.Side.TLabel").pack(side="left")
        ttk.Button(head, text="Re-check", style="Link.Side.TButton", takefocus=False, command=self.check).pack(
            side="right")
        self.checks = [self.check_row(aside, title) for title in self.CHECKS]
        for row in self.checks:
            wrapped.append((row.problem, aside, 36, 9999))  # beside it: the bullet, and a little to spare
        rights = self.check_row(aside, "Merge rights")  # not one of the checks: there's no repo to ask about yet
        self.show_check(rights, "", rights.title.cget("text"), "checked in Settings")

        ttk.Label(aside, text="WHAT COUNTS AS A GO-AHEAD", style="Caps.Side.TLabel").pack(anchor="w", pady=(26, 6))
        chip = app.image("step", lambda p: draw(((p["field-border"], 12, (SQUARE,)), (p["win"], 10.2, (SQUARE,))),
                                                22, 22))
        for at, (title, text) in enumerate(self.STEPS, 1):
            step = ttk.Frame(aside, style="Side.TFrame")
            step.pack(fill="x", pady=5)
            step.columnconfigure(1, weight=1)
            ttk.Label(step, text=at, image=chip, compound="center", style="Step.Side.TLabel").grid(
                row=0, column=0, rowspan=2, sticky="n", padx=(0, 10), pady=(1, 0))
            ttk.Label(step, text=title, style="Bold.Side.TLabel").grid(row=0, column=1, sticky="w")
            body = ttk.Label(step, text=text, style="Side.TLabel", justify="left")
            body.grid(row=1, column=1, sticky="w", pady=(1, 0))
            wrapped.append((body, aside, 38, 9999))

        def wrap(event):
            """Wrap each paragraph to the column it's in. A label can't: it only knows the width it asked for."""
            for label, container, beside, widest in wrapped:
                if container is event.widget:
                    pad = container.cget("padding")
                    room = event.width - sum(int(str(pad[at])) for at in (0, 2)) - beside
                    label.configure(wraplength=max(120, min(room, widest)))
            self.refit()

        for label, _, _, _ in wrapped:  # narrow until first measured, so no paragraph holds its column wide
            label.configure(wraplength=240)
        left.bind("<Configure>", wrap)
        aside.bind("<Configure>", wrap)

    def check_row(self, parent, title):
        """A setup check: its bullet, title and what it found, over what's wrong if it failed."""
        row = ttk.Frame(parent, style="Side.TFrame")
        row.pack(fill="x", pady=4)
        row.columnconfigure(1, weight=1)
        row.bullet = ttk.Label(row, style="Side.TLabel")
        row.bullet.grid(row=0, column=0, padx=(0, 10))
        row.title = ttk.Label(row, text=title, style="Side.TLabel")
        row.title.grid(row=0, column=1, sticky="w")
        row.found = ttk.Label(row, style="Muted.Side.TLabel")
        row.found.grid(row=0, column=2, sticky="e")
        row.problem = ttk.Label(row, style="Problem.Side.TLabel", justify="left")
        return row

    def show_check(self, row, ok, title, note):
        """One check as setup_checks() gives it. ok is None while it's still being asked, "" if it never is."""
        row.bullet.configure(image=check_bullet(self.app, ok))
        row.title.configure(text=title)
        row.found.configure(text="" if ok is False else note)
        row.problem.configure(text=note)
        if ok is False:  # what's wrong is a sentence: it goes under the title, where it can wrap
            row.problem.grid(row=1, column=1, columnspan=2, sticky="w", pady=(1, 0))
        else:
            row.problem.grid_remove()
        self.refit()

    def check(self):
        """Run the setup checks afresh, off the Tk thread. Each says it's checking until they answer."""
        def run():
            try:
                results = setup_checks()
            except Exception as e:  # as poll_all(): say so rather than leave the rows checking for good
                results = [(False, title, f"check crashed: {e!r}") for title in self.CHECKS]
            self.app.events.put(("tell", "checks", results))

        for row in self.checks:
            self.show_check(row, None, row.title.cget("text"), "checking…")
        self.app.bg(run)

    def say(self, text=HINT, style="Fineprint.TLabel"):
        self.status.configure(text=text, style=style)
        self.refit()

    def show_note(self, *_):
        app = self.app
        mode = {value: label for label, value in MODES}.get(app.mode_var.get(), MODES[0][0])
        self.note.configure(text=f"Auto-merge is {'on' if app.merging.get() else 'off'}, in {mode} mode."
                                 " Change either in the toolbar or in Settings.")

    def add(self, _event=None):
        target = self.app.new_repo.get().strip()
        if target:
            self.say(f"Adding {target}…")
            self.app.add()


def gui(targets, mode, interval):
    App(targets, mode, interval).run()


def positive(value):
    if not value.isdigit() or int(value) == 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return int(value)


def parse(argv=None):
    p = argparse.ArgumentParser(description="Squash-merge PRs Codex has cleared.")
    p.add_argument("repos", nargs="*", metavar="repo", help="owner/repo or a local checkout")
    # Both write mode, so the last flag wins: `--admin --force` runs as --force.
    # None means "not given", so the window falls back to its saved settings.
    p.add_argument("--force", dest="mode", action="store_const", const="force", default=None,
                   help="merge now instead of queuing with --auto")
    p.add_argument("--admin", dest="mode", action="store_const", const="admin",
                   help="merge now and bypass branch protection")
    p.add_argument("--interval", type=positive, help="seconds between polls (default 60)")
    p.add_argument("--headless", action="store_true",
                   help="no window; log to stdout (default repo: $REPO or the current directory)")
    p.add_argument("--version", action="version", version=f"prherd {__version__}")
    return p.parse_args(argv)


def main(argv=None):
    a = parse(argv)
    if not a.headless:
        return gui(a.repos, a.mode, a.interval)
    try:
        repos = [resolve(t) for t in a.repos or [os.environ.get("REPO", ".")]]
    except RuntimeError as e:
        sys.exit(f"can't resolve repo: {e}")
    watch(repos, a.mode or "", a.interval or 60)


if __name__ == "__main__":
    main()
