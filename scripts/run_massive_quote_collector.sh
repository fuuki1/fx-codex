#!/bin/sh
# Load a private Massive API key and run the read-only market-data collector.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${FX_CODEX_COLLECTOR_PYTHON:-"$ROOT/.venv/bin/python"}
ENV_FILE=${FX_CODEX_MASSIVE_ENV_FILE:-"$HOME/.config/fx-codex/massive-collector.env"}
MODE=manual

if [ "${1:-}" = "--launchd" ]; then
  MODE=launchd
  shift
fi

finish_expected_stop() {
  code=$1
  message=$2
  echo "[massive-collector-wrapper] $message" >&2
  if [ "$MODE" = launchd ]; then
    exit 0
  fi
  exit "$code"
}

if [ ! -f "$ENV_FILE" ]; then
  finish_expected_stop 78 "configuration file missing: $ENV_FILE"
fi
if [ ! -x "$PYTHON" ]; then
  echo "[massive-collector-wrapper] approved Python is not executable" >&2
  exit 70
fi

case "$(uname -s)" in
  Darwin) permissions=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || printf 'unknown') ;;
  *) permissions=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || printf 'unknown') ;;
esac
if [ "$permissions" != "600" ]; then
  finish_expected_stop 78 "configuration file must be mode 600 (observed: $permissions)"
fi

set +e
"$PYTHON" "$ROOT/tools/fx_massive_quote_collector.py" --env-file "$ENV_FILE" "$@"
code=$?
set -e

case "$code" in
  75) finish_expected_stop "$code" "duplicate quote writer rejected" ;;
  77) finish_expected_stop "$code" "API key or plan authorization rejected" ;;
  78) finish_expected_stop "$code" "configuration rejected" ;;
  *) exit "$code" ;;
esac
