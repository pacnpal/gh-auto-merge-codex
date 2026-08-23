#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT
mkdir -p "$TMPDIR_TEST/bin" "$TMPDIR_TEST/local-repo"

cat >"$TMPDIR_TEST/bin/gh" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "repo" && "${2:-}" == "view" && "${PWD##*/}" == "local-repo" ]]; then
  echo "acme/local-repo"
elif [[ "${1:-}" == "repo" && "${2:-}" == "view" && "${3:-}" == "octo/direct-repo" ]]; then
  echo "octo/direct-repo"
elif [[ "${1:-}" == "repo" && "${2:-}" == "view" ]]; then
  echo "wrong/current-repo"
elif [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
  [[ -z "${MOCK_PR_LIST_FAIL:-}" ]] || exit 1
  [[ -n "${MOCK_PR:-}" ]] && echo "$MOCK_PR"
elif [[ "${1:-}" == "api" ]]; then
  [[ -z "${MOCK_API_FAIL:-}" ]] || exit 1
  bot="${BOT:-chatgpt-codex-connector}"
  jq_filter=""
  for ((i = 1; i <= $#; i++)); do
    case "${!i}" in
      --arg) ((i += 2)); bot="${!i}" ;;
      --jq) ((i += 1)); jq_filter="${!i}" ;;
    esac
  done
  login="${MOCK_REACTION_LOGIN:-chatgpt-codex-connector[bot]}"
  if [[ "$jq_filter" == *startswith* ]]; then
    [[ "$login" == "${BOT:-chatgpt-codex-connector}"* ]] && echo "+1"
  elif [[ "$jq_filter" == *'== $BOT'* ]] && [[ "$login" == "$bot" || "$login" == "$bot[bot]" ]]; then
    echo "+1"
  fi
elif [[ "${1:-}" == "pr" && "${2:-}" == "merge" ]]; then
  printf '%s\n' "$*" >"$GH_ARGS"
fi
EOF
cat >"$TMPDIR_TEST/bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$TMPDIR_TEST/bin/gh" "$TMPDIR_TEST/bin/sleep"

run() {
  (cd "$TMPDIR_TEST" && env -u REPO PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" "$@" 2>&1) || true
}

local_output=$(run --repo local-repo 1)
[[ "$local_output" == *"watching acme/local-repo every 1s"* ]]

github_output=$(run --repo octo/direct-repo 1)
[[ "$github_output" == *"watching octo/direct-repo every 1s"* ]]

zero_output=$(run --repo octo/direct-repo 0)
[[ "$zero_output" == *"usage:"* ]]
[[ "$zero_output" != *"watching"* ]]

unknown_option_output=$(run --unknown)
[[ "$unknown_option_output" == *"usage:"* ]]
[[ "$unknown_option_output" != *"watching"* ]]

admin_args_file="$TMPDIR_TEST/admin-args"
(cd "$TMPDIR_TEST" && env -u REPO MOCK_PR=7 GH_ARGS="$admin_args_file" PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" --admin --repo octo/direct-repo 1 >/dev/null 2>&1) || true
admin_args=$(<"$admin_args_file")
[[ "$admin_args" == *"--admin"* ]]
[[ "$admin_args" != *"--auto"* ]]

force_args_file="$TMPDIR_TEST/force-args"
(cd "$TMPDIR_TEST" && env -u REPO MOCK_PR=7 GH_ARGS="$force_args_file" PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" --force --repo octo/direct-repo 1 >/dev/null 2>&1) || true
force_args=$(<"$force_args_file")
[[ "$force_args" != *"--admin"* ]]
[[ "$force_args" != *"--auto"* ]]

wrong_bot_args_file="$TMPDIR_TEST/wrong-bot-args"
wrong_bot_output=$(cd "$TMPDIR_TEST" && env -u REPO BOT=codex MOCK_PR=7 MOCK_REACTION_LOGIN=codex-impersonator GH_ARGS="$wrong_bot_args_file" PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" --force --repo octo/direct-repo 1 2>&1 || true)
[[ "$wrong_bot_output" == *"#7: no 👍 yet"* ]]
[[ ! -e "$wrong_bot_args_file" ]]

list_error_output=$(cd "$TMPDIR_TEST" && env -u REPO MOCK_PR_LIST_FAIL=1 PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" --repo octo/direct-repo 1 2>&1 || true)
[[ "$list_error_output" == *"poll: failed to list open PRs; retrying"* ]]

api_error_output=$(cd "$TMPDIR_TEST" && env -u REPO MOCK_PR=7 MOCK_API_FAIL=1 PATH="$TMPDIR_TEST/bin:$PATH" "$ROOT/codex-automerge.sh" --repo octo/direct-repo 1 2>&1 || true)
[[ "$api_error_output" == *"#7: failed to fetch reactions; retrying"* ]]

[[ $(<"$ROOT/Procfile") == $'# Hivemind runs this named process from the Procfile.\n# Replace owner/repo with the GitHub repository to monitor.\ncodex-automerge: ./codex-automerge.sh --admin --repo owner/repo' ]]
