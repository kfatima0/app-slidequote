#!/usr/bin/env bash
# Lint, type-check and test. The macOS and Linux twin of check.ps1.
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

uv="$(find_uv)"
echo "using uv at $uv"

echo
echo "== lint =="
"$uv" run --project "$repo/backend" ruff check "$repo/backend/src" "$repo/tests"

echo
echo "== types =="
(cd "$repo/backend" && "$uv" run mypy)

echo
echo "== tests =="
(cd "$repo" && "$uv" run --project "$repo/backend" pytest -q)

echo
echo "all green"
