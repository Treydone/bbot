# Windows-side companion to scripts/launch-wsl.sh.
# Starts the ADB server on TCP:5037 so WSL can reach the phone.
# Idempotent: safe to run again; it won't start a second server or duplicate the firewall rule.
#
# Usage (run from Windows PowerShell — admin shell recommended the first time for the firewall rule):
#   .\scripts\start-adb-server.ps1
#   .\scripts\start-adb-server.ps1 -AdbPath "C:\Users\Me\Downloads\platform-tools\adb.exe"
#
# Notes:
#   - Leaves the ADB server running in the foreground. Ctrl+C to stop.
#   - Windows Defender Firewall rule is created with Direction=Inbound for TCP 5037.

param(
    [string]$AdbPath = "",
    [int]$Port = 5037
)

$ErrorActionPreference = "Stop"

function Write-Info($msg)  { Write-Host "[adb] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[ok]  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Write-Err2($msg)  { Write-Host "[err] $msg"  -ForegroundColor Red }

# --- locate adb ---
if ([string]::IsNullOrWhiteSpace($AdbPath)) {
    $candidates = @(
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "$env:USERPROFILE\Downloads\platform-tools\adb.exe",
        "$env:USERPROFILE\platform-tools\adb.exe",
        "C:\platform-tools\adb.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $AdbPath = $c; break }
    }
    if ([string]::IsNullOrWhiteSpace($AdbPath)) {
        $cmd = Get-Command adb -ErrorAction SilentlyContinue
        if ($cmd) { $AdbPath = $cmd.Source }
    }
}

if ([string]::IsNullOrWhiteSpace($AdbPath) -or -not (Test-Path $AdbPath)) {
    Write-Err2 "adb.exe not found. Pass -AdbPath or install platform-tools."
    Write-Err2 "Download: https://developer.android.com/studio/releases/platform-tools"
    exit 2
}
Write-Ok "adb binary: $AdbPath"

# --- firewall rule (needs admin the first time) ---
$ruleName = "WSL2 ADB Inbound TCP $Port"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Info "creating firewall rule '$ruleName' (requires admin if not already elevated)"
    try {
        New-NetFirewallRule -DisplayName $ruleName `
            -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port `
            -Profile Private,Domain `
            -ErrorAction Stop | Out-Null
        Write-Ok "firewall rule created"
    } catch {
        Write-Warn2 "could not create firewall rule (run elevated once to persist it): $_"
    }
} else {
    Write-Ok "firewall rule '$ruleName' already present"
}

# --- is adb server already running? ---
$listening = $false
try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $listening = $true }
} catch { }

if ($listening) {
    Write-Warn2 "port $Port already listening — killing any existing adb server to restart cleanly"
    & $AdbPath kill-server 2>&1 | Out-Null
    Start-Sleep -Seconds 1
}

# --- start server in foreground, listening on all interfaces ---
Write-Info "starting adb server on 0.0.0.0:$Port (Ctrl+C to stop)"
Write-Info "from WSL, run:  ./scripts/launch-wsl.sh"
& $AdbPath -a -P $Port nodaemon server
