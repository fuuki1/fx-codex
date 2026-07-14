#!/bin/sh
# Load the private collector environment, enforce its permissions, then exec the
# read-only Python collector. In --launchd mode, expected human-action stops are
# translated to exit 0 so launchd does not create a restart loop.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${FX_CODEX_COLLECTOR_PYTHON:-"$ROOT/.venv/bin/python"}
ENV_FILE=${FX_CODEX_COLLECTOR_ENV_FILE:-"$HOME/.config/fx-codex/collector.env"}
MODE=manual

if [ "${1:-}" = "--launchd" ]; then
  MODE=launchd
  shift
fi

finish_expected_stop() {
  code=$1
  message=$2
  echo "[collector-wrapper] $message" >&2
  if [ "$MODE" = launchd ]; then
    exit 0
  fi
  exit "$code"
}

if [ ! -x "$PYTHON" ]; then
  PYTHON=$(command -v python3 || true)
fi
if [ -z "${PYTHON:-}" ] || [ ! -x "$PYTHON" ]; then
  echo "[collector-wrapper] no usable Python interpreter" >&2
  exit 70
fi

if [ ! -f "$ENV_FILE" ]; then
  finish_expected_stop 78 "configuration file missing: $ENV_FILE"
fi

case "$(uname -s)" in
  Darwin)
    permissions=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || printf 'unknown')
    ;;
  *)
    permissions=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || printf 'unknown')
    ;;
esac
if [ "$permissions" != "600" ]; then
  finish_expected_stop 78 "configuration file must be mode 600 (observed: $permissions)"
fi

set +e
"$PYTHON" "$ROOT/tools/fx_quote_collector.py" --env-file "$ENV_FILE" "$@"
code=$?
set -e

case "$code" in
  75)
    finish_expected_stop "$code" "duplicate writer rejected; leaving the active writer untouched"
    ;;
  77)
    finish_expected_stop "$code" "authorization rejected; update credentials before restarting"
    ;;
  78)
    finish_expected_stop "$code" "configuration rejected; correct collector.env before restarting"
    ;;
  *)
    exit "$code"
    ;;
esac
