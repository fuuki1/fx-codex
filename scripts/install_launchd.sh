#!/bin/zsh
# fx-codexの学習データ収集とDiscord通知をlaunchd常駐サービスとしてインストールする。
#
#   ./scripts/install_launchd.sh            # インストール(既存の同名サービスは置換)
#   ./scripts/install_launchd.sh --dry-run  # 生成されるplistを表示するだけ(変更なし)
#
# インストールされるLaunchAgent(gui/$UID):
#   com.fx-codex.snapshot  5分毎の価格スナップショット
#   com.fx-codex.briefing  5分毎のFX統合ブリーフィングDiscord通知
#   com.fx-codex.health    5分毎のデータ鮮度監視+異常時Discord通知
#   com.fx-codex.horizon   5分毎の9ホライズンshadow予測
#   com.fx-codex.monitors  15分毎の学習cache bundle公開
#   com.fx-codex.monitor-diagnostics  15分毎の期待値・改善候補監視
#   com.fx-codex.dukascopy-history / quote-index  仮想約定用の遅延Bid/Askと索引
#   com.fx-codex.virtual-portfolio  3分毎のcanonical仮想口座受付
#   com.fx-codex.virtual-portfolio-postprocess / report / counterfactual  分離した後段処理
#   com.fx-codex.virtual-portfolio-learning / read  週次検証・query-only API
#
# 設計メモ:
# - LaunchAgent(ユーザーセッション)を使う。LaunchDaemonにしないのは、.envや
#   logs/がユーザー所有で、GUIログインセッションのTCC権限に依存するため。
#   注意: LaunchAgentはユーザーがログインしている間だけ動く。Mac miniは
#   自動ログイン運用(再起動→自動ログイン→エージェント自動起動)を前提とする。
# - plistへ秘密情報を書かない。Discord URLは実行時に.envから読まれる。
# - 旧 com.fx-codex.briefing.hourly が居れば置き換え(bootout)する。
# - 競合writer/loopを検知したらインストールを拒否する(自動killはしない)。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(
  com.fx-codex.snapshot
  com.fx-codex.briefing
  com.fx-codex.health
  com.fx-codex.horizon
  com.fx-codex.monitors
  com.fx-codex.monitor-diagnostics
  com.fx-codex.dukascopy-history
  com.fx-codex.quote-index
  com.fx-codex.virtual-portfolio
  com.fx-codex.virtual-portfolio-postprocess
  com.fx-codex.virtual-portfolio-report
  com.fx-codex.virtual-portfolio-counterfactual
  com.fx-codex.virtual-portfolio-learning
  com.fx-codex.virtual-portfolio-read
  com.fx-codex.dashboard-state
  com.fx-codex.operational-read
)
LEGACY_LABELS=(com.fx-codex.briefing.hourly com.fx-codex.virtual-portfolio-close)
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

render() {
  # __FX_ROOT__/__PYTHON__を実パスへ展開してplistを生成する
  sed -e "s|__FX_ROOT__|$ROOT|g" -e "s|__PYTHON__|$PYTHON|g" "$1"
}

has_managed_ancestor() {
  local child_pid="$1"
  local hops=0
  local parent_pid
  local parent_command
  while [ "$hops" -lt 4 ]; do
    parent_pid="$(ps -o ppid= -p "$child_pid" 2>/dev/null | tr -d ' ')"
    [ -n "$parent_pid" ] || return 1
    [ "$parent_pid" -gt 1 ] 2>/dev/null || return 1
    parent_command="$(ps -o command= -p "$parent_pid" 2>/dev/null || true)"
    case "$parent_command" in
      *"/tools/run_exclusive.py"*) return 0 ;;
    esac
    child_pid="$parent_pid"
    hops=$((hops + 1))
  done
  return 1
}

unmanaged_direct_writers() {
  local pid
  local command
  for pid in $(pgrep -f "[p]ython.*(fx_briefing.py|fx_tf_snapshot.py|virtual_portfolio.py)" 2>/dev/null || true); do
    if has_managed_ancestor "$pid"; then
      continue
    fi
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    [ -z "$command" ] || echo "$pid $command"
  done
}

echo "fx-codex root : $ROOT"
echo "python        : $PYTHON"
echo "agents dir    : $AGENTS_DIR"

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: 実行可能なpythonが見つかりません" >&2
  exit 1
fi

if [ "$DRY_RUN" != 1 ]; then
  loops=$(pgrep -fl "fx_briefing_loop.sh|fx_tf_snapshot_loop.sh" || true)
  direct_writers=$(unmanaged_direct_writers)
  cron_writers=$(crontab -l 2>/dev/null | grep -E "fx_briefing.py|fx_tf_snapshot.py|virtual_portfolio.py" || true)
  if [ -n "$loops$direct_writers$cron_writers" ]; then
    echo "ERROR: 競合する手動/cron writerを検知したためインストールを中止します。" >&2
    [ -z "$loops" ] || echo "$loops" >&2
    [ -z "$direct_writers" ] || echo "$direct_writers" >&2
    [ -z "$cron_writers" ] || echo "$cron_writers" >&2
    echo "人間が対象を確認・停止し、監査証跡を保存してから再実行してください。" >&2
    exit 2
  fi
fi

# 変更前に全テンプレートの存在を検証する。
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
done

if [ "$DRY_RUN" = 1 ]; then
  echo "(dry-run: 変更は行っていません)"
  exit 0
fi

# legacy writerを新serviceより先に停止し、残存を検証する。
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      echo "ERROR: legacy bootout失敗: $label" >&2
      exit 2
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "ERROR: legacy serviceがbootout後も残存: $label" >&2
      exit 2
    fi
    echo "legacy bootout: $label"
  fi
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    disabled="$AGENTS_DIR/$label.plist.disabled-$(date +%Y%m%d%H%M%S)"
    if ! mv "$AGENTS_DIR/$label.plist" "$disabled"; then
      echo "ERROR: legacy plistを退避できません: $label" >&2
      exit 2
    fi
    echo "legacy plist退避: $disabled"
  fi
done

for label in $LABELS; do
  tmpl="$ROOT/ops/launchd/$label.plist.tmpl"
  mkdir -p "$AGENTS_DIR" "$ROOT/logs/launchd" "$ROOT/logs/locks"
  target="$AGENTS_DIR/$label.plist"
  candidate="$target.candidate.$$"
  if ! render "$tmpl" > "$candidate"; then
    rm -f "$candidate"
    echo "ERROR: plist生成失敗: $label" >&2
    exit 1
  fi
  if command -v plutil >/dev/null; then
    plutil -lint -s "$candidate" || {
      rm -f "$candidate"
      echo "ERROR: plistが不正: $candidate" >&2
      exit 1
    }
  fi
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      rm -f "$candidate"
      echo "ERROR: 既存serviceのbootout失敗: $label" >&2
      exit 2
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      rm -f "$candidate"
      echo "ERROR: bootout後もserviceが残存: $label" >&2
      exit 2
    fi
  fi
  if ! mv "$candidate" "$target"; then
    rm -f "$candidate"
    echo "ERROR: plist配置失敗: $target" >&2
    exit 1
  fi
  launchctl bootstrap "gui/$(id -u)" "$target" || {
    echo "ERROR: bootstrap失敗: $label" >&2
    exit 1
  }
  echo "installed: $label"
done

echo ""
echo "完了。状態確認: ./scripts/status_fx_services.sh"
