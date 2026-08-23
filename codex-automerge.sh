#!/usr/bin/env bash
# Watch a repo's open PRs; squash-merge each one Codex has 👍'd on the PR body.
#
#   ./codex-automerge.sh [interval_seconds]                  # repo of the current directory
#   ./codex-automerge.sh --repo ../checkout [interval]       # local checkout (relative is fine)
#   ./codex-automerge.sh --repo owner/name [interval]        # GitHub repository
#   ./codex-automerge.sh --admin --repo owner/name [interval] # bypass merge requirements
#   REPO=owner/name ./codex-automerge.sh [interval]          # explicit override
#
# Codex (chatgpt-codex-connector) signals "reviewed, no findings" with a +1
# reaction on the PR body, not a review or comment.
set -euo pipefail

usage() {
  echo "usage: $0 [--admin] [--repo <directory|owner/repo>] [interval_seconds]" >&2
  exit 2
}

repo_target=""
interval=""
admin=false
while (($#)); do
  case "$1" in
    --admin)
      admin=true
      shift
      ;;
    -R|--repo)
      (($# >= 2)) || usage
      repo_target="$2"
      shift 2
      ;;
    -h|--help) usage ;;
    *)
      [[ -z "$interval" ]] || usage
      interval="$1"
      shift
      ;;
  esac
done

if [[ -n "$repo_target" ]]; then
  if [[ -d "$repo_target" ]]; then
    REPO=$(gh -C "$repo_target" repo view --json nameWithOwner --jq .nameWithOwner)
  else
    REPO=$(gh repo view "$repo_target" --json nameWithOwner --jq .nameWithOwner)
  fi
else
  REPO="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
fi
INTERVAL="${interval:-60}"
BOT="${BOT:-chatgpt-codex-connector}"
merge_flags=(--squash --auto)
[[ "$admin" == true ]] && merge_flags=(--squash --admin)

log() { echo "$(date '+%F %T') $*"; }

log "watching $REPO every ${INTERVAL}s for a 👍 from $BOT"

while :; do
  # ponytail: no seen-file — merged PRs drop out of this list on their own.
  prs=$(gh pr list -R "$REPO" --state open --json number --jq '.[].number')
  log "poll: $(echo "$prs" | grep -c '[0-9]') open PR(s)"

  for pr in $prs; do
    reactions=$(gh api "repos/$REPO/issues/$pr/reactions" \
      -H "Accept: application/vnd.github.squirrel-girl-preview+json" \
      --jq "[.[] | select(.user.login | test(\"$BOT\")) | .content] | join(\",\")")

    case ",$reactions," in
      *,+1,*) ;;
      *) log "  #$pr: no 👍 yet (codex reactions: ${reactions:-none})"; continue ;;
    esac

    log "  #$pr: 👍 — squash merging"
    # --auto queues behind required checks on repos with auto-merge enabled;
    # merges immediately on repos without it.
    if gh pr merge -R "$REPO" "$pr" "${merge_flags[@]}"; then
      log "  #$pr: merge requested"
    else
      log "  #$pr: MERGE FAILED"
    fi
  done
  sleep "$INTERVAL"
done
