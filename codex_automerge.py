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
import subprocess
import sys
import threading
import time

BOT = os.environ.get("BOT", "chatgpt-codex-connector")
# Only Codex's review receipt, from its first byte: a free-text Codex reply can't start with this.
CLEAN = re.compile(r"\s*<!-- codex-review-receipt:v1 -->\s*Codex Review: Didn.t find any major issues")  # . matches ' and ’
REVIEWED = re.compile(r"Reviewed commit:\W*([0-9a-f]{10,40})")
MODES = (("Queue (--auto)", ""), ("Force", "force"), ("Admin", "admin"))


def gh(*args, cwd=None):
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
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
    return (item.get("user") or {}).get("login") in (BOT, BOT + "[bot]")


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


def poll(repo, mode, log, merging=True):
    """One pass over repo's open PRs. Returns [(pr, verdict)], or None if listing failed."""
    try:
        # shortcut: PRs past the 1000th open one are never seen; page through the API if a repo gets there.
        prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "1000",
                      "--json", "number,title,headRefOid")
    except (RuntimeError, ValueError) as e:
        log(f"{repo}: failed to list open PRs; retrying ({e})")
        return None
    log(f"{repo}: {len(prs)} open PR(s)")
    rows = []
    for pr in prs:
        tag = f"  {repo}#{pr['number']}"
        try:
            v = verdict(repo, pr)
        except (RuntimeError, ValueError) as e:
            log(f"{tag}: failed to fetch Codex reactions/comments; retrying ({e})")
            rows.append((pr, "?"))
            continue
        rows.append((pr, v))
        if not v:
            log(f"{tag}: no 👍 or clean review yet")
        elif merging:
            log(f"{tag}: {v} — squash merging")
            log(f"{tag}: {merge(repo, pr, mode)}")
    return rows


def stamp(msg):
    return f"{time.strftime('%F %T')} {msg}"


def watch(repos, mode, interval):
    log = lambda msg: print(stamp(msg), flush=True)
    log(f"watching {', '.join(repos)} every {interval}s for a go-ahead from {BOT}")
    while True:
        for repo in repos:
            poll(repo, mode, log)
        time.sleep(interval)


def gui(targets, mode, interval):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Codex automerge")
    events = queue.Queue()  # worker threads never touch tk; they post here
    repos = []
    busy = False
    mode_var = tk.StringVar(value=mode)
    interval_var = tk.IntVar(value=interval)
    merging = tk.BooleanVar(value=True)

    def log(msg):
        events.put(("log", stamp(msg)))

    def bg(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def add_repo(target):
        try:
            repo = resolve(target)
        except Exception as e:
            return log(f"can't add {target}: {e!r}")
        events.put(("repo", repo))
        try:
            events.put(("prs", repo, poll(repo, "", log, merging=False)))  # show it now; merge on the next tick
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

    def ask_codex():
        for iid in tree.selection():
            if "#" in iid:
                repo, number = iid.rsplit("#", 1)
                bg(lambda repo=repo, number=number: log(f"{repo}#{number}: {request_review(repo, number)}"))

    def poll_all(snapshot, mode, merge_on):
        try:
            for repo in snapshot:
                events.put(("prs", repo, poll(repo, mode, log, merge_on)))
        except Exception as e:  # e.g. gh missing; log it rather than kill the thread silently
            log(f"poll crashed; retrying next interval ({e!r})")
        finally:
            events.put(("done",))  # else busy stays set and polling stops for good

    def poll_now():
        nonlocal busy
        if repos and not busy:
            busy = True
            bg(poll_all, list(repos), mode_var.get(), merging.get())

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
        ttk.Radiobutton(bar, text=label, variable=mode_var, value=value).pack(side="left")
    ttk.Label(bar, text="  every").pack(side="left")
    ttk.Spinbox(bar, from_=1, to=86400, width=6, textvariable=interval_var).pack(side="left")
    ttk.Label(bar, text="s").pack(side="left")
    ttk.Checkbutton(bar, text="Auto-merge", variable=merging).pack(side="left", padx=6)

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

    for target in targets:
        bg(add_repo, target)
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
    p.add_argument("--force", dest="mode", action="store_const", const="force", default="",
                   help="merge now instead of queuing with --auto")
    p.add_argument("--admin", dest="mode", action="store_const", const="admin",
                   help="merge now and bypass branch protection")
    p.add_argument("--interval", type=positive, default=60, help="seconds between polls (default 60)")
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
    watch(repos, a.mode, a.interval)


if __name__ == "__main__":
    main()
