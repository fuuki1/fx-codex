#!/bin/zsh
# Manage the optional operational-store shadow services without touching the
# existing dashboard or the default fx-codex launchd service set.
#
# Required for dry-run/install:
#   FX_OPERATIONAL_PYTHON=/absolute/path/to/approved/python
#   FX_OPERATIONAL_DB=/absolute/path/to/candidate.sqlite3
#   FX_OPERATIONAL_EVIDENCE_DIR=/absolute/path/to/create-only/reports
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.operational-sync com.fx-codex.operational-replay)
COMMAND="${1:-status}"

OPERATIONAL_PYTHON="${FX_OPERATIONAL_PYTHON:-}"
OPERATIONAL_DB="${FX_OPERATIONAL_DB:-}"
EVIDENCE_DIR="${FX_OPERATIONAL_EVIDENCE_DIR:-}"

fail() {
  print -u2 -- "拒否: $1"
  return "${2:-78}"
}

require_absolute_safe_path() {
  local name="$1"
  local value="$2"
  [[ -n "$value" ]] || fail "$name が未設定です"
  [[ "$value" == /* ]] || fail "$name は絶対パスで指定してください: $value"
  [[ "$value" != *[\&\<\>\|\\]* ]] || fail "$name にrender上安全でない文字があります"
}

validate_configuration() {
  require_absolute_safe_path FX_OPERATIONAL_PYTHON "$OPERATIONAL_PYTHON"
  require_absolute_safe_path FX_OPERATIONAL_DB "$OPERATIONAL_DB"
  require_absolute_safe_path FX_OPERATIONAL_EVIDENCE_DIR "$EVIDENCE_DIR"
  [[ -x "$OPERATIONAL_PYTHON" ]] || fail "承認済みPythonを実行できません: $OPERATIONAL_PYTHON"
  [[ -f "$OPERATIONAL_DB" ]] || fail "候補DBがありません: $OPERATIONAL_DB"
  [[ -f "$ROOT/logs/briefing_decisions.jsonl" ]] || fail "decision rawがありません"
  [[ -f "$ROOT/logs/briefing_tf_prices.jsonl" ]] || fail "price rawがありません"
}

render_plist() {
  local label="$1"
  local template="$ROOT/ops/launchd/$label.plist.tmpl"
  [[ -f "$template" ]] || fail "launchd templateがありません: $template" 70
  sed \
    -e "s|__FX_ROOT__|$ROOT|g" \
    -e "s|__OPERATIONAL_PYTHON__|$OPERATIONAL_PYTHON|g" \
    -e "s|__OPERATIONAL_DB__|$OPERATIONAL_DB|g" \
    -e "s|__OPERATIONAL_EVIDENCE_DIR__|$EVIDENCE_DIR|g" \
    "$template"
}

preflight() {
  validate_configuration
  "$OPERATIONAL_PYTHON" "$ROOT/tools/operational_store.py" \
    --db "$OPERATIONAL_DB" audit >/dev/null
  for label in $LABELS; do
    render_plist "$label" | plutil -lint - >/dev/null
  done
}

show_status() {
  local label
  for label in $LABELS; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      print -- "$label: loaded"
    else
      print -- "$label: not loaded"
    fi
  done
}

case "$COMMAND" in
  dry-run)
    preflight
    for label in $LABELS; do
      print -- "== $label =="
      render_plist "$label"
    done
    print -- "dry-run passed; launchd state was not changed"
    ;;
  install)
    preflight
    mkdir -p "$AGENTS_DIR" "$ROOT/logs/launchd" "$ROOT/logs/locks" "$EVIDENCE_DIR"
    for label in $LABELS; do
      target="$AGENTS_DIR/$label.plist"
      if [[ -e "$target" ]] || launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        fail "$label は既に配置またはload済みです。先にstatusを確認してください" 73
      fi
    done

    installed=()
    rollback_partial_install() {
      local stamp
      stamp="$(date -u +%Y%m%dT%H%M%SZ)"
      for label in $installed; do
        target="$AGENTS_DIR/$label.plist"
        launchctl bootout "gui/$(id -u)" "$target" >/dev/null 2>&1 || true
        if [[ -f "$target" ]]; then
          mv "$target" "$target.failed-$stamp"
        fi
      done
    }
    trap rollback_partial_install EXIT INT TERM

    for label in $LABELS; do
      target="$AGENTS_DIR/$label.plist"
      candidate="$target.candidate.$$"
      render_plist "$label" > "$candidate"
      plutil -lint "$candidate" >/dev/null
      chmod 600 "$candidate"
      mv "$candidate" "$target"
      installed+=("$label")
      launchctl bootstrap "gui/$(id -u)" "$target"
      launchctl print "gui/$(id -u)/$label" >/dev/null
    done
    trap - EXIT INT TERM
    print -- "installed: ${LABELS[*]}"
    ;;
  status)
    show_status
    ;;
  uninstall|rollback)
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    for label in $LABELS; do
      target="$AGENTS_DIR/$label.plist"
      launchctl bootout "gui/$(id -u)" "$target" >/dev/null 2>&1 || true
      if [[ -f "$target" ]]; then
        archived="$target.disabled-$stamp"
        mv "$target" "$archived"
        print -- "unloaded and archived: $archived"
      else
        print -- "not installed: $label"
      fi
    done
    print -- "database, raw evidence, and reports were retained"
    ;;
  *)
    print -u2 -- "usage: $0 {dry-run|install|status|uninstall|rollback}"
    exit 64
    ;;
esac
