#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${1:-${PI_HOST:-}}"
PI_USER="${PI_USER:-pi}"
REMOTE_DIR="${REMOTE_DIR:-/home/${PI_USER}/fantasy_baseball}"
SERVICE_NAME="${SERVICE_NAME:-fantasy-baseball}"

if [[ -z "${PI_HOST}" ]]; then
  echo "Usage: PI_HOST=raspberrypi.local ./deploy.sh"
  echo "   or: ./deploy.sh raspberrypi.local"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${PI_USER}@${PI_HOST}"

echo "Syncing project to ${REMOTE}:${REMOTE_DIR}"
rsync -avz --delete \
  --exclude ".env" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "yahoo_tokens.json" \
  --exclude ".git/" \
  "${SCRIPT_DIR}/" "${REMOTE}:${REMOTE_DIR}/"

echo "Updating venv and restarting ${SERVICE_NAME} on ${REMOTE}"
ssh "${REMOTE}" "bash -lc '
  set -euo pipefail
  cd \"${REMOTE_DIR}\"
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  . .venv/bin/activate
  pip install -r requirements.txt
  sudo systemctl daemon-reload
  sudo systemctl restart \"${SERVICE_NAME}\"
  sudo systemctl status \"${SERVICE_NAME}\" --no-pager
'"

echo "Deploy complete."
