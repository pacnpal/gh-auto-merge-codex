# gh-auto-merge-codex

Watch a GitHub repo's open PRs and squash-merge each one that Codex has 👍'd.

Codex (`chatgpt-codex-connector`) signals "reviewed, no findings" with a `+1`
reaction on the PR body — not a review or a comment — so status checks and
review APIs never see it. This script polls for that reaction and merges.

## Requirements

`gh` ([GitHub CLI](https://cli.github.com)), authenticated with merge rights on the repo.

## Usage

```bash
./codex-automerge.sh          # repo of the current directory, poll every 60s
```

```bash
./codex-automerge.sh 300      # poll every 5 minutes
```

```bash
REPO=owner/name ./codex-automerge.sh
```

```bash
./codex-automerge.sh --force  # merge now, don't queue with --auto
```

```bash
./codex-automerge.sh --admin  # merge now, bypassing branch protection
```

`BOT=<login>` overrides the reviewer login if you want to key off a different
bot. It matches two exact logins — the value itself and its `[bot]` form, since
the reactions API reports GitHub Apps as `chatgpt-codex-connector[bot]`. Not a
prefix match: on a public repo, anyone could register a login starting with
`chatgpt-codex-connector` and 👍 their own PR.

## Behavior

Every interval it lists open PRs, checks each for a `+1` from the bot, and runs
`gh pr merge --squash --auto`, queuing the merge behind the repo's required
checks.

Where auto-merge isn't allowed on the repo, `gh` rejects that, and the script
retries the merge without `--auto`. The fallback is gated on that specific
error, so a conflict or a failing required check still logs `MERGE FAILED`
rather than merging.

`--force` skips `--auto` and merges immediately. It does **not** bypass branch
protection: on a repo with required checks, GitHub rejects the merge until they
are green, so a 👍'd PR logs `MERGE FAILED` each poll and merges once it is
mergeable.

On a branch with a **merge queue**, `--force` cannot merge now at all: with
required checks pending, `gh pr merge` enables auto-merge even without `--auto`.
The script does not claim otherwise — it only logs `merged now` when `gh` says
`Merged pull request`, and logs `not merged` plus `gh`'s own message otherwise.
Only `--admin` bypasses a merge queue.

`--admin` is `--force` plus `gh pr merge --admin`, which bypasses branch
protection outright — required checks, required reviews, merge queue, and all.
It needs admin rights on the repo.

The two are mutually exclusive; the last one wins (`--admin --force` runs as
`--force`). Any other flag, or a non-numeric interval, exits with usage.

> [!NOTE]
> On the fallback, `--force`, and `--admin` paths the merge happens
> **immediately**, not after CI. With `--admin` a 👍 from Codex is the *only*
> gate: PRs merge with CI red, CI still running, or other reviews outstanding.

Runs until you stop it. Everything is logged with timestamps.

## Caveat

A 👍 from Codex means Codex found nothing — it says nothing about other
reviewers or human approval. Point this at repos where that is the bar you want.
