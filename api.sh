#!/usr/bin/env bash
# Start the demo backend. The macOS and Linux twin of api.ps1.
#
# Once running, open http://127.0.0.1:8000/docs to send a message and watch an
# order move, with no frontend needed. Ctrl+C stops it.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return
    fi
  done
  echo "uv not found. Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
}

# Demo pacing. One acceleration factor cannot serve both halves of the flow:
# canvassing wants to be fast enough to watch, and the confirmation window wants
# to be slow enough to talk over. At 60x the real 30-minute window becomes 30
# wall-clock seconds, which expires the order while the presenter is still
# explaining the screen.
#
# So the window is widened for the demo only. Production values stay in
# config/settings.py: 1800s to confirm, reminder at 900s.
#
#   7200s simulated / 60 = 2 minutes to reply
#   3600s simulated / 60 = 1 minute before the reminder
#
# To demo the expiry deliberately (scenario 5), drop these back to 1800 and 900
# and simply do not answer.
export YQUO_POLICY__CONFIRMATION_WINDOW_S=7200
export YQUO_POLICY__REMINDER_AFTER_S=3600

uv="$(find_uv)"
echo "YQuo demo backend -> http://127.0.0.1:8000/docs"
echo "  time x60 | 2 minutes to confirm a quote"
exec "$uv" run --project "$repo/backend" uvicorn yquo.api.main:app --reload --port 8000
