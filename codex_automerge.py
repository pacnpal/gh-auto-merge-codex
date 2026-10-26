#!/usr/bin/env python3
"""Watch GitHub repos' open PRs; squash-merge each one Codex has cleared.

  ./codex_automerge.py [--force|--admin] [--interval N] [--headless] [repo ...]

Codex (chatgpt-codex-connector) says "reviewed, no findings" with a +1 reaction
on the PR body, or with a "Didn't find any major issues" comment naming the
reviewed commit. Neither is a review or a status check.

--force merges immediately instead of queuing with --auto.
--admin does the same and bypasses branch protection (gh pr merge --admin).
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
CONFLICT = "<!-- codex-automerge:conflict {} -->"
MODES = (("Queue (--auto)", ""), ("Force", "force"), ("Admin", "admin"))
# The window's repos and controls, restored at launch.
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "codex-automerge.json")
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
    """How Codex cleared this PR's current head, or None."""
    host, nwo = repo.split("/", 1)
    issue = f"repos/{nwo}/issues/{pr['number']}"
    pushed = pushed_at(host, nwo, pr)
    if not pushed:  # can't tell when this head arrived, so no go-ahead can be tied to it
        return None
    # A 👍 names no commit and survives pushes, so it only counts if it came after this head arrived.
    if any(is_bot(r) and r["content"] == "+1" and r["created_at"] > pushed
           for r in api(host, f"{issue}/reactions?per_page=100")):
        return "👍"
    for c in api(host, f"{issue}/comments?per_page=100"):
        body = c.get("body") or ""
        m = is_bot(c) and CLEAN.match(body) and REVIEWED.search(body)
        # Counts only for the commit it reviewed, and only if that head didn't arrive later
        # (a pushed commit crafted to share the short hash).
        if m and pr["headRefOid"].startswith(m[1]) and c["created_at"] > pushed:
            return "no major issues"
    return None


def merge(repo, pr, mode):
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
    Conflicting PRs are commented on either way.
    """
    try:
        # shortcut: PRs past the 1000th open one are never seen; page through the API if a repo gets there.
        prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "1000",
                      "--json", "number,title,headRefOid,mergeable,baseRefName")
    except (RuntimeError, ValueError) as e:
        log(f"{repo}: failed to list open PRs; retrying ({e})")
        return None
    log(f"{repo}: {len(prs)} open PR(s)")
    rows = []
    for pr in prs:
        tag = f"  {repo}#{pr['number']}"
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
        if not v:
            log(f"{tag}: no 👍 or clean review yet")
        elif (mode := merge_mode()) is not None:
            log(f"{tag}: {v} — squash merging")
            log(f"{tag}: {merge(repo, pr, mode)}")
    return rows


def load_settings():
    """Saved window settings, each checked: a bad mode would otherwise merge() as force."""
    try:
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
    }


def save_settings(settings):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = f"{CONFIG}.{os.getpid()}.tmp"  # per process: two open windows mustn't share one tmp file
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, CONFIG)  # a crash mid-write leaves the old file whole


def stamp(msg):
    return f"{time.strftime('%F %T')} {msg}"


def watch(repos, mode, interval):
    log = lambda msg: print(stamp(msg), flush=True)
    log(f"watching {', '.join(repos)} every {interval}s for a go-ahead from bot id {BOT_ID}")
    while True:
        for repo in repos:
            poll(repo, log, lambda: mode)
        time.sleep(interval)


def gui(targets, mode, interval):
    import tkinter as tk
    from tkinter import ttk

    saved = load_settings()
    mode = saved["mode"] if mode is None else mode  # a CLI flag wins for this run
    interval = interval or saved["interval"]
    root = tk.Tk()
    root.title("Codex automerge")
    if saved["geometry"]:
        root.geometry(saved["geometry"])
    events = queue.Queue()  # worker threads never touch tk; they post here
    repos = []
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
                           "merging": merging.get(), "geometry": root.geometry()})
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

    def show(repo):
        events.put(("repo", repo))
        try:
            events.put(("prs", repo, poll(repo, log, lambda: None)))  # show it now; merge on the next tick
        except Exception as e:
            log(f"{repo}: poll crashed; retrying next interval ({e!r})")

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

    def tick():
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
                tree.delete(*tree.get_children(repo))
                for pr, v in rows:
                    tree.insert(repo, "end", iid=f"{repo}#{pr['number']}",
                                text=f"#{pr['number']} {pr['title']}", values=(v or "—",))
                tree.selection_set([i for i in selected if tree.exists(i)])
            elif kind == "done":
                busy = False
        root.after(200, drain)

    bar = ttk.Frame(root, padding=6)
    bar.pack(fill="x")
    entry = ttk.Entry(bar, width=36)
    entry.pack(side="left")
    entry.bind("<Return>", add)
    ttk.Button(bar, text="Add repo", command=add).pack(side="left")
    ttk.Button(bar, text="Remove repo", command=remove).pack(side="left")
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
    for label, value in MODES:
        ttk.Radiobutton(bar, text=label, variable=mode_var, value=value,
                        command=lambda: control(mode=mode_var.get())).pack(side="left")
    ttk.Label(bar, text="  every").pack(side="left")
    ttk.Spinbox(bar, from_=1, to=86400, width=6, textvariable=interval_var).pack(side="left")
    ttk.Label(bar, text="s").pack(side="left")
    ttk.Checkbutton(bar, text="Auto-merge", variable=merging,
                    command=lambda: control(merging=merging.get())).pack(side="left", padx=6)

    tree = ttk.Treeview(root, columns=("codex",), height=12)
    tree.heading("#0", text="Repo / pull request", anchor="w")
    tree.heading("codex", text="Codex", anchor="w")
    tree.column("#0", width=560)
    tree.column("codex", width=140)
    tree.pack(fill="both", expand=True, padx=6)

    actions = ttk.Frame(root, padding=6)
    actions.pack(fill="x")
    ttk.Button(actions, text="Request Codex review", command=ask_codex).pack(side="left")
    ttk.Button(actions, text="Poll now", command=poll_now).pack(side="left")

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
