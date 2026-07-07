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

## [解決済み] バー時間軸と backtest 時間軸の不一致

**深刻度**: 中（沈黙は解消したが、戦略の意味論が backtest と一致しない可能性）
**状態**: 解決（対応案どおり params_gate を拡張）

`auto_optimize.py` は時間足（hourly 形状）データで `slow_window` を最適化するが、
既定の `STRATEGY_BAR_SIZE_SEC=5`（5 秒足）では同じ `slow_window=100` でも MA が
約 8 分しか張らず、backtest とは別物の戦略になる。上記修正で沈黙は解消したが、
「配備 params の時間軸」と「live のバー間隔」を突き合わせる仕組みは未整備だった。

**対応（実装済み）**:
- `params_gate.data_provenance()` が最適化データの CSV からバー間隔を推定し
  （連続行の時間差の中央値 = 日足の週末ギャップに頑健）、
  `provenance.data.bar_interval_sec` として記録する。生成側はどこも変更不要
  （root の auto_optimize.py も、trader 側の将来の実データ最適化も自動で記録される）。
- 読み込み側 `validate_params()` / `load_validated_params()` に
  `expected_bar_interval_sec` を追加。trader の `strategy.py`（ParamStore）は
  `STRATEGY_BAR_SIZE_SEC` を渡し、記録された間隔と 10% 超ずれていれば拒否する。
- `bar_interval_sec` の記録が無い既存ファイルは互換のため突合しない
  （次回の最適化実行から自動で記録される）。

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
