# Restart the FundPulse webapp (python -m webapp).
#
# Stops any running `python -m webapp`, starts a fresh instance in the
# background, and health-checks it until it responds (or fails).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\restart_webapp.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\restart_webapp.ps1 -Root D:\opencode\mf_holding -Port 8000

param(
    [string]$Root = "D:\opencode\mf_holding",
    [int]$Port = 8000,
    [int]$Retries = 20,
    [int]$DelaySec = 2
)

$ErrorActionPreference = "Stop"
$url = "http://127.0.0.1:$Port/api/health"
$outLog = Join-Path $Root "webapp_restart.log"
$errLog = Join-Path $Root "webapp_restart.err.log"

# 1. Stop any existing webapp process (match the command line, not just any python).
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '-m\s+webapp' } |
    ForEach-Object {
        Write-Host "Stopping webapp (PID $($_.ProcessId))..."
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 1

# 2. Start a fresh instance in the background.
Write-Host "Starting webapp (root: $Root)..."
$proc = Start-Process -FilePath "python" -ArgumentList "-m", "webapp" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
    -WindowStyle Hidden -PassThru
Write-Host "Started webapp (PID $($proc.Id))"

# 3. Health-check with retries. Report a clear DONE on success.
$start = Get-Date
for ($i = 1; $i -le $Retries; $i++) {
    Start-Sleep -Seconds $DelaySec
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            $elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
            Write-Host "DONE: webapp is UP and healthy at $url (ready after $elapsed s)"
            exit 0
        }
    }
    catch {
        if ($i -lt $Retries) {
            Write-Host "Not ready yet... (check $i/$Retries)"
        }
    }
}

Write-Host "FAILED: webapp did not become healthy after $Retries checks. Last lines of $errLog :"
Get-Content $errLog -Tail 30 -ErrorAction SilentlyContinue
exit 1
