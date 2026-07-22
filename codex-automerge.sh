#!/usr/bin/env bash
# Watch a repo's open PRs; squash-merge each one Codex has 👍'd on the PR body.
#
#   REPO=owner/name ./codex-automerge.sh [interval_seconds]
#
# Codex (chatgpt-codex-connector) signals "reviewed, no findings" with a +1
# reaction on the PR body, not a review or comment.
set -euo pipefail

REPO="${REPO:?set REPO=owner/name}"
INTERVAL="${1:-60}"
BOT="${BOT:-chatgpt-codex-connector}"

while :; do
  # ponytail: no seen-file — merged PRs drop out of this list on their own.
  for pr in $(gh pr list -R "$REPO" --state open --json number --jq '.[].number'); do
    thumbs=$(gh api "repos/$REPO/issues/$pr/reactions" \
      -H "Accept: application/vnd.github.squirrel-girl-preview+json" \
      --jq "[.[] | select(.user.login | test(\"$BOT\")) | select(.content == \"+1\")] | length")
    [ "$thumbs" -gt 0 ] || continue

    echo "$(date '+%F %T') PR #$pr: $BOT 👍 — merging"
    # --auto queues behind required checks on repos with auto-merge enabled;
    # merges immediately on repos without it.
    gh pr merge -R "$REPO" "$pr" --squash --auto || echo "  merge failed for #$pr"
  done
  sleep "$INTERVAL"
done
