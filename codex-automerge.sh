#!/usr/bin/env bash
# Watch a repo's open PRs; squash-merge each one Codex has 👍'd on the PR body.
#
#   ./codex-automerge.sh [interval_seconds]        # repo of the current directory
#   REPO=owner/name ./codex-automerge.sh [interval] # explicit override
#
# Codex (chatgpt-codex-connector) signals "reviewed, no findings" with a +1
# reaction on the PR body, not a review or comment.
set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
INTERVAL="${1:-60}"
BOT="${BOT:-chatgpt-codex-connector}"

log() { echo "$(date '+%F %T') $*"; }

log "watching $REPO every ${INTERVAL}s for a 👍 from $BOT"

while :; do
  # ponytail: no seen-file — merged PRs drop out of this list on their own.
  prs=$(gh pr list -R "$REPO" --state open --json number --jq '.[].number')
  log "poll: $(echo "$prs" | grep -c '[0-9]') open PR(s)"

  for pr in $prs; do
    # ponytail: per_page=100 over --paginate — --jq runs per page, which would
    # split the joined list across lines and break the case match below.
    reactions=$(gh api "repos/$REPO/issues/$pr/reactions?per_page=100" \
      -H "Accept: application/vnd.github.squirrel-girl-preview+json" \
      --jq "[.[] | select(.user.login == \"$BOT\") | .content] | join(\",\")")

    case ",$reactions," in
      *,+1,*) ;;
      *) log "  #$pr: no 👍 yet (codex reactions: ${reactions:-none})"; continue ;;
    esac

    log "  #$pr: 👍 — squash merging"
    # --auto queues behind required checks; needs auto-merge allowed on the repo.
    if gh pr merge -R "$REPO" "$pr" --squash --auto; then
      log "  #$pr: merge requested"
    else
      log "  #$pr: MERGE FAILED"
    fi
  done
  sleep "$INTERVAL"
done
