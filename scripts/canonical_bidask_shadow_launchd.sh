#!/bin/zsh
# Install the analysis-only canonical bid/ask shadow topology on macOS.
#
# This deliberately does not replace com.fx-codex.snapshot or feed the
# canonical output into briefing decisions. Promotion remains a separate,
# evidence-gated change.
set -euo pipefail
setopt NULL_GLOB

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PYTHON="$ROOT/.venv/bin/python"
COLLECTOR_LABEL="com.fx-codex.quote-collector"
LABELS=(
  com.fx-codex.bidask-materializer
  com.fx-codex.bidask-health
)
CMD="${1:-status}"

render_plist() {
  local label="$1"
  sed \
    -e "s|__ROOT__|$ROOT|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    "$ROOT/ops/launchd/$label.plist.tmpl"
}

loaded() {
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

validate_release() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    echo "拒否: launchd topologyはmacOS専用です" >&2
    return 70
  }
  [[ -x "$PYTHON" ]] || {
    echo "拒否: approved Pythonが実行できません: $PYTHON" >&2
    return 70
  }
  local approved_sha="${FX_CODEX_APPROVED_SHA:-}"
  [[ "${#approved_sha}" == 40 ]] && [[ -z "${approved_sha//[0-9a-f]/}" ]] || {
    echo "拒否: FX_CODEX_APPROVED_SHA にレビュー済み40桁commit SHAを指定してください" >&2
    return 78
  }
  local head resolved
  head="$(git -C "$ROOT" rev-parse HEAD)"
  resolved="$(git -C "$ROOT" rev-parse --verify "$approved_sha^{commit}" 2>/dev/null)" || {
    echo "拒否: approved SHAを解決できません" >&2
    return 78
  }
  [[ "$head" == "$resolved" ]] || {
    echo "拒否: runtime HEADがapproved SHAと一致しません" >&2
    return 78
  }
  [[ -z "$(git -C "$ROOT" status --porcelain=v1 --untracked-files=all)" ]] || {
    echo "拒否: dirty checkoutから常駐サービスを導入できません" >&2
    return 78
  }

  local checkout resolved_checkout legacy_paths
  for checkout in "$HOME/Desktop/fx-codex" "$HOME/fx-codex"; do
    [[ -d "$checkout" ]] || continue
    resolved_checkout="$(cd "$checkout" 2>/dev/null && pwd -P)" || continue
    [[ "$resolved_checkout" == "$(cd "$ROOT" && pwd -P)" ]] && continue
    git -C "$checkout" rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue
    legacy_paths="$(
      git -C "$checkout" ls-files \
        'trader/**' \
        'executor.py' \
        'app/executor.py' \
        '*/app/executor.py'
    )"
    [[ -z "$legacy_paths" ]] || {
      echo "拒否: legacy execution checkoutを先に隔離してください: $checkout" >&2
      return 78
    }
  done

  "$ROOT/scripts/quote_collector_launchd.sh" dry-run >/dev/null
  for label in $LABELS; do
    [[ -f "$ROOT/ops/launchd/$label.plist.tmpl" ]] || {
      echo "拒否: plist templateがありません: $label" >&2
      return 70
    }
    render_plist "$label" | plutil -lint - >/dev/null
    render_plist "$label" | "$PYTHON" -c '
import plistlib
import sys
from tools.run_exclusive import build_parser

arguments = plistlib.loads(sys.stdin.buffer.read())["ProgramArguments"]
separator = arguments.index("--")
build_parser().parse_args([*arguments[2:separator], "--", "/usr/bin/true"])
'
  done
}

write_plist_atomically() {
  local label="$1"
  local plist="$AGENTS_DIR/$label.plist"
  local temporary="$plist.tmp.$$"
  trap 'rm -f "$temporary"' EXIT INT TERM
  render_plist "$label" > "$temporary"
  plutil -lint "$temporary" >/dev/null
  chmod 600 "$temporary"
  mv -f "$temporary" "$plist"
  trap - EXIT INT TERM
}

install_label() {
  local label="$1"
  local plist="$AGENTS_DIR/$label.plist"
  write_plist_atomically "$label"
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  launchctl enable "gui/$(id -u)/$label"
  launchctl print "gui/$(id -u)/$label" >/dev/null
}

remove_label() {
  local label="$1"
  local plist="$AGENTS_DIR/$label.plist"
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  rm -f "$plist"
}

case "$CMD" in
  dry-run)
    validate_release
    echo "dry-run passed: approved clean SHA, collector config, legacy isolation, plist syntax"
    ;;
  install)
    validate_release
    for label in "$COLLECTOR_LABEL" $LABELS; do
      if loaded "$label" || [[ -e "$AGENTS_DIR/$label.plist" ]]; then
        echo "拒否: 既存label/plistを上書きしません。先に明示的にuninstallしてください: $label" >&2
        exit 78
      fi
    done
    mkdir -p "$AGENTS_DIR" "$ROOT/collect/logs" "$ROOT/collect/state" "$ROOT/logs/locks"
    "$ROOT/scripts/quote_collector_launchd.sh" install
    installed_labels=()
    for label in $LABELS; do
      if ! install_label "$label"; then
        remove_label "$label"
        for installed in $installed_labels; do
          remove_label "$installed"
        done
        "$ROOT/scripts/quote_collector_launchd.sh" uninstall
        echo "導入失敗: 新規に追加したcanonical shadowサービスをrollbackしました" >&2
        exit 70
      fi
      installed_labels+=("$label")
    done
    echo "installed: $COLLECTOR_LABEL ${LABELS[*]}"
    ;;
  status)
    for label in "$COLLECTOR_LABEL" $LABELS; do
      if loaded "$label"; then
        state="$(
          launchctl print "gui/$(id -u)/$label" 2>/dev/null \
            | grep -E "state =|last exit" \
            | head -2 \
            | tr -s " " \
            | tr "\n" " "
        )"
        echo "$label: LOADED ($state)"
      else
        echo "$label: NOT LOADED"
      fi
    done
    report="$ROOT/logs/canonical_bidask_freshness_report.json"
    [[ -f "$report" ]] && "$PYTHON" -m json.tool "$report"
    ;;
  uninstall|rollback)
    for label in $LABELS; do
      remove_label "$label"
    done
    "$ROOT/scripts/quote_collector_launchd.sh" uninstall
    echo "removed canonical shadow services; collect/log/state data was retained"
    ;;
  *)
    echo "usage: $0 {dry-run|install|status|uninstall|rollback}" >&2
    exit 64
    ;;
esac
