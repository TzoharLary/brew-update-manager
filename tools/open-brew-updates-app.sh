#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_LOG="${PROJECT_ROOT}/data/logs/app-launcher.log"

mkdir -p "$(dirname "$APP_LOG")"

if [[ ! -f "$PROJECT_ROOT/package.json" ]]; then
  echo "[open-brew-updates-app] package.json not found at $PROJECT_ROOT" >> "$APP_LOG"
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[open-brew-updates-app] npm is not installed" >> "$APP_LOG"
  exit 1
fi

nohup npm --prefix "$PROJECT_ROOT" run start-app >> "$APP_LOG" 2>&1 &
