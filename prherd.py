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
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

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
MODES = (("Queue (--auto)", ""), ("Force", "force"), ("Admin", "admin"))
# The window's repos and controls, restored at launch.
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "prherd.json")
# A full path, no cwd and close_fds=False let subprocess use posix_spawn instead of fork: macOS libmalloc
# prints "MallocStackLogging: can't turn off..." in every forked child of the Tk process.
GH = os.path.abspath(found) if (found := shutil.which("gh")) else "gh"  # absolute: a relative PATH hit breaks under cwd


def gh(*args, cwd=None):
    try:
        return subprocess.run([GH, *args], cwd=cwd, capture_output=True, text=True, timeout=120, close_fds=cwd is not None)
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


def verdict(repo, pr):
    """How Codex cleared this PR's current head, "👀" if it's still reviewing it, or None."""
    host, nwo = repo.split("/", 1)
    issue = f"repos/{nwo}/issues/{pr['number']}"
    pushed = pushed_at(host, nwo, pr)
    if not pushed:  # can't tell when this head arrived, so no go-ahead can be tied to it
        return None
    # A reaction names no commit and survives pushes, so it only counts if it came after this head arrived.
    reactions = {r["content"] for r in api(host, f"{issue}/reactions?per_page=100")
                 if is_bot(r) and r["created_at"] > pushed}
    if "+1" in reactions:
        return "👍"
    for c in api(host, f"{issue}/comments?per_page=100"):
        body = c.get("body") or ""
        m = is_bot(c) and CLEAN.match(body) and REVIEWED.search(body)
        # Counts only for the commit it reviewed, and only if that head didn't arrive later
        # (a pushed commit crafted to share the short hash).
        if m and pr["headRefOid"].startswith(m[1]) and c["created_at"] > pushed:
            return "no major issues"
    return "👀" if "eyes" in reactions else None  # Codex reacts 👀 when it starts a review


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


def poll(repo, log, merge_mode):
    """One pass over repo's open PRs. Returns [(pr, verdict)], or None if listing failed.

    merge_mode() is asked right before each merge: the mode to merge with, or None to skip.
    Conflicting PRs are commented on either way, unless marked don't merge.
    """
    try:
        # shortcut: PRs past the 1000th open one are never seen; page through the API if a repo gets there.
        prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "1000",
                      "--json", "number,title,headRefOid,mergeable,baseRefName,labels,autoMergeRequest,isDraft,author")
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
            v = verdict(repo, pr)
        except (RuntimeError, ValueError) as e:
            log(f"{tag}: failed to fetch Codex reactions/comments; retrying ({e})")
            rows.append((pr, "?"))
            continue
        rows.append((pr, v))
        if v == "👀":
            log(f"{tag}: Codex is reviewing (👀)")
        elif not v:
            log(f"{tag}: no 👍 or clean review yet")
        elif (mode := merge_mode()) is not None:
            log(f"{tag}: {v} — squash merging")
            log(f"{tag}: {merge(repo, pr, mode)}")
    return rows


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
    return {
        "repos": [r for r in repos if isinstance(r, str)] if isinstance(repos, list) else [],
        "mode": s.get("mode") if s.get("mode") in [v for _, v in MODES] else "",
        "interval": interval if type(interval) is int and interval > 0 else 60,
        "merging": s.get("merging") is not False,
        "geometry": geometry if isinstance(geometry, str) else None,
        "appearance": s.get("appearance") if s.get("appearance") in APPEARANCES else "system",
    }


def row_values(pr, v):
    """The tree's columns for a PR. A marked PR's auto-merge is cancelled during the poll, so it isn't "queued"."""
    state = "draft" if pr["isDraft"] else "queued" if pr["autoMergeRequest"] and v != "don't merge" else ""
    return VERDICTS[v]["label"], (pr["author"] or {}).get("login", ""), state


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
    "👍": {"label": "Thumbs up", "tone": "ok"},
    "no major issues": {"label": "No major issues", "tone": "ok"},
    "👀": {"label": "Reviewing", "tone": "warn"},
    "merge conflict": {"label": "Merge conflict", "tone": "bad"},
    "?": {"label": "Lookup failed", "tone": "err"},
    "don't merge": {"label": "Don't merge", "tone": "hold"},
    None: {"label": "Awaiting Codex", "tone": "none"},
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


