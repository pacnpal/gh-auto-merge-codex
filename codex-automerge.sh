#!/usr/bin/env bash
# Watch a repo's open PRs; squash-merge each one Codex has 👍'd on the PR body.
#
#   ./codex-automerge.sh [--force|--admin] [interval_seconds]  # repo of the cwd
#   REPO=owner/name ./codex-automerge.sh [--force|--admin] [interval]  # override
#
# --force merges immediately instead of queuing with --auto.
# --admin does the same and bypasses branch protection (gh pr merge --admin).
#
# Codex (chatgpt-codex-connector) signals "reviewed, no findings" with a +1
# reaction on the PR body, not a review or comment.
set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
BOT="${BOT:-chatgpt-codex-connector}"
FORCE=""
ADMIN=""
INTERVAL=60
for arg in "$@"; do
  case "$arg" in
    # ponytail: each mode sets both vars — last flag wins outright, so
    # `--admin --force` downgrades instead of silently keeping --admin.
    --force) FORCE="--force"; ADMIN="" ;;
    --admin) FORCE="--admin"; ADMIN="--admin" ;;
    # ponytail: one arm rejects both typo'd flags and a non-numeric interval.
    ''|*[!0-9]*) echo "usage: $0 [--force|--admin] [interval_seconds]" >&2; exit 2 ;;
    *) INTERVAL="$arg" ;;
  esac
done

log() { echo "$(date '+%F %T') $*"; }

log "watching $REPO every ${INTERVAL}s for a 👍 from $BOT"

while :; do
  # ponytail: no seen-file — merged PRs drop out of this list on their own.
  prs=$(gh pr list -R "$REPO" --state open --json number --jq '.[].number')
  log "poll: $(echo "$prs" | grep -c '[0-9]') open PR(s)"

  for pr in $prs; do
    # ponytail: per_page=100 over --paginate — --jq runs per page, which would
    # split the joined list across lines and break the case match below.
    # ponytail: startswith, not == — the API reports the login as
    # "chatgpt-codex-connector[bot]", so an exact match never fires.
    reactions=$(gh api "repos/$REPO/issues/$pr/reactions?per_page=100" \
      -H "Accept: application/vnd.github.squirrel-girl-preview+json" \
      --jq "[.[] | select(.user.login | startswith(\"$BOT\")) | .content] | join(\",\")")

    case ",$reactions," in
      *,+1,*) ;;
      *) log "  #$pr: no 👍 yet (codex reactions: ${reactions:-none})"; continue ;;
    esac

    log "  #$pr: 👍 — squash merging"
    # --auto queues behind required checks, but repos without auto-merge allowed
    # reject it. Fall back to merging now — gated on that specific error, so a
    # conflict or a failing required check still stops us instead of merging.
    if [[ -z "$FORCE" ]] && err=$(gh pr merge -R "$REPO" "$pr" --squash --auto 2>&1); then
      log "  #$pr: merge queued behind required checks"
    # ponytail: || order is load-bearing — under --force $err is unset, so the
    # grep must never run. ${ADMIN} is unquoted on purpose: one flag or nothing.
    elif { [[ -n "$FORCE" ]] || grep -qiE 'auto.?merge' <<<"$err"; } \
      && err=$(gh pr merge -R "$REPO" "$pr" --squash ${ADMIN} 2>&1); then
      # Without --auto, gh still enables auto-merge (or the merge queue) when
      # required checks are pending — so a success here is not proof of a merge.
      # Trust gh's own wording rather than claiming one.
      if grep -qi 'merged pull request' <<<"$err"; then
        log "  #$pr: merged now (${FORCE:-auto-merge unavailable})"
      else
        log "  #$pr: not merged — ${err//$'\n'/ }"
      fi
    else
      log "  #$pr: MERGE FAILED: ${err//$'\n'/ }"
    fi
  done
  sleep "$INTERVAL"
done
