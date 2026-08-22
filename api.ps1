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

$uv = Find-Uv
Write-Host "YQuo demo backend -> http://127.0.0.1:8000/docs" -ForegroundColor Cyan
& $uv run --project "$repo\backend" uvicorn yquo.api.main:app --reload --port 8000
