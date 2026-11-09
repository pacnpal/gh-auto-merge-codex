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
    return {
        "repos": [r for r in repos if isinstance(r, str)] if isinstance(repos, list) else [],
        "mode": s.get("mode") if s.get("mode") in [v for _, v in MODES] else "",
        "interval": interval if type(interval) is int and interval > 0 else 60,
        "merging": s.get("merging") is not False,
        "geometry": geometry if isinstance(geometry, str) else None,
        "appearance": s.get("appearance") if s.get("appearance") in APPEARANCES else "system",
        "view": view if isinstance(view, str) and view in VIEWS else "all",
        "sidebar": sidebar if type(sidebar) is int and SIDEBAR[0] <= sidebar <= SIDEBAR[1] else 232,
        "log": s.get("log") is not False,
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
    ("error", "err", ("failed", "crashed", "can't")),
    ("conflict", "bad", ("merge conflict",)),
    ("review", "warn", ("asked codex", "codex is reviewing")),
    ("codex", "ok", ("squash merging",)),
    ("merge", "acc", ("merge queued", "merged now", "not merged", "auto-merge")),
    ("poll", "hold", ("open pr(s)", "review yet", "skipping")),
)
SIDEBAR = (160, 480)  # the narrowest and widest sidebar a saved width restores
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
    "plus": ((12, 5, 12, 19), (5, 12, 19, 12)),
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
                      "caps": cut("TkDefaultFont", -2, "bold"), "title": cut("TkDefaultFont", 6, "bold"),
                      "mono": cut("TkFixedFont")}
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


