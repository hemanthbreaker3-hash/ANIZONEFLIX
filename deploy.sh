#!/usr/bin/env bash
# AniZoneFlix VPS/Termux deployment — FAST mode, no long lock waits, 24/7 systemd.
# Default: zero wait. Set APT_LOCK_TIMEOUT=30 if you prefer a short wait.
# FORCE_APT_UNLOCK=1 will kill stuck apt/dpkg and remove locks (use only if stuck).
set -Eeuo pipefail
cd "$(dirname "$0")"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log(){ printf "${CYAN}[deploy]${NC} %s\n" "$*"; }
ok(){ printf "${GREEN}[ok]${NC} %s\n" "$*"; }
warn(){ printf "${YELLOW}[warn]${NC} %s\n" "$*"; }
die(){ printf "${RED}[error]${NC} %s\n" "$*" >&2; exit 1; }
trap 'rc=$?; printf "${RED}[error]${NC} Deployment failed at line %s (exit %s). Check the message above.\n" "$LINENO" "$rc" >&2; exit "$rc"' ERR

[[ -f main.py && -f requirements.txt ]] || die "Run this script from the project directory (main.py and requirements.txt must exist)."
IS_TERMUX=0
if [[ -n "${PREFIX:-}" && -d /data/data/com.termux ]] || command -v termux-info >/dev/null 2>&1; then IS_TERMUX=1; fi
IS_ROOT=0; [[ $EUID -eq 0 ]] && IS_ROOT=1
SUDO=()
if [[ $IS_ROOT -eq 0 && $IS_TERMUX -eq 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "sudo is required. Run as root or install sudo."
  SUDO=(sudo)
fi
runroot(){ if [[ $IS_TERMUX -eq 1 ]]; then "$@"; else "${SUDO[@]}" "$@"; fi; }

# FAST lock handling: default timeout 0 (no wait). Optional force unlock.
wait_apt_lock(){
  local elapsed=0
  local max="${APT_LOCK_TIMEOUT:-0}"   # 0 = no wait, proceed immediately
  local holders=""
  local force="${FORCE_APT_UNLOCK:-0}"

  while :; do
    holders=""
    if command -v fuser >/dev/null 2>&1; then
      holders="$(fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock 2>/dev/null || true)"
    fi
    if [[ -z "${holders//[[:space:]]/}" ]]; then
      if pgrep -x apt >/dev/null 2>&1 || pgrep -x apt-get >/dev/null 2>&1 || pgrep -x dpkg >/dev/null 2>&1 || pgrep -x unattended-upgr >/dev/null 2>&1; then
        holders="package manager process"
      fi
    fi
    [[ -z "${holders//[[:space:]]/}" ]] && return 0

    if [[ "$force" == "1" ]]; then
      warn "FORCE_APT_UNLOCK=1 — killing stuck apt/dpkg and removing locks…"
      runroot pkill -9 -x apt 2>/dev/null || true
      runroot pkill -9 -x apt-get 2>/dev/null || true
      runroot pkill -9 -x dpkg 2>/dev/null || true
      runroot pkill -9 -x unattended-upgr 2>/dev/null || true
      runroot rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock-frontend.lock 2>/dev/null || true
      runroot dpkg --configure -a 2>/dev/null || true
      return 0
    fi

    if (( max <= 0 )); then
      warn "APT/DPKG busy (holder: ${holders}). Proceeding immediately (APT_LOCK_TIMEOUT=0). If install fails, re-run with FORCE_APT_UNLOCK=1"
      return 0
    fi

    if (( elapsed >= max )); then
      warn "APT/DPKG still busy after ${max}s. Proceeding anyway. Re-run with FORCE_APT_UNLOCK=1 if needed."
      return 0
    fi
    if (( elapsed % 5 == 0 )); then warn "APT/DPKG busy (${elapsed}/${max}s; holder: ${holders})…"; fi
    sleep 1
    elapsed=$((elapsed+1))
  done
}
apt(){ wait_apt_lock; runroot apt-get "$@"; }

log "Detecting platform… (FAST deploy, no long waits)"
if [[ $IS_TERMUX -eq 1 ]]; then
  log "Termux detected; installing build dependencies."
  pkg update -y || true
  pkg install -y python git clang libffi openssl rust binutils make || true
elif [[ -f /etc/alpine-release ]]; then
  runroot apk add --no-cache python3 py3-pip py3-virtualenv git build-base libffi-dev openssl-dev curl bash || true
elif [[ -f /etc/redhat-release || -f /etc/rocky-release || -f /etc/almalinux-release ]]; then
  PM=dnf; command -v dnf >/dev/null 2>&1 || PM=yum
  runroot "$PM" install -y python3 python3-pip git gcc gcc-c++ make libffi-devel openssl-devel curl || true
elif command -v apt-get >/dev/null 2>&1; then
  log "APT mode — fast path (no lock queue)…"
  apt update -y || apt update || true
  apt install -y python3 python3-venv python3-pip git build-essential libffi-dev libssl-dev curl ca-certificates screen || true
else
  die "Unsupported package manager. Install Python 3, pip, venv and build tools manually."
fi

PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "Python 3 was not found after package installation."
"$PY" -c 'import sys; print("Python", sys.version.split()[0])'
if [[ $IS_TERMUX -eq 1 ]]; then
  export CFLAGS="-I${PREFIX}/include" LDFLAGS="-L${PREFIX}/lib"
fi

# Preserve existing .env values; only overwrite keys explicitly exported by caller.
touch .env
chmod 600 .env
export DEPLOY_ENV_FILE="$PWD/.env"
"$PY" - <<'PY'
import os, secrets
path=os.environ["DEPLOY_ENV_FILE"]
keys=("WEB_ONLY","API_ID","API_HASH","BOT_TOKEN","ADMIN_IDS","ADMIN_API_KEY","MONGO_URI","DB_NAME","BASE_URL","SECRET_KEY","LOGO_URL","JIKAN_API","PORT","TMDB_API_KEY","SIMKL_ID")
try:
    lines=open(path,encoding="utf-8").read().splitlines()
except OSError:
    lines=[]
values={}
for key in keys:
    val=os.environ.get(key)
    if val is not None and val != "":
        values[key]=val
if not any(line.startswith("SECRET_KEY=") and line.split("=",1)[1].strip() for line in lines) and not os.environ.get("SECRET_KEY"):
    values["SECRET_KEY"]=secrets.token_hex(32)
values["WEB_ONLY"]="false"
seen=set(); out=[]
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key=line.split("=",1)[0]
        if key in values:
            if key not in seen: out.append(f"{key}={values[key]}"); seen.add(key)
            continue
    out.append(line)
for key,val in values.items():
    if key not in seen: out.append(f"{key}={val}")
with open(path,"w",encoding="utf-8") as f: f.write("\n".join(out).rstrip()+"\n")
os.chmod(path,0o600)
PY

# Load .env values for validation and process startup (without printing secrets).
set -a
# shellcheck disable=SC1091
source .env
set +a
export WEB_ONLY=false
for key in API_ID API_HASH BOT_TOKEN MONGO_URI; do
  [[ -n "${!key:-}" ]] || die "Missing $key in .env. Edit .env and run bash deploy.sh again."
done
[[ "${API_ID}" =~ ^[0-9]+$ ]] || die "API_ID must be numeric."
[[ -n "${SECRET_KEY:-}" ]] || die "SECRET_KEY is empty; rerun deployment to generate it."

log "Creating/updating isolated Python virtual environment…"
if [[ ! -x venv/bin/python ]]; then
  "$PY" -m venv venv || die "Could not create venv. Install python3-venv (Ubuntu/Debian) or py3-virtualenv (Alpine)."
fi
venv/bin/python -m pip install --upgrade pip setuptools wheel
venv/bin/python -m pip install --no-cache-dir -r requirements.txt

log "Checking application imports and configuration…"
venv/bin/python - <<'PY'
from config.config import Config
ok, message = Config.validate()
print(message)
if not ok: raise SystemExit(1)
import main
print("Application import check passed.")
PY

# systemd is the most reliable VPS process manager for 24/7.
if [[ $IS_TERMUX -eq 0 && $IS_ROOT -eq 1 ]] && command -v systemctl >/dev/null 2>&1; then
  SERVICE="anizoneflix"
  APP_DIR="$PWD"
  PYTHON_BIN="$APP_DIR/venv/bin/python"
  cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=AniZoneFlix Telegram Bot + Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=WEB_ONLY=false
ExecStart=${PYTHON_BIN} ${APP_DIR}/main.py
Restart=always
RestartSec=3
TimeoutStopSec=20
KillSignal=SIGINT
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  sleep 2
  if systemctl is-active --quiet "$SERVICE"; then
    ok "AniZoneFlix is ACTIVE and set to run 24/7 (auto-restart + boot)."
    echo "Logs:   journalctl -u anizoneflix -f"
    echo "Status: systemctl status anizoneflix"
    echo "Stop:   systemctl stop anizoneflix"
  else
    systemctl --no-pager --full status "$SERVICE" || true
    journalctl -u "$SERVICE" -n 40 --no-pager || true
    die "Service did not stay running. Review the logs above."
  fi
elif [[ $IS_TERMUX -eq 0 ]] && command -v screen >/dev/null 2>&1; then
  screen -S anizoneflix -X quit >/dev/null 2>&1 || true
  screen -dmS anizoneflix bash -lc "cd '$PWD' && exec '$PWD/venv/bin/python' '$PWD/main.py'"
  sleep 2
  screen -list | grep -q anizoneflix || die "Bot failed to stay running in screen. Run: venv/bin/python main.py"
  ok "Bot running in screen session 'anizoneflix' (24/7 while screen is up). Attach: screen -r anizoneflix"
else
  ok "Dependencies and configuration validated. Starting in foreground…"
  exec venv/bin/python main.py
fi
