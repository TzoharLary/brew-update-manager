#!/bin/bash
set -euo pipefail

##############################################################################
# Daily LaunchAgent entrypoint (09:00)
# Runs a check + notification using project-local backend service.
##############################################################################

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SCRIPT="${PROJECT_ROOT}/backend/brew-updates-service.py"
PYTHON_BIN=""

select_python_314() {
  local candidate
  for candidate in \
    "/usr/local/bin/python3" \
    "/usr/local/bin/python3.14" \
    "$(command -v python3.14 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)"
  do
    [[ -n "$candidate" ]] || continue
    [[ -x "$candidate" ]] || continue

    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (sys.version_info.major, sys.version_info.minor) == (3, 14) else 1)
PY
    then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done

  echo "[brew-outdated-check] Python 3.14 not found. Aborting." >&2
  return 1
}

if [[ ! -f "$SERVICE_SCRIPT" ]]; then
  echo "[brew-outdated-check] Missing backend service script at: $SERVICE_SCRIPT" >&2
  exit 1
fi

select_python_314
exec "$PYTHON_BIN" "$SERVICE_SCRIPT" --check-only --notify
