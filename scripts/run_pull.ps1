# Live-run wrapper for scripts/pull_annual_results.py.
#
# Launches the rebuild in the background (GLM vision via OpenRouter) and
# then STREAMS the log in the same terminal, so progress appears live right
# after "PID: <n>". Every line shows: stock symbol, PDF file, the year the
# filing relates to, and the per-stock summary once it is built.
#
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File scripts/run_pull.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/run_pull.ps1 -Symbols KRBL,TCS -MaxDocs 5
#
# Ctrl+C stops only the live tail; the background rebuild keeps running.
# To stop the run for real: Stop-Process -Id <PID>; the PID is printed first.

param(
    [string]$Symbols = "",
    [int]$MaxDocs = 90,
    [int]$Pages = 30,
    [int]$AiPages = 24,
    [double]$Sleep = 1.2,
    [switch]$NoAi,
    [switch]$Refresh
)

$env:STMT_BACKEND = 'openrouter'
$env:STMT_AI = '1'
$env:STMT_AI_MODEL = 'z-ai/glm-5.3-flash'
$env:STMT_AI_FALLBACK = 'never'
$env:PYTHONUTF8 = '1'

$root = Split-Path -Parent $PSScriptRoot
$ts = Get-Date -Format 'yyMMdd_HHmmss'
$log = Join-Path $env:TEMP "pull_live_$ts.log"
$err = Join-Path $env:TEMP "pull_live_$ts.err.log"
if (Test-Path $log) { Remove-Item $log -Force }
if (Test-Path $err) { Remove-Item $err -Force }

$runArgs = @('-u', '-X', 'utf8', 'scripts/pull_annual_results.py',
             '--max-docs', "$MaxDocs", '--pages', "$Pages",
             '--ai-pages', "$AiPages", '--sleep', "$Sleep")
if ($Symbols) { $runArgs += @('--symbols', $Symbols) }
if ($NoAi) { $runArgs += '--no-ai' }
if ($Refresh) { $runArgs += '--refresh-status' }

$p = Start-Process -FilePath 'python' -ArgumentList $runArgs `
    -WorkingDirectory $root -RedirectStandardOutput $log `
    -RedirectStandardError $err -WindowStyle Hidden -PassThru

Write-Host "PID: $($p.Id)"
Write-Host "Log: $log"
Write-Host "--- live progress ---"
try {
    Get-Content -Path $log -Wait -Tail 40
} catch {
    Write-Host ""
    Write-Host "tail stopped; the rebuild keeps running (PID $($p.Id))."
    Write-Host "full log: $log"
}