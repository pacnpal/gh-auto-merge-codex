# gh-auto-merge-codex

Watch GitHub repos' open PRs and squash-merge each one Codex has cleared.

Codex (`chatgpt-codex-connector`) signals "reviewed, no findings" in one of two
ways, neither of which is a review or a status check:

- a `+1` reaction on the PR body, or
- its review receipt saying **"Codex Review: Didn't find any major issues"**.
  This counts only when the receipt's `Reviewed commit:` matches the PR's
  current head, so a clean review of an older commit can't merge newer pushes.
  A free-text Codex reply containing the same words doesn't count.

Both must be newer than the PR's current commit. The commit's arrival time is
the later of its newest check suite and the PR's latest force-push, both
timestamped by GitHub (git dates can be faked). A 👍 names no commit and stays
on the PR through pushes. If neither timestamp exists, nothing merges. A check
suite added after Codex's go-ahead (say, a re-triggered workflow) also voids
it. Request a new Codex review to get a fresh one.

## Requirements

Python 3 with tkinter (the python.org macOS installer includes it), plus
`gh` ([GitHub CLI](https://cli.github.com)) 2.48 or newer, authenticated with merge rights on the repos.

## Usage

```bash
./codex_automerge.py [--force|--admin] [--interval N] [--headless] [repo ...]
```

```bash
./codex_automerge.py                                  # open the window, add repos there
./codex_automerge.py owner/a owner/b ../checkout     # open the window with repos preloaded
./codex_automerge.py --headless --admin owner/a      # no window; log to stdout
```

A `repo` is `owner/repo` or a local checkout directory (GitHub Enterprise hosts
work too).

### Window

- **Add repo / Remove repo**: type `owner/repo` or a path and press Enter. You
  can monitor any number of repos at once.
- **Queue (--auto) / Force / Admin**: the merge mode (see Behavior).
- **every N s**: the polling interval.
- **Auto-merge**: untick it to watch without merging. Conflict comments are still posted.
- **Request Codex review**: posts an `@codex review` comment on each selected PR.
- **Poll now**: poll immediately.

The tree lists each repo's open PRs and Codex's verdict on each: `👍`,
`no major issues`, `merge conflict`, `—` (none yet), or `?` (lookup failed). A newly added repo is
shown right away. Merging starts on the next poll.

The window saves its repo list, merge mode, interval, Auto-merge setting, and
size to `$XDG_CONFIG_HOME/codex-automerge.json` (default
`~/.config/codex-automerge.json`) whenever one changes and when you close it,
and restores them at the next launch. `repo` arguments are added to the saved
list. `--force`, `--admin`, and `--interval` override the saved values. If the
file can't be parsed, the tool exits instead of overwriting it. `--headless`
neither reads nor writes this file.

## Options

| Option | Description |
| --- | --- |
| `repo ...` | Repos to monitor at startup. More can be added in the window. |
| `--force` | Merge without requesting auto-merge. This does not bypass branch protection. |
| `--admin` | Merge with administrator privileges and bypass branch protection. |
| `--interval N` | Positive polling interval in seconds. Defaults to `60`, or to the saved interval in the window. |
| `--headless` | Run without a window, logging to stdout. With no `repo`, uses `REPO` or the current directory's repository. |
| `-h`, `--help` | Print command usage. |

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `REPO` | Current directory's repository | Repo for `--headless` when no `repo` is given. |
| `XDG_CONFIG_HOME` | `~/.config` | Directory holding the window's saved settings. |
| `BOT_ID` | `199175422` | GitHub user id whose 👍 or clean comment authorizes a merge. |

`199175422` is `chatgpt-codex-connector[bot]`. The tool matches the numeric id,
not the login, so no other account can pass as Codex.

## Hivemind

The included `Procfile` is for [Hivemind](https://github.com/darthsim/hivemind), a process manager for Procfile-based applications. Replace `owner/repo` with the repositories to monitor (space-separated), then run:

```sh
hivemind
```

## Behavior

Every interval it lists each repo's open PRs, checks each one for Codex's
go-ahead, and runs `gh pr merge --squash --auto --match-head-commit <head>`,
which queues the merge behind the repo's required checks. `--match-head-commit`
makes GitHub refuse the merge if someone pushed after the poll.

A PR GitHub reports as conflicting with its base isn't checked or merged.
Instead the tool comments on it once per head commit, even with merging off, saying it has merge
conflicts with the base branch. A hidden marker in that comment keeps it from
commenting again, including after a restart. Only the marker in a comment
from the account `gh` is logged in as counts. GitHub works out whether a PR
conflicts in the background, so a fresh push can take one more poll to be
flagged.

Where auto-merge isn't allowed on the repo, `gh` rejects that, and it
retries the merge without `--auto`. The fallback is gated on that specific
error, so a conflict or a failing required check still logs `MERGE FAILED`
rather than merging.

`--force` skips `--auto` and merges immediately. It does **not** bypass branch
protection: on a repo with required checks, GitHub rejects the merge until they
are green, so a 👍'd PR logs `MERGE FAILED` each poll and merges once it is
mergeable.

On a branch with a **merge queue**, `--force` cannot merge now at all: with
required checks pending, `gh pr merge` enables auto-merge even without `--auto`.
The tool does not claim otherwise — it only logs `merged now` when `gh` says
`Merged pull request`, and logs `not merged` plus `gh`'s own message otherwise.
Only `--admin` bypasses a merge queue.

`--admin` is `--force` plus `gh pr merge --admin`, which bypasses branch
protection outright — required checks, required reviews, merge queue, and all.
It needs admin rights on the repo.

The two are mutually exclusive; the last one wins (`--admin --force` runs as
`--force`). Any other flag, a non-numeric interval, or `0` exits with usage.

> [!NOTE]
> On the fallback, `--force`, and `--admin` paths the merge happens
> **immediately**, not after CI. With `--admin` Codex's go-ahead is the *only*
> gate: PRs merge with CI red, CI still running, or other reviews outstanding.

Runs until you stop it. Everything is logged with timestamps.
If listing PRs or reading Codex's reactions or comments fails, it logs the failure and retries on
the next interval.

## Check

```sh
python3 -m unittest test_codex_automerge
```

## Caveat

A 👍 or clean comment from Codex means Codex found nothing — it says nothing about other
reviewers or human approval. Point this at repos where that is the bar you want.
