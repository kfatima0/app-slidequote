# Lint, type-check and test. The Windows equivalent of `make check`.
#
# This exists because PATH on this machine is unreliable: conda's PowerShell hook
# rebuilds it at shell startup and drops the entry uv installs itself into. So we
# find uv rather than assuming the shell can.

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
Write-Host "using uv at $uv" -ForegroundColor DarkGray

Write-Host "`n== lint ==" -ForegroundColor Cyan
& $uv run --project "$repo\backend" ruff check "$repo\backend\src" "$repo\tests"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n== types ==" -ForegroundColor Cyan
Push-Location "$repo\backend"
try {
    & $uv run mypy
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally { Pop-Location }

Write-Host "`n== tests ==" -ForegroundColor Cyan
Push-Location $repo
try {
    & $uv run --project "$repo\backend" pytest -q
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally { Pop-Location }

Write-Host "`nall green" -ForegroundColor Green
