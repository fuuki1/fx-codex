#!/bin/zsh
# fx-codexの学習データ収集をlaunchd常駐サービスとしてインストールする。
#
#   ./scripts/install_launchd.sh            # インストール(既存の同名サービスは置換)
#   ./scripts/install_launchd.sh --dry-run  # 生成されるplistを表示するだけ(変更なし)
#
# インストールされるLaunchAgent(gui/$UID):
#   com.fx-codex.snapshot  5分毎の価格スナップショット
#   com.fx-codex.briefing  毎時:10のブリーフィング(融合+時間足別)
#   com.fx-codex.health    5分毎のデータ鮮度監視+Discord通知
#
# 設計メモ:
# - LaunchAgent(ユーザーセッション)を使う。LaunchDaemonにしないのは、.envや
#   logs/がユーザー所有で、GUIログインセッションのTCC権限に依存するため。
#   注意: LaunchAgentはユーザーがログインしている間だけ動く。Mac miniは
#   自動ログイン運用(再起動→自動ログイン→エージェント自動起動)を前提とする。
# - plistへ秘密情報を書かない。Discord URLは実行時に.envから読まれる。
# - 旧 com.fx-codex.briefing.hourly が居れば置き換え(bootout)する。
# - 手動起動の fx_*_loop.sh が動いていれば警告する(自動killはしない)。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

render() {
  # __FX_ROOT__/__PYTHON__を実パスへ展開してplistを生成する
  sed -e "s|__FX_ROOT__|$ROOT|g" -e "s|__PYTHON__|$PYTHON|g" "$1"
}

echo "fx-codex root : $ROOT"
echo "python        : $PYTHON"
echo "agents dir    : $AGENTS_DIR"

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: 実行可能なpythonが見つかりません" >&2
  exit 1
fi

for label in $LABELS; do
  tmpl="$ROOT/ops/launchd/$label.plist.tmpl"
  if [ ! -f "$tmpl" ]; then
    echo "ERROR: テンプレートがありません: $tmpl" >&2
    exit 1
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "--- $label (dry-run: 生成内容) ---"
    render "$tmpl"
    continue
  fi
  mkdir -p "$AGENTS_DIR" "$ROOT/logs/launchd" "$ROOT/logs/locks"
  target="$AGENTS_DIR/$label.plist"
  render "$tmpl" > "$target"
  if command -v plutil >/dev/null; then
    plutil -lint -s "$target" || { echo "ERROR: plistが不正: $target" >&2; exit 1; }
  fi
  # 既にロード済みなら一旦bootout(置換のため)。未ロードのエラーは無視
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$target" || {
    echo "ERROR: bootstrap失敗: $label" >&2
    exit 1
  }
  echo "installed: $label"
done

if [ "$DRY_RUN" = 1 ]; then
  echo "(dry-run: 変更は行っていません)"
  exit 0
fi

# 旧サービスの置き換え
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$label" && echo "legacy bootout: $label"
  fi
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    mv "$AGENTS_DIR/$label.plist" "$AGENTS_DIR/$label.plist.disabled-$(date +%Y%m%d)"
    echo "legacy plist退避: $label"
  fi
done

# 多重起動源の検知(自動killはしない: 人間が確認して止める)
loops=$(pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh" || true)
if [ -n "$loops" ]; then
  echo ""
  echo "⚠️  手動起動のループが動いています。launchdと二重実行になるため停止してください:"
  echo "$loops"
  echo "    停止コマンド: pkill -f 'fx_briefing_loop.sh|fx_tf_snapshot_loop.sh'"
fi

echo ""
echo "完了。状態確認: ./scripts/status_fx_services.sh"
