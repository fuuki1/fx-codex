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
# - 競合writer/loopを検知したらインストールを拒否する(自動killはしない)。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFLIGHT_LIB="$ROOT/scripts/writer_preflight.sh"
if [ ! -r "$PREFLIGHT_LIB" ]; then
  echo "ERROR: writer preflight libraryがありません: $PREFLIGHT_LIB" >&2
  exit 1
fi
source "$PREFLIGHT_LIB"
PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(com.fx-codex.snapshot com.fx-codex.briefing com.fx-codex.health)
LEGACY_LABELS=(com.fx-codex.briefing.hourly)
ALL_LABELS=($LABELS $LEGACY_LABELS)
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

render() {
  # __FX_ROOT__/__PYTHON__を実パスへ展開してplistを生成する
  sed -e "s|__FX_ROOT__|$ROOT|g" -e "s|__PYTHON__|$PYTHON|g" "$1"
}

lint_plist() {
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint -s "$1"
  else
    # CI/Linuxでplutilが無い場合も、実行runtimeの標準ライブラリで
    # XML plistとしてparseできることを変更前に検証する。
    "$PYTHON" -c 'import plistlib, sys; plistlib.load(open(sys.argv[1], "rb"))' "$1"
  fi
}

STAGE_DIR=""
MUTATION_STARTED=0

cleanup_stage() {
  if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
    rm -rf "$STAGE_DIR"
  fi
}

rollback_install() {
  local rollback_status=0
  local label target backup restore disabled_marker disabled_path
  echo "ROLLBACK: 部分導入を停止し、plistを変更前の状態へ戻します。" >&2

  # 新旧を問わず対象labelをすべて停止し、部分writerを残さない。
  for label in $ALL_LABELS; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      if ! launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1; then
        echo "CRITICAL: rollback bootout失敗: $label" >&2
        rollback_status=2
        continue
      fi
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        echo "CRITICAL: rollback後もserviceが残存: $label" >&2
        rollback_status=2
      fi
    fi
  done

  # 全labelのbackupは任意のmutationより前に取得済み。元ファイルが
  # 無かったlabelは新規plistを削除する。旧serviceは自動再起動しない。
  for label in $ALL_LABELS; do
    target="$AGENTS_DIR/$label.plist"
    backup="$STAGE_DIR/$label.plist.previous"
    if [ -f "$backup" ]; then
      restore="$target.rollback.$$"
      if ! cp -p "$backup" "$restore" || ! mv -f "$restore" "$target"; then
        rm -f "$restore"
        echo "CRITICAL: 旧plist復元失敗: $target" >&2
        rollback_status=2
      fi
    elif ! rm -f "$target"; then
      echo "CRITICAL: 新規plist削除失敗: $target" >&2
      rollback_status=2
    fi
  done

  for label in $LEGACY_LABELS; do
    disabled_marker="$STAGE_DIR/$label.disabled-path"
    if [ -f "$disabled_marker" ]; then
      disabled_path="$(<"$disabled_marker")"
      case "$disabled_path" in
        "$AGENTS_DIR/$label.plist.disabled-"*)
          if ! rm -f "$disabled_path"; then
            echo "CRITICAL: legacy disabled copy削除失敗: $disabled_path" >&2
            rollback_status=2
          fi
          ;;
        *)
          echo "CRITICAL: legacy disabled pathが許可範囲外: $disabled_path" >&2
          rollback_status=2
          ;;
      esac
    fi
  done
  if [ "$rollback_status" -eq 0 ]; then
    cleanup_stage
  else
    echo "CRITICAL: rollback backupを保全します: $STAGE_DIR" >&2
  fi
  MUTATION_STARTED=0
  if [ "$rollback_status" -eq 0 ]; then
    echo "ROLLBACK: 対象3serviceは停止済み。自動再起動はしません。" >&2
  fi
  return "$rollback_status"
}

abort_install() {
  local code="$1"
  shift
  echo "ERROR: $*" >&2
  if [ "$MUTATION_STARTED" = 1 ]; then
    if ! rollback_install; then
      code=2
    fi
  else
    cleanup_stage
  fi
  trap - HUP INT TERM
  exit "$code"
}

