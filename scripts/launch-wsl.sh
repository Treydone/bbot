#!/usr/bin/env bash
# Preflight + launch wrapper for GramAddict running on WSL against a Windows ADB server.
#
# Usage:
#   ./scripts/launch-wsl.sh                           # use ACCOUNT_NAME from .env
#   ./scripts/launch-wsl.sh --account glowofsin
#   ./scripts/launch-wsl.sh --pair                    # force interactive adb pair
#   ./scripts/launch-wsl.sh --reinit-uia2             # force python3 -m uiautomator2 init
#   ./scripts/launch-wsl.sh --diagnostic              # run preflight only, do not launch bot
#   ./scripts/launch-wsl.sh -- <extra args passed to python3 run.py>
#
# Prereqs (one-time):
#   - Windows: run scripts/start-adb-server.ps1 (starts adb TCP:5037 + firewall rule)
#   - WSL:     cp .env.example .env && edit PHONE_IP / CONNECT_PORT

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)"
cd "$REPO_ROOT"

# --- colored log helpers ---
_C_GRN=$'\e[32m'; _C_YEL=$'\e[33m'; _C_RED=$'\e[31m'; _C_CYA=$'\e[36m'; _C_RST=$'\e[0m'
log()  { printf "%s[launch]%s %s\n" "$_C_CYA" "$_C_RST" "$*"; }
ok()   { printf "%s[ok]%s    %s\n" "$_C_GRN" "$_C_RST" "$*"; }
warn() { printf "%s[warn]%s  %s\n" "$_C_YEL" "$_C_RST" "$*"; }
err()  { printf "%s[err]%s   %s\n" "$_C_RED" "$_C_RST" "$*" >&2; }
die()  { err "$*"; exit 1; }

# --- args ---
ACCOUNT_OVERRIDE=""
DO_PAIR=0
REINIT_UIA2=0
DIAGNOSTIC=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)      ACCOUNT_OVERRIDE="$2"; shift 2 ;;
    --pair)         DO_PAIR=1; shift ;;
    --reinit-uia2)  REINIT_UIA2=1; shift ;;
    --diagnostic|--check) DIAGNOSTIC=1; shift ;;
    --help|-h)      grep -E '^# ' "$0" | head -15; exit 0 ;;
    --)             shift; EXTRA_ARGS=("$@"); break ;;
    *)              EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# --- load .env ---
if [[ -f .env ]]; then
  set -a; source .env; set +a
  ok ".env loaded"
elif [[ -f .env.example ]]; then
  warn ".env not found, using .env.example defaults. Copy it to .env and edit before real use."
  set -a; source .env.example; set +a
else
  die "no .env or .env.example found in $REPO_ROOT"
fi

ACCOUNT_NAME="${ACCOUNT_OVERRIDE:-${ACCOUNT_NAME:-}}"
[[ -n "$ACCOUNT_NAME" ]] || die "ACCOUNT_NAME not set (in .env or --account)"
CONFIG_PATH="accounts/${ACCOUNT_NAME}/config.yml"
[[ -f "$CONFIG_PATH" ]] || die "config not found: $CONFIG_PATH"

PHONE_IP="${PHONE_IP:?PHONE_IP not set in .env}"
CONNECT_PORT="${CONNECT_PORT:?CONNECT_PORT not set in .env}"
ADB_SERVER_PORT="${ADB_SERVER_PORT:-5037}"

# --- detect Windows host IP (WSL gateway) ---
if ! command -v ip &>/dev/null; then
  die "'ip' command missing. Install iproute2: sudo apt install iproute2"
fi
WSL_HOST_IP="$(ip route show | awk '/default/ {print $3; exit}')"
[[ -n "$WSL_HOST_IP" ]] || die "could not detect WSL gateway IP from 'ip route show'"
ok "WSL→Windows gateway: ${WSL_HOST_IP}:${ADB_SERVER_PORT}"

export ANDROID_ADB_SERVER_HOST="$WSL_HOST_IP"
export ANDROID_ADB_SERVER_PORT="$ADB_SERVER_PORT"
export ADB_SERVER_SOCKET="tcp:${WSL_HOST_IP}:${ADB_SERVER_PORT}"

# --- locate adb ---
if [[ -n "${ADB_PATH:-}" ]] && [[ -x "$ADB_PATH" ]]; then
  ADB="$ADB_PATH"
