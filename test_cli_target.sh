#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT
mkdir -p "$TMPDIR_TEST/bin" "$TMPDIR_TEST/local-repo"

cat >"$TMPDIR_TEST/bin/gh" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-C" ]]; then
  [[ "${2:-}" == "local-repo" ]] && echo "acme/local-repo"
elif [[ "${1:-}" == "repo" && "${2:-}" == "view" && "${3:-}" == "octo/direct-repo" ]]; then
  echo "octo/direct-repo"
elif [[ "${1:-}" == "repo" && "${2:-}" == "view" ]]; then
  echo "wrong/current-repo"
elif [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
  [[ -n "${MOCK_PR:-}" ]] && echo "$MOCK_PR"
elif [[ "${1:-}" == "api" ]]; then
  echo "+1"
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

[[ $(<"$ROOT/Procfile") == $'# Hivemind runs this named process from the Procfile.\n# Replace owner/repo with the GitHub repository to monitor.\ncodex-automerge: ./codex-automerge.sh --admin --repo owner/repo' ]]