abort_on_signal() {
  local code="$1"
  local signal_name="$2"
  abort_install "$code" "$signal_nameを受信したため導入を中止"
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
  direct_writers=$(pgrep -fl "[p]ython.*(fx_briefing.py|fx_tf_snapshot.py)" || true)
  wrapper_writers=$(pgrep -fl "[f]x_briefing_once.sh|[r]un_exclusive.py.*fx-(briefing|snapshot)" || true)
  cron_stdout=$(mktemp "${TMPDIR:-/tmp}/fx-codex-crontab.XXXXXX") || exit 1
  cron_stderr=$(mktemp "${TMPDIR:-/tmp}/fx-codex-crontab-error.XXXXXX") || {
    rm -f "$cron_stdout"
    exit 1
  }
  crontab -l > "$cron_stdout" 2> "$cron_stderr"
  cron_status=$?
  if [ "$cron_status" -eq 0 ]; then
    :
  elif fx_crontab_is_absent "$cron_status" "$cron_stderr"; then
    : > "$cron_stdout"
  else
    echo "ERROR: crontabを検証できないためインストールを中止します。" >&2
    sed -n '1,3p' "$cron_stderr" >&2
    rm -f "$cron_stdout" "$cron_stderr"
    exit 2
  fi
  cron_writers=$(fx_filter_writer_lines < "$cron_stdout" || true)
  rm -f "$cron_stdout" "$cron_stderr"

  dormant_writers=""
  if [ -d "$AGENTS_DIR" ]; then
    for candidate in "$AGENTS_DIR"/*.plist(N); do
      case "${candidate:t}" in
        com.fx-codex.snapshot.plist|com.fx-codex.briefing.plist|com.fx-codex.health.plist|com.fx-codex.briefing.hourly.plist) ;;
        *)
          if fx_file_has_writer_signature "$candidate"; then
            dormant_writers+="$candidate\n"
          fi
          ;;
      esac
    done
  fi
  if [ -n "$loops$direct_writers$wrapper_writers$cron_writers$dormant_writers" ]; then
    echo "ERROR: 競合する手動/cron writerを検知したためインストールを中止します。" >&2
    [ -z "$loops" ] || echo "$loops" >&2
    [ -z "$direct_writers" ] || echo "$direct_writers" >&2
    [ -z "$wrapper_writers" ] || echo "$wrapper_writers" >&2
    [ -z "$cron_writers" ] || echo "$cron_writers" >&2
    [ -z "$dormant_writers" ] || printf '%b' "$dormant_writers" >&2
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

mkdir -p "$AGENTS_DIR" "$ROOT/logs/launchd" "$ROOT/logs/locks" || {
  echo "ERROR: 必要ディレクトリを作成できません" >&2
  exit 1
}
STAGE_DIR="$AGENTS_DIR/.fx-codex-install.$$"
if ! mkdir "$STAGE_DIR"; then
  echo "ERROR: plist staging directoryを作成できません: $STAGE_DIR" >&2
  exit 1
fi
trap 'abort_on_signal 129 HUP' HUP
trap 'abort_on_signal 130 INT' INT
trap 'abort_on_signal 143 TERM' TERM

# 全candidateのrender/lintを完了するまでlaunchdや現行plistに触れない。
for label in $LABELS; do
  tmpl="$ROOT/ops/launchd/$label.plist.tmpl"
  candidate="$STAGE_DIR/$label.plist"
  if ! render "$tmpl" > "$candidate"; then
    abort_install 1 "plist生成失敗: $label"
  fi
  if ! lint_plist "$candidate"; then
    abort_install 1 "plistが不正: $candidate"
  fi
done

# 現行・legacy plistも全て変更前にbackupする。一部でも取得できなければ
# serviceを止めずに中止する。
for label in $ALL_LABELS; do
  target="$AGENTS_DIR/$label.plist"
  if [ -f "$target" ] && ! cp -p "$target" "$STAGE_DIR/$label.plist.previous"; then
    abort_install 1 "旧plist backup失敗: $target"
  fi
done

# legacy writerを新serviceより先に停止し、残存を検証する。
# ここから先の全変更はrollback対象。
MUTATION_STARTED=1
for label in $LEGACY_LABELS; do
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      abort_install 2 "legacy bootout失敗: $label"
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      abort_install 2 "legacy serviceがbootout後も残存: $label"
    fi
    echo "legacy bootout: $label"
  fi
  if [ -f "$AGENTS_DIR/$label.plist" ]; then
    staged_legacy="$STAGE_DIR/$label.plist.legacy-disabled"
    if ! mv "$AGENTS_DIR/$label.plist" "$staged_legacy"; then
      abort_install 2 "legacy plistを退避できません: $label"
    fi
    echo "legacy plistをtransaction内へ退避: $label"
  fi
done

for label in $LABELS; do
  target="$AGENTS_DIR/$label.plist"
  candidate="$STAGE_DIR/$label.plist"
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    if ! launchctl bootout "gui/$(id -u)/$label"; then
      abort_install 2 "既存serviceのbootout失敗: $label"
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      abort_install 2 "bootout後もserviceが残存: $label"
    fi
  fi
  if ! mv -f "$candidate" "$target"; then
    abort_install 1 "plist配置失敗: $target"
  fi
  if ! launchctl bootstrap "gui/$(id -u)" "$target"; then
    abort_install 1 "bootstrap失敗: $label"
  fi
  if ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    abort_install 1 "bootstrap後のservice確認失敗: $label"
  fi
  echo "installed: $label"
done

# 新serviceが全て確認できた後にだけlegacy無効化を確定する。
for label in $LEGACY_LABELS; do
  staged_legacy="$STAGE_DIR/$label.plist.legacy-disabled"
  if [ -f "$staged_legacy" ]; then
    disabled="$AGENTS_DIR/$label.plist.disabled-$(date +%Y%m%d%H%M%S)-$$"
    printf '%s\n' "$disabled" > "$STAGE_DIR/$label.disabled-path" || {
      abort_install 2 "legacy disabled path記録失敗: $label"
    }
    if ! mv "$staged_legacy" "$disabled"; then
      abort_install 2 "legacy plist無効化の確定失敗: $label"
    fi
    echo "legacy plist退避: $disabled"
  fi
done

trap - HUP INT TERM
cleanup_stage
MUTATION_STARTED=0

echo ""
echo "完了。状態確認: ./scripts/status_fx_services.sh"