elif command -v adb &>/dev/null; then
  ADB="$(command -v adb)"
else
  die "adb not found. Install platform-tools or set ADB_PATH in .env"
fi
ok "adb binary: $ADB"

# --- check ADB server reachable ---
if ! "$ADB" version &>/dev/null; then
  err "adb version failed — server on Windows unreachable"
  warn "On Windows, run: scripts\\start-adb-server.ps1"
  warn "Then verify firewall allows inbound TCP ${ADB_SERVER_PORT}"
  exit 2
fi

# --- check device connection ---
DEVICES="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}')"

if [[ -z "$DEVICES" ]]; then
  if [[ "$DO_PAIR" -eq 1 ]]; then
    PAIR_PORT_USE="${PAIR_PORT:-}"
    [[ -n "$PAIR_PORT_USE" ]] || read -r -p "Pair port (from phone): " PAIR_PORT_USE
    PAIR_CODE_USE="${PAIR_CODE:-}"
    [[ -n "$PAIR_CODE_USE" ]] || read -r -p "Pair code (from phone): " PAIR_CODE_USE
    log "adb pair ${PHONE_IP}:${PAIR_PORT_USE}"
    printf "%s\n" "$PAIR_CODE_USE" | "$ADB" pair "${PHONE_IP}:${PAIR_PORT_USE}" || die "pair failed"
  fi
  log "attempting adb connect ${PHONE_IP}:${CONNECT_PORT}"
  "$ADB" connect "${PHONE_IP}:${CONNECT_PORT}" || true
  sleep 1
  DEVICES="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}')"
fi

[[ -n "$DEVICES" ]] || {
  err "no device connected. Check phone WiFi IP/port, re-pair if needed:"
  warn "  ./scripts/launch-wsl.sh --pair"
  exit 3
}

SERIAL="$(echo "$DEVICES" | head -1)"
ok "device ready: $SERIAL"
export ANDROID_SERIAL="$SERIAL"

# --- device health checks ---
SCREEN_ON=$("$ADB" -s "$SERIAL" shell dumpsys power 2>/dev/null | grep -E "mWakefulness=|mScreenOn=" | head -2 || true)
log "screen state: $(echo "$SCREEN_ON" | tr '\n' ' ')"

IG_VER=$("$ADB" -s "$SERIAL" shell dumpsys package com.instagram.android 2>/dev/null | awk '/versionName=/ {sub(/.*versionName=/,""); print $1; exit}' || true)
if [[ -z "$IG_VER" ]]; then
  warn "com.instagram.android not detected on device — install Instagram first"
else
  ok "Instagram version on device: $IG_VER"
fi

FREE_MB=$("$ADB" -s "$SERIAL" shell df -m /sdcard 2>/dev/null | awk 'NR==2 {print $4}' || echo "?")
log "free storage on /sdcard: ${FREE_MB} MB"
if [[ "$FREE_MB" =~ ^[0-9]+$ ]] && (( FREE_MB < 500 )); then
  warn "low storage on device (< 500 MB) — pushing media may fail"
fi

# --- uiautomator2 server / ATX-agent ---
ATX_PRESENT=$("$ADB" -s "$SERIAL" shell "ls /data/local/tmp/atx-agent 2>/dev/null | head -1" || true)
if [[ "$REINIT_UIA2" -eq 1 ]] || [[ -z "$ATX_PRESENT" ]]; then
  log "(re)initializing uiautomator2 on device (this may take ~30s)"
  if ! python3 -m uiautomator2 init 2>&1 | tail -5; then
    die "uiautomator2 init failed"
  fi
  ok "uiautomator2 ready"
else
  ok "uiautomator2 already installed on device"
fi

# --- virtualenv ---
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  ok "venv activated: $(python3 --version)"
elif [[ -f venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
  ok "venv activated: $(python3 --version)"
else
  warn "no .venv or venv/ found — using system python3 ($(python3 --version))"
fi

if [[ "$DIAGNOSTIC" -eq 1 ]]; then
  ok "diagnostic complete (not launching bot)"
  exit 0
fi

# --- launch bot ---
log "launching: python3 run.py --config $CONFIG_PATH ${EXTRA_ARGS[*]:-}"
exec python3 run.py --config "$CONFIG_PATH" "${EXTRA_ARGS[@]}"
