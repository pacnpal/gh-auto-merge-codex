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

`BOT=<login>` overrides the reviewer login (matched exactly) if you want to key
off a different bot.

## Behavior

Every interval it lists open PRs, checks each for a `+1` from the bot, and runs
`gh pr merge --squash --auto`, queuing the merge behind the repo's required
checks.

Where auto-merge isn't allowed on the repo, `gh` rejects that, and the script
retries the merge without `--auto`. The fallback is gated on that specific
error, so a conflict or a failing required check still logs `MERGE FAILED`
rather than merging.

> [!NOTE]
> On the fallback path the merge happens **immediately**, not after CI. If the
> repo has no branch protection requiring checks, a PR can merge with CI still
> running or red. Enable auto-merge, or protect the branch, if that matters.

Runs until you stop it. Everything is logged with timestamps.

## Caveat

A 👍 from Codex means Codex found nothing — it says nothing about other
reviewers or human approval. Point this at repos where that is the bar you want.
