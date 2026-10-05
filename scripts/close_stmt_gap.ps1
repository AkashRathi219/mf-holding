# Gap-closing chain for docs/NIFTY50_STATEMENTS_GAP.md:
#   1) deep feed scan + downloads (incl. refused-PDF retry passes)
#   2) full-universe re-parse via run_pull_waves --refresh (GLM/OpenRouter)
#   3) coverage report refresh
# Usage: powershell -ExecutionPolicy Bypass -File scripts/close_stmt_gap.ps1
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = '1'
# GLM vision via OpenRouter (opencode free tier retired for these runs)
$env:STMT_BACKEND = 'openrouter'
$env:STMT_AI = '1'
$env:STMT_AI_MODEL = 'z-ai/glm-5.3-flash'
$env:STMT_AI_FALLBACK = 'never'

$symbols = 'ADANIENT,APOLLOHOSP,AXISBANK,BAJAJ-AUTO,BAJAJFINSV,BAJFINANCE,BEL,' +
           'BHARTIARTL,CIPLA,COALINDIA,EICHERMOT,GRASIM,HDFCBANK,HDFCLIFE,' +
           'HINDALCO,HINDUNILVR,INFY,JSWSTEEL,M&M,MAXHEALTH,NESTLEIND,ONGC,' +
           'POWERGRID,SBILIFE,SBIN,SHRIRAMFIN,TATACONSUM,TATASTEEL,TITAN,' +
           'TRENT,WIPRO'

Write-Host "=== [1/3] deep download ($symbols.Split(',').Count symbols, pages 100) ==="
python -u scripts/download_results.py --symbols $symbols --pages 100 `
    --workers 5 --passes 2 --pause 90
Write-Host "=== [2/3] full-universe re-parse (refresh) ==="
python -u scripts/run_pull_waves.py --from-local --refresh --waves 3 `
    --max-attempts 10 --agents 5 --batch 10 --pages 100 --max-docs 120
Write-Host "=== [3/3] coverage report ==="
python scripts/statement_coverage_report.py
Write-Host "=== gap-closing chain complete ==="
