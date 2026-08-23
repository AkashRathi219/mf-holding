# Production smoke test — DNS-proof (pins the resolved edge IP).
# Usage:
#   powershell -File scripts\smoke_prod.ps1                       # auto-resolve via 1.1.1.1
#   powershell -File scripts\smoke_prod.ps1 -BaseUrl https://fundpulse.aracharatventures.com
# Exit code 1 if any check fails (CI-friendly).
param(
    [string]$BaseUrl = "https://mf-holding-production-7baa.up.railway.app",
    [string]$DnsServer = "1.1.1.1"
)

$ErrorActionPreference = "Stop"
$host_ = ([uri]$BaseUrl).Host

# Resolve via public DNS, bypassing any poisoned local cache.
$ans = Resolve-DnsName $host_ -Server $DnsServer -Type A -ErrorAction SilentlyContinue |
    Where-Object IPAddress | Select-Object -First 1
if (-not $ans) {
    Write-Host "FAIL: '$host_' does not resolve even via $DnsServer" -ForegroundColor Red
    exit 1
}
$ip = $ans.IPAddress
Write-Host "target : $host_ @ $ip"

$results = foreach ($path in "/api/version", "/api/health", "/api/scope-stats", "/login") {
    $args = @("-s", "--max-time", "20",
              "--resolve", "${host_}:443:${ip}")
    $out = & curl.exe @args "$BaseUrl$path" 2>$null
    $code = (& curl.exe @args -o NUL -w "%{http_code}" "$BaseUrl$path" 2>$null)
    [pscustomobject]@{
        Path   = $path
        Status = [int]$code
        Ok     = ($code -ge 200 -and $code -lt 500)
        Peek   = (($out | Out-String).Trim() -replace "\s+", " ")
        Detail = $out
    }
}

$results | Format-Table Path, Status, Ok -AutoSize
foreach ($r in $results) {
    if ($r.Path -eq "/api/version") { Write-Host "  version : $($r.Detail)" }
    if ($r.Path -eq "/api/health")  { Write-Host "  health  : $($r.Detail)" }
}

if ($results.Where({ -not $_.Ok })) {
    Write-Host "SMOKE FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "SMOKE PASSED" -ForegroundColor Green
