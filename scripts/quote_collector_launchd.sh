#!/bin/zsh
# Read-only quote collector の launchd 管理 (install / uninstall / status / dry-run / rollback)
#
#   ./scripts/quote_collector_launchd.sh dry-run    # plist生成+daemon設定検証のみ(変更なし)
#   ./scripts/quote_collector_launchd.sh install    # LaunchAgentとして常駐化
#   ./scripts/quote_collector_launchd.sh status     # 稼働状態と直近runの表示
#   ./scripts/quote_collector_launchd.sh uninstall  # bootout+plist撤去(データは残す)
#   ./scripts/quote_collector_launchd.sh rollback   # uninstallと同義(収集停止)。raw/logは削除しない
#
# 設計は既存の install_launchd.sh と同じ:
# - LaunchAgent(gui/$UID)。Mac miniは自動ログイン運用前提
# - plistに秘密情報を書かない。FX_OANDA_* は ~/.config/fx-codex/collector.env
#   (chmod 600) から wrapper が読み込む。.envはgit管理外
# - 多重writer防止は daemon 側の ExclusiveLock (EX_TEMPFAIL=75)
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="com.fx-codex.quote-collector"
TMPL="$ROOT/ops/launchd/$LABEL.plist.tmpl"
PLIST="$AGENTS_DIR/$LABEL.plist"
ENV_FILE="$HOME/.config/fx-codex/collector.env"
CMD="${1:-status}"

render_plist() {
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PYTHON|g" -e "s|__HOME__|$HOME|g" "$TMPL"
}

case "$CMD" in
  dry-run)
    echo "== rendered plist =="
    render_plist
    echo "== plist syntax =="
    render_plist | plutil -lint - || exit 1
    echo "== daemon config check (credentials names only; values never printed) =="
    if [ -f "$ENV_FILE" ]; then
      set -a; source "$ENV_FILE"; set +a
    fi
    "$PYTHON" "$ROOT/tools/fx_quote_collector.py" --output-root "$HOME/srv/fx-codex/collect" --dry-run
    rc=$?
    [ $rc -eq 78 ] && echo "(EX_CONFIG: credentials未設定。$ENV_FILE に FX_OANDA_API_TOKEN / FX_OANDA_ACCOUNT_ID / FX_OANDA_ENV を chmod 600 で置く)"
    exit 0
    ;;
  install)
    mkdir -p "$AGENTS_DIR" "$HOME/srv/fx-codex/collect/logs"
    if [ ! -f "$ENV_FILE" ]; then
      echo "拒否: $ENV_FILE がありません(credentials未設定でinstallしない=fail-closed)" >&2
      exit 78
    fi
    perms=$(stat -f '%Lp' "$ENV_FILE")
    if [ "$perms" != "600" ]; then
      echo "拒否: $ENV_FILE は chmod 600 が必須(現在 $perms)" >&2
      exit 78
    fi
    render_plist > "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl enable "gui/$(id -u)/$LABEL"
    echo "installed: $LABEL"
    ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | head -20 || echo "not loaded"
    LAST="$HOME/srv/fx-codex/collect/state/last_run.json"
    [ -f "$LAST" ] && { echo "== last run =="; cat "$LAST"; }
    ;;
  uninstall|rollback)
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL (raw/logデータは $HOME/srv/fx-codex/collect に保持)"
    ;;
  *)
    echo "usage: $0 {dry-run|install|uninstall|status|rollback}" >&2
    exit 64
    ;;
esac
