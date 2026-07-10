# 既知の課題（次PR以降）

コミット前レビューで検出したが、本PRに含めると差分が膨らむため別途対応とする項目。

---

## [解決済み] slow_window が大きいと検証済みパラメータでもシグナルが沈黙する

**深刻度**: 高（正規パラメータでも発注が止まる沈黙障害）
**状態**: 解決（branch `feature/fx-intel-reliability`）

`params_gate` は `slow_window` を最大 500 まで受理する（`params_gate.py` の
`PARAM_BOUNDS`）。一方 `trader/app/strategy.py` の旧 `fetch_prices` は
`durationStr=f"{max(bars, 60)} S"` × `barSizeSetting="5 secs"` で最大 40 本しか
取得できず（200 S ÷ 5 secs）、`ma_cross_signal` は `len(df) < slow + 1` で None を
返すため、検証を**通過した正規パラメータ**（現行 active の `slow_window=100` を含む）で
シグナルが一切出なくなっていた。

**対応（実装済み）**:
- `fetch_prices` は取得本数を `required_bars(slow_window, atr_window)` で逆算し、
  `duration_str()` で IB の `N S`/`N D`/`N W` に単位を繰り上げて要求する。
- バー間隔は `STRATEGY_BAR_SIZE_SEC`（既定 5、config でバリデーション）で設定可能に。
- 取得後 `len(df) < slow+1` を検知したら warning を出す（無音の沈黙を防ぐ）。
- `tests/test_strategy_params.py` に「gate 受理範囲（PARAM_BOUNDS の全 slow_window）× 全
  バー間隔で必要本数が満たされる」ことを突き合わせる回帰テストを追加。

---

## [残課題・別PR] バー時間軸と backtest 時間軸の不一致

**深刻度**: 中（沈黙は解消したが、戦略の意味論が backtest と一致しない可能性）

`auto_optimize.py` は時間足（hourly 形状）データで `slow_window` を最適化するが、
既定の `STRATEGY_BAR_SIZE_SEC=5`（5 秒足）では同じ `slow_window=100` でも MA が
約 8 分しか張らず、backtest とは別物の戦略になる。上記修正で沈黙は解消したが、
「配備 params の時間軸」と「live のバー間隔」を突き合わせる仕組みは未整備。

**対応案**:
- `provenance` に最適化データのバー間隔を記録し、`STRATEGY_BAR_SIZE_SEC` と一致
  しなければ warning／拒否する（params_gate 拡張）。

---

## [別issue] ミラー params_gate の synthetic-hash 検知が読み込み側で弱い（多層防御の非対称）

**深刻度**: 低（生成側が先に弾くため実害は限定的）

`params_gate.synthetic_hashes()` は「定数の既知ハッシュ ∪（`BUNDLED_SAMPLE` が存在すれば）
その実ファイルのハッシュ」を返す。flat repo 側は `examples/sample_prices.csv` があるため
サンプル再生成でハッシュが変わっても動的検知できるが、`trader/app/params_gate.py`
（コンテナ焼き込みミラー）は `examples/` を含まないため `BUNDLED_SAMPLE.is_file()` が
False になり、**定数 `KNOWN_SYNTHETIC_SHA256` の 1 個のみ**で判定する。

サンプルデータが再生成されて新ハッシュになった場合、生成側は弾くが読み込み側ミラーは
定数が古いままだと素通りしうる。現状は provenance を stamp する生成側が先に弾くので
実害は低いが、多層防御の原則からは読み込み側でも最新の合成ハッシュを検知できることが望ましい。

**対応案**:
- 既知合成ハッシュの定数リストを生成側・ミラー双方で最新に保つ運用を明文化する、または
- ミラーにも合成サンプルのハッシュ（ファイルではなく値）を同梱して定数を拡充する。

---

## [更新 2026-07-10午後] 下記「学習データ収集ループの停止」の実態はMac mini側で別物と判明

同日のMac mini実機監査(SSH)で訂正:

- **収集は停止していない**。本番実体は `~/srv/fx-codex`(開発機の当初想定と別ディレクトリ)で、
  `briefing_tf_prices.jsonl` は5分毎更新継続中・融合ジャーナル466行。
  開発機`logs/`の停止(07-08)は手動実験の残骸で、収集責務は元々Mac mini側にあった
- **真の問題は多重起動**: 手動ループ×3組 + launchd(briefing.hourly) + 壊れた5分毎cron
  (`~/trader/fx-codex`、params_gate欠落でクラッシュループ)が並走し、
  ジャーナルへ毎時2〜3回の重複判断を書込み=学習サンプルの水増し汚染
- **対策実装済み**: launchd常駐化(排他ロック付き)+鮮度監視+Discord通知。
  移行手順は docs/OPERATIONS_RUNBOOK.md §2。srv実体がmainから10PR遅れ+
  未コミット手パッチ(約800行)を持つ問題は移行時にrescueブランチで保全する

## [運用・P1] 学習データ収集ループの停止 → 学習層全体がデータ飢餓（2026-07-10監査）

**深刻度**: 高（コードは健全だが、学習・昇格・期待値ガードの全レイヤーが入力ゼロで空転する）

2026-07-10時点の開発機 `logs/` の実測:

- `briefing_journal.jsonl`（融合判断）: **6件**（2026-07-07〜07-08で停止）
- `briefing_tf_journal.jsonl`（時間足別判断）: **36件**（同上）
- `briefing_tf_prices.jsonl`（5分価格スナップショット）: **28件、最終 2026-07-08 15:21 UTC**
  — `fx_tf_snapshot_loop.sh` が止まっており、15m/1h判断の採点窓に将来価格が入らない
- `promotion_state.json`: macro/ml両委員とも**実効サンプル 0/40件**でshadow足止め
  （昇格ゲート自体は設計どおり正しく動作している）

つまり learning.py / tf_learning.py / decision_feedback.py / promotion.py /
trade_outcome.py の精緻な学習・監査基盤は実装済みだが、**入力となる判断と
価格系列が継続供給されていない**。`ps aux` でも開発機にループプロセスは無し。
Mac mini側の稼働状態はこのセッションからは未確認（SSH権限外）。

**対応案**:
- `fx_briefing_loop.sh`（毎時）と `fx_tf_snapshot_loop.sh`（5分毎）をどちらのマシンで
  常駐させるかを決めて起動する（launchd不可のためターミナル起動 or Mac mini側cron相当）
- 鮮度監視: ジャーナル/スナップショットの最終書込みが閾値（例: snapshot 30分、判断 3時間）を
  超えたらDiscordへWARNINGを出す監視をmonitor系に追加する

---

## [CI・低] `tools/` が mypy 対象外

CIの型チェックは `mypy fx_backtester fx_intel *.py` で、`tools/`
（learning_capture / 各monitor / ai_learning_dashboard）は対象外。black/ruffは通るが
型エラーは検出されない。対応案: CI対象へ追加し、既存エラーはベースライン化して
新規エラーのみ落とす。