def theme(root, dark, appearance="system"):
    """Restyle every widget under root in the light or dark palette, on ttk's clam theme."""
    import tkinter as tk
    from tkinter import font, ttk
    p = PALETTE["dark" if dark else "light"]
    root.update_idletasks()  # Tk can't set a window's appearance before it exists
    try:  # the title bar, native menus and dialogs; "auto" keeps following the system
        root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", root._w,
                     {"light": "aqua", "dark": "darkaqua"}.get(appearance, "auto"))
    except tk.TclError:
        pass
    root.configure(background=p["win"])
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
    # width 0: as wide as the text, as aqua's are, rather than clam's 11-character minimum
    s.configure("TButton", background=p["field"], foreground=p["text"], padding=(10, 4), width=0, **flat)
    s.map("TButton", background=[("pressed", p["side-on"]), ("active", p["hover"])],
          bordercolor=focus, foreground=[("disabled", p["faint"])])
    # The design's primary button is ink on paper: near-black in light, near-white in dark.
    ink = {"bordercolor": p["inv"], "lightcolor": p["inv"], "darkcolor": p["inv"]}
    s.configure("Primary.TButton", background=p["inv"], foreground=p["inv-fg"], **ink)
    s.map("Primary.TButton", background=[("pressed", p["text-3"]), ("active", p["text-2"])],
          lightcolor=[("active", p["text-2"])], darkcolor=[("active", p["text-2"])],
          bordercolor=[("active", p["text-2"])])
    # Radiobuttons drawn as one segmented control: the chosen segment lifts out of the track.
    seg = {"bordercolor": p["seg"], "lightcolor": p["seg"], "darkcolor": p["seg"]}
    s.configure("Toolbutton", background=p["seg"], foreground=p["muted"], padding=(10, 4), relief="flat", **seg)
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
    rows = font.nametofont("TkDefaultFont").metrics("linespace") + 12
    s.configure("Treeview", background=p["win"], fieldbackground=p["win"], foreground=p["text"],
                bordercolor=p["line"], lightcolor=p["win"], darkcolor=p["win"], rowheight=rows)
    s.map("Treeview", background=[("selected", p["acc-bg"])], foreground=[("selected", p["acc-fg"])])
    s.configure("Treeview.Heading", background=p["head"], foreground=p["muted"], relief="flat",
                bordercolor=p["line"], lightcolor=p["head"], darkcolor=p["head"], padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", p["hover"])])
    for bar in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        s.configure(bar, background=p["seg"], troughcolor=p["win"], arrowcolor=p["muted"],
                    bordercolor=p["win"], lightcolor=p["seg"], darkcolor=p["seg"])
        s.map(bar, background=[("active", p["side-on"])])

    def plain(widget):  # tk widgets ttk.Style doesn't reach
        if isinstance(widget, tk.Menu):
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


def gui(targets, mode, interval):
    import tkinter as tk
    from tkinter import messagebox, ttk

    saved = load_settings()
    mode = saved["mode"] if mode is None else mode  # a CLI flag wins for this run
    interval = interval or saved["interval"]
    root = tk.Tk()
    root.title("prherd")
    if saved["geometry"]:
        root.geometry(saved["geometry"])
    events = queue.Queue()  # worker threads never touch tk; they post here
    repos = []
    prs = {}  # tree row -> its PR as of the last poll
    verdicts = {}  # tree row -> its verdict as poll() returned it; the cell shows a label, so guards read this
    appearance = saved["appearance"]
    dark = None  # the palette in use; None until the first theme()
    busy = False
    mode_var = tk.StringVar(value=mode)
    interval_var = tk.IntVar(value=interval)
    merging = tk.BooleanVar(value=saved["merging"])
    # Mirrors the controls for worker threads, which can't read tk vars. Read at each merge,
    # so removing a repo or unticking Auto-merge stops a poll that's already running.
    live = {"mode": mode, "merging": saved["merging"]}

    def log(msg):
        events.put(("log", stamp(msg)))

    def bg(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def save():
        try:
            seconds = max(1, interval_var.get())
        except tk.TclError:
            seconds = interval
        try:
            save_settings({"repos": repos, "mode": mode_var.get(), "interval": seconds,
                           "merging": merging.get(), "geometry": root.geometry(), "appearance": appearance})
        except OSError as e:
            log(f"can't save settings to {CONFIG}: {e}")

    def control(**change):
        live.update(change)
        save()

    def add_repo(target):
        try:
            repo = resolve(target)
        except Exception as e:
            return log(f"can't add {target}: {e!r}")
        show(repo)

    def refresh(repo):
        try:
            events.put(("prs", repo, poll(repo, log, lambda: None)))  # show it now; merge on the next tick
        except Exception as e:
            log(f"{repo}: poll crashed; retrying next interval ({e!r})")

    def show(repo):
        events.put(("repo", repo))
        refresh(repo)

    def add(_event=None):
        target = entry.get().strip()
        if target:
            entry.delete(0, "end")
            bg(add_repo, target)

    def remove():
        for iid in tree.selection():
            if iid in repos:
                repos.remove(iid)
                tree.delete(iid)
        save()

    def ask_codex():
        for iid in tree.selection():
            if "#" in iid:
                repo, number = iid.rsplit("#", 1)
                bg(lambda repo=repo, number=number: log(f"{repo}#{number}: {request_review(repo, number)}"))

    def selected_prs():
        return [iid for iid in tree.selection() if "#" in iid]

    def selected_repos():
        return [iid for iid in tree.selection() if "#" not in iid]

    def act_on(iids, action):
        """action(repo, number) for each PR row, off the Tk thread; then update their repos."""
        def run():
            for iid in iids:
                repo, number = iid.rsplit("#", 1)
                log(f"{iid}: {action(repo, number)}")
            for repo in {iid.rsplit("#", 1)[0] for iid in iids}:
                refresh(repo)  # also cancels a queued auto-merge without waiting for a tick
        if iids:
            bg(run)

    def set_dont_merge(dont):
        act_on(selected_prs(), lambda repo, number: mark(repo, number, dont))

    def mergeable_now():
        return [iid for iid in selected_prs() if verdicts.get(iid) not in ("don't merge", "merge conflict")]

    def merge_now():
        # The PRs as shown when asked, so a push after that makes GitHub refuse (--match-head-commit).
        chosen = {iid: prs[iid] for iid in mergeable_now()}
        lines = [f"#{pr['number']} {pr['title']} ({tree.set(iid, 'codex')})" for iid, pr in chosen.items()]
        mode = "admin" if live["mode"] == "admin" else "force"
        note = "This bypasses branch protection." if mode == "admin" else "Branch protection still applies."
        if messagebox.askyesno("Merge now?", "Squash-merge without waiting for Codex?\n\n" + "\n".join(lines) + f"\n\n{note}",
                               icon="warning", default="no", parent=root):
            act_on(list(chosen), lambda repo, number: merge(repo, chosen[f"{repo}#{number}"], mode))

    def open_selected():
        for iid in tree.selection():
            repo, _, number = iid.partition("#")
            bg(lambda iid=iid, repo=repo, number=number: log(f"{iid}: {browse(repo, number)}"))

    def copy_links():
        root.clipboard_clear()
        root.clipboard_append("\n".join(f"https://{iid.replace('#', '/pull/')}" for iid in tree.selection()))

    def refresh_selected():
        for repo in selected_repos():
            bg(refresh, repo)

    def poll_all(snapshot):
        try:
            for repo in snapshot:
                events.put(("prs", repo, poll(repo, log, lambda repo=repo: live["mode"] if live["merging"] and repo in repos else None)))
        except Exception as e:  # e.g. gh missing; log it rather than kill the thread silently
            log(f"poll crashed; retrying next interval ({e!r})")
        finally:
            events.put(("done",))  # else busy stays set and polling stops for good

    def poll_now():
        nonlocal busy
        if repos and not busy:
            busy = True
            bg(poll_all, list(repos))

    def retheme():
        """Theme the window, again whenever the system appearance has changed since."""
        nonlocal dark
        now = appearance == "dark" or appearance == "system" and system_dark(root)
        if now != dark:
            dark = now
            theme(root, dark, appearance)

    def tick():
        retheme()
        poll_now()
        try:
            seconds = max(1, interval_var.get())
        except tk.TclError:  # spinbox holds non-numeric text
            seconds = interval
        root.after(seconds * 1000, tick)

    def drain():
        nonlocal busy
        while not events.empty():
            kind, *rest = events.get()
            if kind == "log":
                logbox.configure(state="normal")
                logbox.insert("end", rest[0] + "\n")
                if int(logbox.index("end-1c").split(".")[0]) > 5000:
                    logbox.delete("1.0", "1001.0")
                logbox.see("end")
                logbox.configure(state="disabled")
            elif kind == "repo" and rest[0] not in repos:
                repos.append(rest[0])
                tree.insert("", "end", iid=rest[0], text=rest[0], open=True)
                save()
            elif kind == "prs" and rest[1] is not None and tree.exists(rest[0]):
                repo, rows = rest
                selected = tree.selection()
                for iid in tree.get_children(repo):
                    prs.pop(iid, None)
                    verdicts.pop(iid, None)
                tree.delete(*tree.get_children(repo))
                for pr, v in rows:
                    iid = f"{repo}#{pr['number']}"
                    prs[iid] = pr
                    verdicts[iid] = v
                    tree.insert(repo, "end", iid=iid, text=f"#{pr['number']} {pr['title']}", values=row_values(pr, v))
                tree.selection_set([i for i in selected if tree.exists(i)])
            elif kind == "done":
                busy = False
        root.after(200, drain)

    bar = ttk.Frame(root, padding=6)
    bar.pack(fill="x")
    entry = ttk.Entry(bar, width=36)
    entry.pack(side="left", padx=(0, 6))
    entry.bind("<Return>", add)
    ttk.Button(bar, text="Add repo", command=add).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text="Remove repo", command=remove).pack(side="left", padx=(0, 6))
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
    for label, value in MODES:
        ttk.Radiobutton(bar, text=label, variable=mode_var, value=value, style="Toolbutton",
                        command=lambda: control(mode=mode_var.get())).pack(side="left")
    ttk.Label(bar, text="  every").pack(side="left")
    ttk.Spinbox(bar, from_=1, to=86400, width=6, textvariable=interval_var).pack(side="left")
    ttk.Label(bar, text="s").pack(side="left")
    ttk.Checkbutton(bar, text="Auto-merge", variable=merging,
                    command=lambda: control(merging=merging.get())).pack(side="left", padx=6)

    tree = ttk.Treeview(root, columns=("codex", "author", "state"), height=12)
    tree.heading("#0", text="Repo / pull request", anchor="w")
    tree.heading("codex", text="Codex", anchor="w")
    tree.heading("author", text="Author", anchor="w")
    tree.heading("state", text="State", anchor="w")
    tree.column("#0", width=440)
    tree.column("codex", width=130)
    tree.column("author", width=120)
    tree.column("state", width=70)
    tree.pack(fill="both", expand=True, padx=6)

    actions = ttk.Frame(root, padding=6)
    actions.pack(fill="x")
    ttk.Button(actions, text="Request Codex review", command=ask_codex).pack(side="left", padx=(0, 6))
    ttk.Button(actions, text="Don't merge", command=lambda: set_dont_merge(True)).pack(side="left", padx=(0, 6))
    ttk.Button(actions, text="Allow merge", command=lambda: set_dont_merge(False)).pack(side="left", padx=(0, 6))
    ttk.Button(actions, text="Poll now", command=poll_now, style="Primary.TButton").pack(side="left", padx=(0, 6))

    pr_menu = tk.Menu(root, tearoff=False)
    pr_menu.add_command(label="Open in browser", command=open_selected)
    pr_menu.add_command(label="Copy link", command=copy_links)
    pr_menu.add_separator()
    pr_menu.add_command(label="Merge now…", command=merge_now)
    pr_menu.add_command(label="Don't merge", command=lambda: set_dont_merge(True))
    pr_menu.add_command(label="Allow merge", command=lambda: set_dont_merge(False))
    pr_menu.add_separator()
    pr_menu.add_command(label="Update branch", command=lambda: act_on(selected_prs(), update_branch))
    pr_menu.add_command(label="Request Codex review", command=ask_codex)

    repo_menu = tk.Menu(root, tearoff=False)
    repo_menu.add_command(label="Open in browser", command=open_selected)
    repo_menu.add_command(label="Copy link", command=copy_links)
    repo_menu.add_command(label="Refresh", command=refresh_selected)
    repo_menu.add_separator()
    repo_menu.add_command(label="Remove repo", command=remove)

    def context_menu(event):
        row = tree.identify_row(event.y)
        if not row:
            return None
        if row not in tree.selection():
            tree.selection_set(row)
        if "#" not in row:
            repo_menu.tk_popup(event.x_root, event.y_root)
            return "break"
        marked = [verdicts.get(iid) == "don't merge" for iid in selected_prs()]
        pr_menu.entryconfigure("Merge now…", state="normal" if mergeable_now() else "disabled")
        pr_menu.entryconfigure("Don't merge", state="disabled" if all(marked) else "normal")
        pr_menu.entryconfigure("Allow merge", state="normal" if any(marked) else "disabled")
        pr_menu.tk_popup(event.x_root, event.y_root)
        return "break"  # else Control-click also toggles the row's selection

    # A repo row keeps double-click for expanding and collapsing.
    tree.bind("<Double-Button-1>", lambda event: open_selected() if "#" in tree.identify_row(event.y) else None)

    # Right-click is Button-3, except on macOS Tk 8.6, which calls it Button-2.
    for gesture in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
        tree.bind(gesture, context_menu)

    logbox = tk.Text(root, height=12, state="disabled", wrap="word")
    logbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def close():
        save()
        root.destroy()

    for repo in saved["repos"]:
        bg(show, repo)  # already resolved; re-resolving offline would drop it from the next save
    for target in targets:
        bg(add_repo, target)
    root.protocol("WM_DELETE_WINDOW", close)
    retheme()
    drain()
    root.after(interval * 1000, tick)
    root.mainloop()


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
