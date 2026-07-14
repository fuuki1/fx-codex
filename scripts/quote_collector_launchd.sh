#!/bin/zsh
# Read-only quote collector launchd management.
#
#   ./scripts/quote_collector_launchd.sh dry-run
#   ./scripts/quote_collector_launchd.sh install
#   ./scripts/quote_collector_launchd.sh status
#   ./scripts/quote_collector_launchd.sh uninstall
#   ./scripts/quote_collector_launchd.sh rollback
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="com.fx-codex.quote-collector"
TMPL="$ROOT/ops/launchd/$LABEL.plist.tmpl"
PLIST="$AGENTS_DIR/$LABEL.plist"
WRAPPER="$ROOT/scripts/run_quote_collector.sh"
ENV_FILE="${FX_CODEX_COLLECTOR_ENV_FILE:-$HOME/.config/fx-codex/collector.env}"
OUTPUT_ROOT="$HOME/srv/fx-codex/collect"
CMD="${1:-status}"

render_plist() {
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$TMPL"
}

validate_files() {
  [[ -f "$WRAPPER" ]] || {
    echo "拒否: wrapperがありません: $WRAPPER" >&2
    return 70
  }
  [[ -f "$ENV_FILE" ]] || {
    echo "拒否: $ENV_FILE がありません" >&2
    return 78
  }
  local permissions
  permissions="$(stat -f '%Lp' "$ENV_FILE")"
  [[ "$permissions" == "600" ]] || {
    echo "拒否: $ENV_FILE は chmod 600 が必須(現在 $permissions)" >&2
    return 78
  }
}

validate_runtime() {
  FX_CODEX_COLLECTOR_ENV_FILE="$ENV_FILE" \
    /bin/sh "$WRAPPER" --output-root "$OUTPUT_ROOT" --dry-run >/dev/null
}

write_plist_atomically() {
  mkdir -p "$AGENTS_DIR"
  local temporary="$PLIST.tmp.$$"
  trap 'rm -f "$temporary"' EXIT INT TERM
  render_plist > "$temporary"
  plutil -lint "$temporary" >/dev/null
  chmod 600 "$temporary"
  mv -f "$temporary" "$PLIST"
  trap - EXIT INT TERM
}

case "$CMD" in
  dry-run)
    echo "== rendered plist =="
    render_plist
    echo "== plist syntax =="
    render_plist | plutil -lint -
    echo "== wrapper/config validation =="
    validate_files
    validate_runtime
    echo "dry-run passed: credentials were loaded without printing their values"
    ;;
  install)
    validate_files
    validate_runtime
    mkdir -p "$OUTPUT_ROOT/logs"
    write_plist_atomically
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl enable "gui/$(id -u)/$LABEL"
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null
    echo "installed and verified: $LABEL"
    ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | head -40 || echo "not loaded"
    LAST="$OUTPUT_ROOT/state/last_run.json"
    [[ -f "$LAST" ]] && { echo "== last terminal state =="; cat "$LAST"; }
    INCIDENTS="$OUTPUT_ROOT/state/incidents"
    if [[ -d "$INCIDENTS" ]]; then
      local_incidents=("$INCIDENTS"/*.json(Nom))
      if (( ${#local_incidents} > 0 )); then
        echo "== latest incidents =="
        print -l -- "${local_incidents[1,5]}"
      fi
    fi
    ;;
  uninstall|rollback)
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL (raw/log data retained under $OUTPUT_ROOT)"
    ;;
  *)
    echo "usage: $0 {dry-run|install|uninstall|status|rollback}" >&2
    exit 64
    ;;
esac
