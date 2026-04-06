#!/bin/bash
set -euo pipefail

# Backward-compat alias kept inside the project: UI now means desktop app.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${PROJECT_ROOT}/tools/open-brew-updates-app.sh"