class App:
    """The window: a toolbar and banners over a sidebar, the PR table and the activity log.

    Worker threads never touch Tk. They post to self.events, which drain() empties on the Tk thread.
    """

    # Right-click is Button-3, except on macOS Tk 8.6, which calls it Button-2.
    MENU_GESTURES = ("<Button-2>", "<Button-3>", "<Control-Button-1>")

    def __init__(self, targets, mode, interval):
        global tk, ttk, messagebox  # imported here, not at the top: --headless runs where Tk isn't installed
        import tkinter as tk
        from tkinter import messagebox, ttk

        saved = load_settings()
        mode = saved["mode"] if mode is None else mode  # a CLI flag wins for this run
        self.interval = interval or saved["interval"]  # the seconds between polls, as last read from the spinbox
        self.root = root = tk.Tk()
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
        self.appearance = saved["appearance"]
        self.dark = None  # the palette in use; None until the first theme()
        self.busy = False
        self.timer = None  # the tick that's waiting
        self.due = time.time()  # when it fires
        self.view, self.only = saved["view"], None  # the sidebar's view, and the one repo picked there, if any
        self.adding = False  # whether + has opened the sidebar's add-a-repo entry
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
        self.build_menus()
        self.query.trace_add("write", lambda *_: self.render())
        root.bind("<Command-k>" if sys.platform == "darwin" else "<Control-k>", lambda _: self.search.focus_set())

        for repo in saved["repos"]:
            self.bg(self.show, repo)  # already resolved; re-resolving offline would drop it from the next save
        for target in targets:
            self.bg(self.add_repo, target)
        self.bg(lambda: self.events.put(("account", whoami())))
        root.protocol("WM_DELETE_WINDOW", self.close)
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
        spin = ttk.Spinbox(every, from_=1, to=86400, width=5, textvariable=self.interval_var, command=self.retime)
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
            ttk.Label(row, text=lead, style=f"Bold.{style}.TLabel").pack(side="left")
            ttk.Label(row, text=text, style=f"{style}.TLabel").pack(side="left")
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

        # Who gh is signed in as. show_account() packs it at the foot once gh has said.
        self.account = ttk.Frame(side, style="Side.TFrame")
        ttk.Frame(self.account, style="Line.TFrame", height=1).pack(fill="x", padx=10)
        who = ttk.Frame(self.account, style="Side.TFrame", padding=(18, 10))
        who.pack(fill="x")
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
            the edge. Leave out Head, then Age, while they won't fit beside a readable PR column."""
            shown = list(tree["columns"])
            for column in ("head", "age"):
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

    def hint(self, entry, var, text):
        """Grey text in an entry while it's empty, saying what goes there. Tk 8.6's entries have no placeholder."""
        label = ttk.Label(entry, text=text, style="Hint.Field.TLabel")
        label.bind("<Button-1>", lambda _: entry.focus_set())

        def show(*_):
            label.place_forget() if var.get() else label.place(x=5, rely=0.5, anchor="w")

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
        if groups:
            self.empty.place_forget()
        else:
            title, text = (("Nothing here", "No open pull requests match this view.") if self.repos else
                           ("No repositories yet", "Add one with + in the sidebar: owner/repo, or a local checkout."))
            self.empty_title.configure(text=title)
            self.empty_text.configure(text=text)
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.show_sidebar()

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
        """Lay the selection bar over the heading while two or more PRs are selected, with the actions that apply."""
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

    def show_log(self, at, msg):
        box, kind = self.logbox, kind_of(msg)
        subject, text = split_log(msg)
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
            self.account.pack(side="bottom", fill="x", before=self.side_canvas)

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

    def control(self, **change):
        """A merge control changed: tell the workers, show what it means, save it."""
        self.live.update(change)
        self.show_state()
        self.save()

    def seconds(self):
        try:
            return max(1, self.interval_var.get())
        except tk.TclError:  # spinbox holds non-numeric text
            return self.interval

    def retime(self, _event=None):
        """The interval changed: count down from it afresh. Not on every key, or typing 120 would poll after 1."""
        if self.seconds() != self.interval:
            self.schedule()
            self.save()

    # Polling.

    def log(self, msg):
        self.events.put(("log", time.strftime("%T"), msg))

    def bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def add_repo(self, target):
        try:
            repo = resolve(target)
        except Exception as e:
            return self.log(f"can't add {target}: {e!r}")
        self.show(repo)

    def refresh(self, repo):
        try:
            self.events.put(("prs", repo, poll(repo, self.log, lambda: None)))  # show it now; merge on the next tick
        except Exception as e:
            self.log(f"{repo}: poll crashed; retrying next interval ({e!r})")

    def show(self, repo):
        self.events.put(("repo", repo))
        self.refresh(repo)

    def poll_all(self, snapshot):
        try:
            for repo in snapshot:
                mode = lambda repo=repo: self.live["mode"] if self.live["merging"] and repo in self.repos else None
                self.events.put(("prs", repo, poll(repo, self.log, mode)))
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
                repo, rows = rest
                if rows is None:  # couldn't list them: keep showing the last list that could
                    self.failed.add(repo)
                else:
                    self.failed.discard(repo)
                    self.rows[repo] = rows
                changed = True
            elif kind == "done":
                self.busy = False
            elif kind == "account":
                self.show_account(rest[0])
        if changed:
            self.render()
        self.root.after(200, self.drain)

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
        lines = [f"#{pr['number']} {pr['title']} ({VERDICTS[self.verdicts[iid]]['label']})"
                 for iid, pr in chosen.items()]
        mode = "admin" if self.live["mode"] == "admin" else "force"
        note = "This bypasses branch protection." if mode == "admin" else "Branch protection still applies."
        if messagebox.askyesno("Merge now?", "Squash-merge without waiting for Codex?\n\n" + "\n".join(lines) + f"\n\n{note}",
                               icon="warning", default="no", parent=self.root):
            self.act_on(list(chosen), lambda repo, number: merge(repo, chosen[f"{repo}#{number}"], mode))

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

    def save(self):
        if self.sidebar.winfo_width() > 1:  # it's 1 until the window is drawn
            self.sidebar_width = min(max(self.sidebar.winfo_width(), SIDEBAR[0]), SIDEBAR[1])
        try:
            save_settings({"repos": self.repos, "mode": self.mode_var.get(), "interval": self.seconds(),
                           "merging": self.merging.get(), "geometry": self.root.geometry(),
                           "appearance": self.appearance, "view": self.view, "sidebar": self.sidebar_width,
                           "log": self.log_open})
        except OSError as e:
            self.log(f"can't save settings to {CONFIG}: {e}")

    def close(self):
        self.save()
        self.root.destroy()


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
