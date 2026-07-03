# 既知の課題（次PR以降）

コミット前レビューで検出したが、本PRに含めると差分が膨らむため別途対応とする項目。

---

## [最優先・次PR] slow_window が大きいと検証済みパラメータでもシグナルが沈黙する

**深刻度**: 高（正規パラメータでも発注が止まる沈黙障害）

`params_gate` は `slow_window` を最大 500 まで受理する（`params_gate.py` の
`PARAM_BOUNDS`）。一方 `trader/app/strategy.py` の `fetch_prices` は
`durationStr=f"{max(bars, 60)} S"` × `barSizeSetting="5 secs"` で最大 200 本しか
取得しない。`ma_cross_signal` は `len(df) < slow + 1` でデータ不足時に None を返すため、
検証を**通過した正規パラメータ**でも `slow_window ≳ 200` だとシグナルが一切出なくなる。

**対応案**:
- `fetch_prices` の取得本数を `slow_window` から必要数を逆算して決める、または
- バー間隔（5 secs）を戦略の時間軸に合わせて広げる、または
- `PARAM_BOUNDS` の `slow_window` 上限を取得可能本数と整合させる。

いずれにせよ「gate 受理範囲」と「実際に取得できるバー数」を突き合わせるテストを追加する。

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
