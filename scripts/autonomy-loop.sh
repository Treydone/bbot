#!/usr/bin/env bash
# Wrapper d'autonomie : relance le bot si jamais il sort via sys.exit
# (par ex. quand watch-reels hit son reels-watches-limit → stop_bot dur).
#
# Usage:
#   ./scripts/autonomy-loop.sh                           # config par défaut
#   ./scripts/autonomy-loop.sh --account other_account   # compte custom
#
# Ctrl+C propre à n'importe quel moment (trap SIGINT → exit clean).

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)"
cd "$REPO_ROOT"

ACCOUNT="${ACCOUNT_NAME:-glowofsin}"
CFG="accounts/${ACCOUNT}/config.yml"

# Parse --account override
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="$2"; CFG="accounts/${ACCOUNT}/config.yml"; shift 2 ;;
    *) shift ;;
  esac
done

# Load .env for ADB env vars if present
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
export ANDROID_ADB_SERVER_HOST="${ANDROID_ADB_SERVER_HOST:-$(ip route show | awk '/default/ {print $3; exit}')}"
export ANDROID_ADB_SERVER_PORT="${ANDROID_ADB_SERVER_PORT:-5037}"
export ADB_SERVER_SOCKET="tcp:${ANDROID_ADB_SERVER_HOST}:${ANDROID_ADB_SERVER_PORT}"

# Activate venv if present
[[ -f .venv/bin/activate ]] && source .venv/bin/activate

_stop=0
trap '_stop=1' INT TERM

# Min gap between restarts (seconds) — don't thrash if the bot insta-crashes
MIN_GAP=900          # 15 min
MAX_GAP=2700         # 45 min
LAST_RESTART=0

echo "[autonomy] loop starting at $(date) — config: $CFG"
while [[ $_stop -eq 0 ]]; do
  now=$(date +%s)
  since_last=$(( now - LAST_RESTART ))
  if [[ $since_last -lt $MIN_GAP && $LAST_RESTART -ne 0 ]]; then
    sleep_for=$(( MIN_GAP - since_last ))
    echo "[autonomy] bot exited too quickly; cooldown ${sleep_for}s before restart"
    # honor SIGINT during cooldown
    for ((i=0; i<sleep_for && _stop==0; i++)); do sleep 1; done
    [[ $_stop -eq 1 ]] && break
  fi

  LAST_RESTART=$(date +%s)
  echo "[autonomy] starting bot at $(date)"
  python3 run.py --config "$CFG"
  rc=$?
  echo "[autonomy] bot exited rc=$rc at $(date)"

  if [[ $_stop -eq 1 ]]; then
    echo "[autonomy] stop requested — exiting loop"
    break
  fi

  # Random sleep between sessions: 30–60 min (humain, évite le thrash)
  gap=$(( RANDOM % (MAX_GAP - MIN_GAP) + MIN_GAP ))
  echo "[autonomy] sleeping ${gap}s (~$((gap/60)) min) before next session"
  for ((i=0; i<gap && _stop==0; i++)); do sleep 1; done
done

echo "[autonomy] loop terminated at $(date)"
