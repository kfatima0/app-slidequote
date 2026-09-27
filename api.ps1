# Start the demo backend. The Windows equivalent of `make api`.
#
# Once running, open http://127.0.0.1:8000/docs to send a message and watch an
# order move, with no frontend needed. Ctrl+C stops it.

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot

function Find-Uv {
    $onPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $candidates = @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "uv not found. Install it from https://docs.astral.sh/uv/ and re-run."
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
$env:SLIDEQUOTE_POLICY__CONFIRMATION_WINDOW_S = "7200"
$env:SLIDEQUOTE_POLICY__REMINDER_AFTER_S = "3600"

$uv = Find-Uv
Write-Host "SlideQuote demo backend -> http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host "  time x60 | 2 minutes to confirm a quote" -ForegroundColor DarkGray
& $uv run --project "$repo\backend" uvicorn slidequote.api.main:app --reload --port 8000
