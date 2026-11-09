#!/usr/bin/env bash
# Take the README's screenshots of the real prherd window, populated with demo
# data from scripts/demo-gh/gh instead of GitHub.
#
# Used by .github/workflows/screenshots.yml on Ubuntu. Also runnable on any Linux
# box with: python3-tk xvfb xdotool imagemagick
#
#   bash scripts/screenshots.sh        # writes docs/screenshots/*.png
#
# Runs under its own virtual display and a throwaway config dir, with merging
# off, so it never touches your settings, and nothing it does reaches GitHub.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=docs/screenshots
DISPLAY_NUM="${SCREENSHOT_DISPLAY:-:99}"
WORK="$(mktemp -d)"
mkdir -p "$OUT"

export XDG_CONFIG_HOME="$WORK/config"
export DEMO_GH_LOG="$WORK/gh.log"
export PATH="$PWD/scripts/demo-gh:$PATH"
export DISPLAY="$DISPLAY_NUM"
mkdir -p "$XDG_CONFIG_HOME"
: > "$DEMO_GH_LOG"

# Saved repos skip `gh repo view`; an hour's interval means only the startup poll runs.
cat > "$XDG_CONFIG_HOME/prherd.json" <<'JSON'
{
  "repos": ["github.com/octo-org/storefront", "github.com/octo-org/api", "github.com/octo-org/docs"],
  "mode": "",
  "interval": 3600,
  "merging": false,
  "geometry": "1180x720+0+0"
}
JSON

cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${XVFB_PID:-}" ] && kill "$XVFB_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> Starting a virtual display on $DISPLAY_NUM"
Xvfb "$DISPLAY_NUM" -screen 0 1400x900x24 -nolisten tcp >"$WORK/xvfb.log" 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 50); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
xdpyinfo >/dev/null 2>&1 || { echo "Xvfb never came up" >&2; cat "$WORK/xvfb.log" >&2; exit 1; }

echo "==> Launching prherd against the demo gh"
python3 prherd.py >"$WORK/app.log" 2>&1 &
APP_PID=$!

echo "==> Waiting for the first poll to finish"
# Done when every repo has been listed and gh has had no calls for two seconds.
repos=3
for _ in $(seq 1 120); do
  kill -0 "$APP_PID" 2>/dev/null || { echo "prherd exited:" >&2; cat "$WORK/app.log" >&2; exit 1; }
  listed="$(grep -c '^pr list' "$DEMO_GH_LOG" || true)"
  if [ "$listed" -ge "$repos" ]; then
    before="$(wc -l < "$DEMO_GH_LOG")"; sleep 2; after="$(wc -l < "$DEMO_GH_LOG")"
    [ "$before" = "$after" ] && break
  else
    sleep 0.5
  fi
done
[ "$(grep -c '^pr list' "$DEMO_GH_LOG" || true)" -ge "$repos" ] || { echo "polling never finished" >&2; exit 1; }
sleep 1  # the window drains its queue every 200 ms

wid="$(xdotool search --sync --onlyvisible --name '^prherd$' | head -1)"
[ -n "$wid" ] || { echo "no prherd window" >&2; exit 1; }

echo "==> Capturing"
import -window "$wid" "$OUT/main.png"
# Fail rather than commit a blank capture: a populated window has many colors.
colors="$(identify -format '%k' "$OUT/main.png")"
[ "$colors" -gt 50 ] || { echo "capture looks blank ($colors colors)" >&2; exit 1; }
echo "==> Wrote $OUT/main.png ($(identify -format '%wx%h' "$OUT/main.png"), $colors colors)"
