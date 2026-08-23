# gh-auto-merge-codex

Watch a GitHub repo's open PRs and squash-merge each one that Codex has 👍'd.

Codex (`chatgpt-codex-connector`) signals "reviewed, no findings" with a `+1`
reaction on the PR body — not a review or a comment — so status checks and
review APIs never see it. This script polls for that reaction and merges.

## Requirements

`gh` ([GitHub CLI](https://cli.github.com)), authenticated with merge rights on the repo.

## Usage

```bash
./codex-automerge.sh [--force|--admin] [-R|--repo <directory|owner/repo>] [interval_seconds]
```

```bash
./codex-automerge.sh --repo ../checkout  # local checkout; relative paths work
```

```bash
./codex-automerge.sh --repo owner/repo   # monitor a GitHub repository directly
```

```bash
REPO=owner/name ./codex-automerge.sh
```

## Options

| Option | Description |
| --- | --- |
| `-R`, `--repo <directory\|owner/repo>` | Monitor a local Git checkout or a GitHub repository reference. This overrides `REPO`. |
| `--force` | Merge without requesting auto-merge. This does not bypass branch protection. |
| `--admin` | Merge with administrator privileges and bypass branch protection. |
| `-h`, `--help` | Print command usage. |
| `interval_seconds` | Polling interval in seconds. Defaults to `60`. |

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `REPO` | Current directory's repository | GitHub repository to monitor when `--repo` is not set. |
| `BOT` | `chatgpt-codex-connector` | GitHub login whose 👍 reaction authorizes a merge. |

`BOT` is a prefix match because the reactions API reports the login with a
`[bot]` suffix — `chatgpt-codex-connector` matches
`chatgpt-codex-connector[bot]`.

## Hivemind

The included `Procfile` is for [Hivemind](https://github.com/darthsim/hivemind), a process manager for Procfile-based applications. Replace `owner/repo` with the repository to monitor, then run:

```sh
hivemind
```

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

## Check

```sh
bash test_cli_target.sh
```

## Caveat

A 👍 from Codex means Codex found nothing — it says nothing about other
reviewers or human approval. Point this at repos where that is the bar you want.
