"""過去の判断ジャーナルから学習し、次回以降の分析を自動調整する。

journal.py が毎回記録するトレードプラン(方向・確信度・スコア内訳・終値/ATR)を
学習データとして使う。ブリーフィングは定期実行で追記され続けるため、
後続エントリの終値がそのまま「過去の判断から見た将来価格」になる。
これを利用して、成熟した(記録から約24時間経過した)全ての方向判断を
履歴内で相互採点し、3種類の調整を導く:

1. 複合スコア重みの再推定 — テクニカル単独/ニュース単独の方向的中率を比べ、
   当たっている根拠側へ重みを寄せる(シュリンク+クランプ付き)
2. 確信度キャリブレーション — 確信度帯ごとの実際の的中率を集計し、
   「確信度が高いほど本当に当たっているか」を可視化する
3. ペア別の確信度減衰 — 直近の的中率が低いペアの確信度を下げる
   (減衰のみ。成績が良くても確信度は増幅しない)
4. チャート状態別の学習(方向別) — 判断時に記録した特徴量(RSI・MA乖離・
   ボラ・時間足一致度・ニュース量など)を解釈しやすい固定バケットに分け、
   さらにロング/ショート別に「どんな状態のどちら向きが当たりやすい/
   外しやすいか」を集計する。同じ状態でも向きによって成績は非対称になる
   (例: RSI買われすぎ圏はロングでは外しやすいがショートは当たる)ため、
   状態×方向のセル単位で学習し、外しやすいセルに該当する判断だけ
   確信度を減衰して理由を注意点に載せる

journal.evaluate_directional_accuracy が「いま24時間前後の判断」だけを
現在価格と突き合わせるのに対し、本モジュールは履歴全体を学習データ化する。
各判断はホライズンに最も近い将来価格1点とだけ突き合わせるため、
同じ判断が経過時間ごとに違う結果で二重カウントされることはない。

安全設計(サンプル不足で暴れないためのガード):

- 記録間隔非依存の間引き — derive_profile は同一ペアの判断を1時間に1件へ
  間引いてから数える。5分間隔運用などでほぼ同一の判断が並んでも、
  サンプル数ガードが「実効サンプル」に対して機能するようにするため
- 重み再推定はテクニカル/ニュース両方の評価が20件以上そろうまで既定値のまま
- 重みはシュリンク n/(n+40) で徐々にしか動かず、テクニカル35〜70%にクランプ
- ペア別係数は8件以上たまってから、0.6〜1.0の範囲で減衰のみ
- 状態別の減衰は「バケット×方向」のセルごとに12件以上+的中率45%未満で発動、
  0.7〜1.0の範囲。複数の苦手状態に該当しても最悪の1条件のみ適用
  (特徴量は相関しやすく、掛け合わせると過剰減衰になるため)。
  方向で分けるぶんセルあたりのサンプルは半分になるので発動は遅くなるが、
  向きの非対称性を混ぜた誤った減衰を防ぐことを優先する
- プロファイルJSONが壊れていても既定プロファイルで動作継続

観測系(学習には使わず、可視化・反省に使う):

- ホライズン別レポート(horizon_report_ja) — 4h/24h/72hの複数ホライズンで
  的中率を観測する。学習(重み・状態・ML)は24h主ホライズンのみを使う。
  複数ホライズンで学習まで行うと多重検定で偶然のパターンを拾うため
- 確信度Brier — conviction/100 を確率予測とみなした平均二乗誤差。
  「常に全体的中率を出す予測」を基準に、確信度が情報を持つかを追跡する
- 反省レポート(reflection_report_ja) — 外した判断を「上位足逆行」
  「RSI極端圏への追随」「テクニカル/ニュース対立の押し切り」等の
  失敗理由テンプレートに分類し、全体より有意に外しやすい条件だけを報告する

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping, Sequence

from .briefing import CONFLICT_THRESHOLD, NEWS_WEIGHT, TECH_WEIGHT
from .journal import (
    DEFAULT_ATR_FRACTION,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_TOLERANCE_HOURS,
)
from .market import WEEKEND_CLOSURE, open_hours_between

# 学習サンプルの間引き幅。ブリーフィングの設計上の記録間隔(毎時)に合わせ、
# それより高頻度の運用(Mac miniは5分間隔)でもガードの実効性を保つ
DERIVE_THIN_GAP_HOURS = 1.0

# 重み再推定のガード
MIN_WEIGHT_SAMPLES = 20  # テクニカル/ニュース両方の評価がこの件数未満なら既定重みのまま
WEIGHT_SHRINK_HALFWAY = 40  # シュリンク係数 n/(n+この値)。40件で半分だけ動く
TECH_WEIGHT_MIN = 0.35
TECH_WEIGHT_MAX = 0.70

# ペア別確信度減衰のガード
MIN_SYMBOL_SAMPLES = 8
SYMBOL_FACTOR_MIN = 0.6
SYMBOL_HIT_RATE_BASELINE = 0.5  # 減衰係数の分母(この的中率なら係数1.0)
SYMBOL_HIT_RATE_TRIGGER = 0.45  # これ未満で初めて減衰(±数%の誤差範囲では動かさない)

# 確信度キャリブレーションの帯(下限, 上限=排他)
CONVICTION_BINS = ((0, 25), (25, 50), (50, 75), (75, 101))

# 確信度Brier(確率予測としての精度)を表示する最低採点数
MIN_BRIER_SAMPLES = 20

# 反省レポートのガード
MIN_REFLECTION_SAMPLES = 10  # 失敗条件1個あたりの最低採点数
REFLECTION_NOTABLE_DELTA = 0.10  # 全体的中率からこれ以上低い条件だけ報告
REFLECTION_MAX_ITEMS = 3  # 最も外しやすい条件からこの件数まで表示

# チャート状態(特徴量バケット×方向)別学習のガード
MIN_CONDITION_SAMPLES = 12  # バケット×方向のセルごとに必要な採点数
CONDITION_FACTOR_MIN = 0.7
CONDITION_HIT_RATE_TRIGGER = 0.45  # これ未満のセルだけ減衰対象
CONDITION_NOTABLE_DELTA = 0.10  # 全体的中率から±これ以上ずれたら学習メモに載せる

DIRECTION_LABEL_JA = {"long": "ロング", "short": "ショート"}


@dataclass(frozen=True)
class FeatureBucket:
    """特徴量1個ぶんの値域バケット。lowは含み、highは含まない。"""

    label_ja: str
    low: float
    high: float


@dataclass(frozen=True)
class FeatureSpec:
    """特徴量の定義。use_abs=Trueは符号(向き)を無視して大きさで分類する。"""

    key: str
    label_ja: str
    buckets: tuple[FeatureBucket, ...]
    use_abs: bool = False


# briefing._extract_features が記録する特徴量と対になる固定バケット定義。
# 分位点ではなく相場用語として解釈できる固定閾値を使う(表示・テストのしやすさ優先)
FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "rsi_1h",
        "RSI(1h)",
        (
            FeatureBucket("売られすぎ圏(35未満)", float("-inf"), 35.0),
            FeatureBucket("中立圏(35-65)", 35.0, 65.0),
            FeatureBucket("買われすぎ圏(65超)", 65.0, float("inf")),
        ),
    ),
    FeatureSpec(
        "ma_gap_atr",
        "MA乖離(ATR換算)",
        (
            FeatureBucket("小(0.5未満)", 0.0, 0.5),
            FeatureBucket("中(0.5-2)", 0.5, 2.0),
            FeatureBucket("大(2以上)", 2.0, float("inf")),
        ),
        use_abs=True,
    ),
    FeatureSpec(
        "atr_pct",
        "ボラティリティ(ATR/価格)",
        (
            FeatureBucket("低ボラ(0.10%未満)", 0.0, 0.10),
            FeatureBucket("中ボラ(0.10-0.25%)", 0.10, 0.25),
            FeatureBucket("高ボラ(0.25%以上)", 0.25, float("inf")),
        ),
    ),
    FeatureSpec(
        "tf_agreement",
        "時間足一致度",
        (
            FeatureBucket("不一致(50%未満)", 0.0, 0.5),
            FeatureBucket("部分一致(50-99%)", 0.5, 1.0),
            FeatureBucket("全時間足一致", 1.0, float("inf")),
        ),
    ),
    FeatureSpec(
        "news_count",
        "関連ニュース量",
        (
            FeatureBucket("僅少(0-1件)", 0.0, 2.0),
            FeatureBucket("普通(2-4件)", 2.0, 5.0),
            FeatureBucket("豊富(5件以上)", 5.0, float("inf")),
        ),
    ),
    FeatureSpec(
        "adx_1h",
        "ADX(1h)トレンド強度",
        (
            FeatureBucket("レンジ(20未満)", 0.0, 20.0),
            FeatureBucket("弱トレンド(20-30)", 20.0, 30.0),
            FeatureBucket("強トレンド(30以上)", 30.0, float("inf")),
        ),
    ),
    # 上位足レーティング(-1.0〜+1.0)。バケット×方向のセルにより
    # 「上位足が売り寄りなのにロング」=上位足逆行の成績を直接学習できる
    FeatureSpec(
        "rating_4h",
        "上位足レーティング(4h)",
        (
            FeatureBucket("売り寄り(-0.25未満)", float("-inf"), -0.25),
            FeatureBucket("中立(±0.25)", -0.25, 0.25),
            FeatureBucket("買い寄り(+0.25以上)", 0.25, float("inf")),
        ),
    ),
    FeatureSpec(
        "rating_1d",
        "上位足レーティング(1d)",
        (
            FeatureBucket("売り寄り(-0.25未満)", float("-inf"), -0.25),
            FeatureBucket("中立(±0.25)", -0.25, 0.25),
            FeatureBucket("買い寄り(+0.25以上)", 0.25, float("inf")),
        ),
    ),
)


def bucket_for(spec: FeatureSpec, value: float) -> FeatureBucket | None:
    """値が属するバケットを返す(定義域の外はNone)。"""
    checked = abs(value) if spec.use_abs else value
    for bucket in spec.buckets:
        if bucket.low <= checked < bucket.high:
            return bucket
    return None


@dataclass(frozen=True)
class EvaluatedCall:
    """過去の方向判断1件の採点結果。outcomeはhit/miss/flatのいずれか。"""

    symbol: str
    direction: str  # long / short
    conviction: int
    tech_score: float
    news_score: float
    outcome: str
    ts: str  # 記録時刻(ISO)
    features: Mapping[str, float] = field(default_factory=dict)  # 判断時のチャート状態
    # 判断方向に沿った符号付き値動きをATR換算した値(+1.0=ATR1個ぶん順行)。
    # ml.py(確率モデルの教師)と promotion.py(期待値計算)が使う
    move_atr: float | None = None
    data_quality: float | None = None  # 記録時のデータ品質(0.0〜1.0)


@dataclass(frozen=True)
class ConvictionBin:
    """確信度帯別の的中集計。"""

    low: int
    high: int  # 排他
    evaluated: int
    hits: int

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated


@dataclass
class LearnedProfile:
    """ジャーナル履歴から導いた分析調整のスナップショット。

    既定値のまま生成すればbriefingの既定挙動と完全に一致するため、
    学習データ不足・プロファイル破損時のフォールバックとしてそのまま使える。
    """

    generated_at: str = ""
    evaluated: int = 0  # hit/missとして採点できた件数(flat除く)
    hits: int = 0
    flat: int = 0
    tech_weight: float = TECH_WEIGHT
    news_weight: float = NEWS_WEIGHT
    tech_hit_rate: float | None = None
    news_hit_rate: float | None = None
    # 確信度(conviction/100)を確率予測とみなしたBrierスコア。
    # baseは「常に全体的中率を出す予測」のBrier(これを下回れば情報がある)
    conviction_brier: float | None = None
    conviction_brier_base: float | None = None
    bins: list[ConvictionBin] = field(default_factory=list)
    symbol_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    symbol_factors: dict[str, float] = field(default_factory=dict)
    # チャート状態×方向別の集計:
    # {特徴量キー: {バケット名: {"long"/"short": {"evaluated": n, "hits": h}}}}
    condition_stats: dict[str, dict[str, dict[str, dict[str, int]]]] = field(default_factory=dict)
    # 苦手な状態×方向の減衰係数: {特徴量キー: {バケット名: {"long"/"short": 係数}}}
    condition_factors: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    notes_ja: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated

    def conviction_factor(self, symbol: str) -> float:
        """ペア別の確信度減衰係数(調整対象外のペアは1.0)。"""
        return self.symbol_factors.get(symbol, 1.0)

    def condition_adjustment(
        self, features: Mapping[str, float], direction: str
    ) -> tuple[float, str]:
        """現在のチャート状態×判断方向が過去に外しやすかったセルなら(減衰係数, 理由)を返す。

        briefing.build_trade_plan の condition_adjuster にそのまま渡せる形。
        同じ状態でも向きで成績が非対称になるため、判断の方向(long/short)と
        一致するセルの係数だけを見る。複数の苦手条件に該当しても最も悪い
        1条件だけを適用する(特徴量同士は相関しやすく、掛け合わせると
        過剰減衰になるため)。該当なし・方向なしは (1.0, "")。
        """
        if direction not in ("long", "short"):
            return 1.0, ""
        worst: tuple[float, str] | None = None
        for spec in FEATURE_SPECS:
            factors = self.condition_factors.get(spec.key)
            if not factors:
                continue
            value = features.get(spec.key)
            if not isinstance(value, (int, float)):
                continue
            bucket = bucket_for(spec, float(value))
            if bucket is None:
                continue
            factor = factors.get(bucket.label_ja, {}).get(direction)
            if factor is None:
                continue
            cell = (
                self.condition_stats.get(spec.key, {}).get(bucket.label_ja, {}).get(direction, {})
            )
            evaluated = cell.get("evaluated", 0)
            hits = cell.get("hits", 0)
            rate = hits / evaluated if evaluated else 0.0
            direction_ja = DIRECTION_LABEL_JA.get(direction, direction)
            reason = (
                f"いまのチャート状態「{spec.label_ja}: {bucket.label_ja}」での"
                f"{direction_ja}判断は過去の的中率{rate:.0%}({evaluated}件)と低いため"
                f"確信度を×{factor:.2f}に減衰"
            )
            if worst is None or factor < worst[0]:
                worst = (factor, reason)
        return worst if worst is not None else (1.0, "")

    def summary_ja(self) -> str:
        """Discord表示用の学習メモ。"""
        if not self.notes_ja:
            return (
                "学習データ蓄積中 — 採点可能な過去判断がまだありません"
                "(記録から約24時間たつと自己学習が始まります)"
            )
        return "\n".join(self.notes_ja)


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def thin_calls(calls: Sequence[EvaluatedCall], min_gap_hours: float) -> list[EvaluatedCall]:
    """自己相関対策の間引き: 同一ペアで最低min_gap_hours空いた判断だけ残す。

    高頻度実行(5分間隔など)では隣接判断の評価窓がほぼ重複しており、
    全部数えると「n件の独立サンプル」を装った1件分の情報になるため。
    ml.py(4時間)と derive_profile(1時間)が共用する。
    """
    ordered = sorted(
        (call for call in calls if _parse_ts(call.ts) is not None),
        key=lambda call: call.ts,
    )
    last_kept: dict[str, datetime] = {}
    kept: list[EvaluatedCall] = []
    for call in ordered:
        ts = _parse_ts(call.ts)
        assert ts is not None  # 上でフィルタ済み
        previous = last_kept.get(call.symbol)
        if previous is not None and (ts - previous) < timedelta(hours=min_gap_hours):
            continue
        last_kept[call.symbol] = ts
        kept.append(call)
    return kept


@dataclass(frozen=True)
class HorizonSpec:
    """評価ホライズン1本の定義。hoursは市場オープン時間換算。"""

    label: str
    hours: float
    tolerance_hours: float


# 観測用の複数ホライズン。学習(重み・状態・ML・昇格)は24h主ホライズンのみを
# 使い、ここは「どの時間軸で当たっているか」の観測に徹する(多重検定の回避)
HORIZONS: tuple[HorizonSpec, ...] = (
    HorizonSpec("4h", 4.0, 1.0),
    HorizonSpec("24h", DEFAULT_HORIZON_HOURS, DEFAULT_TOLERANCE_HOURS),
    HorizonSpec("72h", 72.0, 6.0),
)


def horizon_report_ja(
    entries: Iterable[dict],
    thin_gap_hours: float = DERIVE_THIN_GAP_HOURS,
) -> str:
    """ホライズン別(短期4h/主24h/スイング72h)の方向的中率1行。データ無しは空文字。"""
    materialized = list(entries)
    parts: list[str] = []
    total_scored = 0
    for spec in HORIZONS:
        calls = evaluate_history(
            materialized,
            horizon_hours=spec.hours,
            tolerance_hours=spec.tolerance_hours,
        )
        if thin_gap_hours > 0:
            calls = thin_calls(calls, thin_gap_hours)
        scored = [call for call in calls if call.outcome in ("hit", "miss")]
        if not scored:
            parts.append(f"{spec.label} —(n=0)")
            continue
        hits = sum(1 for call in scored if call.outcome == "hit")
        total_scored += len(scored)
        parts.append(f"{spec.label} {hits / len(scored):.0%}(n={len(scored)})")
    if total_scored == 0:
        return ""
    return "ホライズン別の方向的中率(学習には24hのみ使用): " + " / ".join(parts)


def evaluate_history(
    entries: Iterable[dict],
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    atr_fraction: float = DEFAULT_ATR_FRACTION,
) -> list[EvaluatedCall]:
    """ジャーナル履歴内の全方向判断を、後続エントリの終値で採点する。

    各判断は「同じペアの、市場オープン時間換算で horizon±tolerance 先の
    将来価格のうちホライズンに最も近い1点」とだけ突き合わせる。
    値動きが記録時ATR×atr_fraction未満ならflat(判定除外)。
    将来価格がまだ無い判断(未成熟)は結果に含めない。
    """
    parsed: list[tuple[datetime, dict]] = []
    prices: dict[str, list[tuple[datetime, float]]] = {}
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        parsed.append((ts, entry))
        close = entry.get("close")
        if isinstance(close, (int, float)):
            prices.setdefault(str(entry.get("symbol", "")), []).append((ts, float(close)))
    for series in prices.values():
        series.sort(key=lambda point: point[0])
    # 二分探索用に時刻列を分離(毎時追記で数万行たまっても全走査しないため)
    price_times = {symbol: [point[0] for point in series] for symbol, series in prices.items()}

    calls: list[EvaluatedCall] = []
    for ts, entry in parsed:
        direction = entry.get("direction")
        if direction not in ("long", "short"):
            continue
        close = entry.get("close")
        if not isinstance(close, (int, float)):
            continue
        symbol_key = str(entry.get("symbol", ""))
        series = prices.get(symbol_key, [])
        times = price_times.get(symbol_key, [])
        # オープン時間は壁時計時間を超えないため、候補は壁時計で
        # [ホライズン下限, ホライズン上限+週末クローズ1回分] の範囲に限られる
        window_lower = ts + timedelta(hours=horizon_hours - tolerance_hours)
        window_upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
        best: tuple[float, float] | None = None  # (|経過-ホライズン|, 将来終値)
        for index in range(bisect_left(times, window_lower), len(series)):
            point_ts, point_close = series[index]
            if point_ts > window_upper:
                break
            age = open_hours_between(ts, point_ts)
            if not (horizon_hours - tolerance_hours <= age <= horizon_hours + tolerance_hours):
                continue
            gap = abs(age - horizon_hours)
            if best is None or gap < best[0]:
                best = (gap, point_close)
        if best is None:
            continue
        move = best[1] - float(close)
        signed_move = move if direction == "long" else -move
        atr = entry.get("atr")
        atr_value = float(atr) if isinstance(atr, (int, float)) and atr > 0 else None
        threshold = atr_fraction * atr_value if atr_value is not None else 0.0
        move_atr = round(signed_move / atr_value, 4) if atr_value is not None else None
        if signed_move > threshold:
            outcome = "hit"
        elif signed_move < -threshold:
            outcome = "miss"
        else:
            outcome = "flat"
        raw_quality = entry.get("data_quality")
        data_quality = float(raw_quality) if isinstance(raw_quality, (int, float)) else None
        raw_features = entry.get("features")
        features = (
            {
                str(key): float(value)
                for key, value in raw_features.items()
                if isinstance(value, (int, float))
            }
            if isinstance(raw_features, dict)
            else {}
        )
        calls.append(
            EvaluatedCall(
                symbol=str(entry.get("symbol", "")),
                direction=str(direction),
                conviction=int(entry.get("conviction", 0) or 0),
                tech_score=float(entry.get("tech_score", 0.0) or 0.0),
                news_score=float(entry.get("news_score", 0.0) or 0.0),
                outcome=outcome,
                ts=ts.isoformat(),
                features=features,
                move_atr=move_atr,
                data_quality=data_quality,
            )
        )
    return calls


def calibration_bins(calls: Sequence[EvaluatedCall]) -> list[ConvictionBin]:
    """確信度帯別の的中率(flatは除外)。"""
    bins = []
    scored = [call for call in calls if call.outcome in ("hit", "miss")]
    for low, high in CONVICTION_BINS:
        in_bin = [call for call in scored if low <= call.conviction < high]
        bins.append(
            ConvictionBin(
                low=low,
                high=high,
                evaluated=len(in_bin),
                hits=sum(1 for call in in_bin if call.outcome == "hit"),
            )
        )
    return bins


def _signal_hit_rate(calls: Sequence[EvaluatedCall], attr: str) -> tuple[float | None, int]:
    """tech_score/news_score単独で方向を当てられたかの的中率と評価件数。

    実際の値動きの方向は「hitなら判断どおり、missなら判断の逆」として復元する。
    スコアが0(方向の意見なし)の判断は評価から除く。
    """
    hits = 0
    total = 0
    for call in calls:
        if call.outcome not in ("hit", "miss"):
            continue
        score = getattr(call, attr)
        if score == 0:
            continue
        direction_sign = 1.0 if call.direction == "long" else -1.0
        actual_sign = direction_sign if call.outcome == "hit" else -direction_sign
        total += 1
        if (score > 0) == (actual_sign > 0):
            hits += 1
    if total == 0:
        return None, 0
    return hits / total, total


def _estimate_weights(
    tech_hit_rate: float | None,
    tech_n: int,
    news_hit_rate: float | None,
    news_n: int,
) -> tuple[float, float, bool]:
    """根拠別的中率から複合重みを再推定する。戻り値は(tech, news, 調整したか)。

    当たっている側(50%超過分)の比率へシュリンク付きで寄せる。
    どちらも50%以下なら「どちらの根拠も信用を増やせない」ので既定値のまま。
    """
    samples = min(tech_n, news_n)
    if tech_hit_rate is None or news_hit_rate is None or samples < MIN_WEIGHT_SAMPLES:
        return TECH_WEIGHT, NEWS_WEIGHT, False
    tech_excess = max(tech_hit_rate - 0.5, 0.0)
    news_excess = max(news_hit_rate - 0.5, 0.0)
    if tech_excess + news_excess <= 0:
        return TECH_WEIGHT, NEWS_WEIGHT, False
    raw_tech = tech_excess / (tech_excess + news_excess)
    shrink = samples / (samples + WEIGHT_SHRINK_HALFWAY)
    tech = TECH_WEIGHT + shrink * (raw_tech - TECH_WEIGHT)
    tech = round(max(TECH_WEIGHT_MIN, min(TECH_WEIGHT_MAX, tech)), 3)
    adjusted = tech != TECH_WEIGHT
    return tech, round(1.0 - tech, 3), adjusted


def derive_profile(
    calls: Sequence[EvaluatedCall],
    now: datetime | None = None,
    thin_gap_hours: float = DERIVE_THIN_GAP_HOURS,
) -> LearnedProfile:
    """採点済みの判断一覧から学習プロファイルを導く。

    記録間隔がどれだけ短くても(5分間隔運用など)、同一ペアの判断は
    thin_gap_hours に1件へ間引いてから数える。各種サンプル数ガードが
    「ほぼ同じ判断の重複」ではなく実効サンプルに対して機能するようにするため。
    """
    now = now or datetime.now(UTC)
    if thin_gap_hours > 0:
        calls = thin_calls(calls, thin_gap_hours)
    scored = [call for call in calls if call.outcome in ("hit", "miss")]
    flat = sum(1 for call in calls if call.outcome == "flat")
    hits = sum(1 for call in scored if call.outcome == "hit")

    tech_hit_rate, tech_n = _signal_hit_rate(scored, "tech_score")
    news_hit_rate, news_n = _signal_hit_rate(scored, "news_score")
    tech_weight, news_weight, weights_adjusted = _estimate_weights(
        tech_hit_rate, tech_n, news_hit_rate, news_n
    )

    symbol_stats: dict[str, dict[str, int]] = {}
    for call in scored:
        stats = symbol_stats.setdefault(call.symbol, {"evaluated": 0, "hits": 0})
        stats["evaluated"] += 1
        stats["hits"] += 1 if call.outcome == "hit" else 0
    symbol_factors: dict[str, float] = {}
    for symbol, stats in symbol_stats.items():
        if stats["evaluated"] < MIN_SYMBOL_SAMPLES:
            continue
        rate = stats["hits"] / stats["evaluated"]
        if rate >= SYMBOL_HIT_RATE_TRIGGER:
            continue
        factor = max(SYMBOL_FACTOR_MIN, round(rate / SYMBOL_HIT_RATE_BASELINE, 2))
        symbol_factors[symbol] = factor

    # チャート状態(特徴量バケット×方向)別の的中集計と苦手セルの減衰係数。
    # 同じ状態でもロング/ショートで成績が非対称になるため方向で分けて数える
    condition_stats: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for call in scored:
        if call.direction not in ("long", "short"):
            continue
        for spec in FEATURE_SPECS:
            value = call.features.get(spec.key)
            if not isinstance(value, (int, float)):
                continue
            bucket = bucket_for(spec, float(value))
            if bucket is None:
                continue
            cell = (
                condition_stats.setdefault(spec.key, {})
                .setdefault(bucket.label_ja, {})
                .setdefault(call.direction, {"evaluated": 0, "hits": 0})
            )
            cell["evaluated"] += 1
            cell["hits"] += 1 if call.outcome == "hit" else 0
    condition_factors: dict[str, dict[str, dict[str, float]]] = {}
    for key, buckets in condition_stats.items():
        for label, directions in buckets.items():
            for direction, cell in directions.items():
                if cell["evaluated"] < MIN_CONDITION_SAMPLES:
                    continue
                rate = cell["hits"] / cell["evaluated"]
                if rate >= CONDITION_HIT_RATE_TRIGGER:
                    continue
                condition_factors.setdefault(key, {}).setdefault(label, {})[direction] = max(
                    CONDITION_FACTOR_MIN, round(rate / SYMBOL_HIT_RATE_BASELINE, 2)
                )

    # 確信度を確率予測として採点(Brier)。基準は「常に全体的中率を出す予測」
    conviction_brier = conviction_brier_base = None
    if scored:
        overall_rate = hits / len(scored)
        outcomes = [
            (call.conviction / 100.0, 1.0 if call.outcome == "hit" else 0.0) for call in scored
        ]
        conviction_brier = round(sum((p - y) ** 2 for p, y in outcomes) / len(outcomes), 4)
        conviction_brier_base = round(
            sum((overall_rate - y) ** 2 for _, y in outcomes) / len(outcomes), 4
        )

    profile = LearnedProfile(
        generated_at=now.isoformat(),
        evaluated=len(scored),
        hits=hits,
        flat=flat,
        tech_weight=tech_weight,
        news_weight=news_weight,
        tech_hit_rate=tech_hit_rate,
        news_hit_rate=news_hit_rate,
        conviction_brier=conviction_brier,
        conviction_brier_base=conviction_brier_base,
        bins=calibration_bins(calls),
        symbol_stats=symbol_stats,
        symbol_factors=symbol_factors,
        condition_stats=condition_stats,
        condition_factors=condition_factors,
    )
    profile.notes_ja = _build_notes_ja(profile, weights_adjusted, tech_n, news_n)
    profile.notes_ja.extend(reflection_report_ja(scored))
    return profile


def _build_notes_ja(
    profile: LearnedProfile, weights_adjusted: bool, tech_n: int, news_n: int
) -> list[str]:
    if profile.evaluated == 0 and profile.flat == 0:
        return []
    notes: list[str] = []
    if profile.evaluated:
        line = (
            f"過去の方向判断{profile.evaluated}件を約24時間後の値動きで採点"
            f" — 的中率 {profile.hit_rate:.0%}"
        )
        if profile.flat:
            line += f" (ほか{profile.flat}件は小動きで判定除外)"
        notes.append(line)
    elif profile.flat:
        notes.append(f"採点対象{profile.flat}件はいずれも小動きのため判定除外")
        return notes

    if weights_adjusted:
        notes.append(
            f"根拠別の的中率: テクニカル{profile.tech_hit_rate:.0%}"
            f" / ニュース{profile.news_hit_rate:.0%}"
            f" → 複合重みを テクニカル{profile.tech_weight:.0%}"
            f"/ニュース{profile.news_weight:.0%} に自動調整"
        )
    else:
        samples = min(tech_n, news_n)
        base = (
            f"複合重みは既定(テクニカル{profile.tech_weight:.0%}"
            f"/ニュース{profile.news_weight:.0%})のまま"
        )
        if samples < MIN_WEIGHT_SAMPLES:
            notes.append(base + f" — 重み学習はサンプル{MIN_WEIGHT_SAMPLES}件から(現在{samples}件)")
        else:
            notes.append(
                base
                + f" — 根拠別の的中率(テクニカル{profile.tech_hit_rate:.0%}"
                + f"/ニュース{profile.news_hit_rate:.0%})に重みを動かすだけの差がない"
            )

    bin_parts = [
        f"{b.low}-{b.high - 1}帯 {b.hit_rate:.0%}(n={b.evaluated})"
        for b in profile.bins
        if b.evaluated > 0
    ]
    if bin_parts:
        notes.append("確信度帯別の的中率: " + " / ".join(bin_parts))

    # 確信度の確率精度(Brier)。サンプルが揃うまでは表示しない
    if (
        profile.evaluated >= MIN_BRIER_SAMPLES
        and profile.conviction_brier is not None
        and profile.conviction_brier_base is not None
    ):
        verdict = (
            "確信度は的中率の情報を持っている"
            if profile.conviction_brier < profile.conviction_brier_base
            else "確信度が実際の的中率と乖離(過信/過小)"
        )
        notes.append(
            f"確信度の確率精度: Brier {profile.conviction_brier:.3f}"
            f"(基準〈常に全体的中率を出す予測〉{profile.conviction_brier_base:.3f}"
            f"、小さいほど良い) → {verdict}"
        )

    # チャート状態×方向別: 全体的中率から大きくずれたセルだけを報告する
    overall = profile.hit_rate
    if overall is not None:
        strong: list[tuple[float, str]] = []
        weak: list[tuple[float, str]] = []
        for spec in FEATURE_SPECS:
            for label, directions in profile.condition_stats.get(spec.key, {}).items():
                for direction, cell in directions.items():
                    evaluated = cell.get("evaluated", 0)
                    if evaluated < MIN_CONDITION_SAMPLES:
                        continue
                    rate = cell.get("hits", 0) / evaluated
                    direction_ja = DIRECTION_LABEL_JA.get(direction, direction)
                    text = f"{spec.label_ja}: {label}×{direction_ja} {rate:.0%}(n={evaluated})"
                    factor = (
                        profile.condition_factors.get(spec.key, {}).get(label, {}).get(direction)
                    )
                    if factor is not None:
                        text += f" → 該当時は確信度×{factor:.2f}"
                    if rate >= overall + CONDITION_NOTABLE_DELTA:
                        strong.append((rate, text))
                    elif rate <= overall - CONDITION_NOTABLE_DELTA or factor is not None:
                        weak.append((rate, text))
        if strong:
            strong.sort(reverse=True)
            notes.append(
                "👍 当たりやすいチャート状態: " + " / ".join(text for _, text in strong[:3])
            )
        if weak:
            weak.sort()
            notes.append("⚠️ 苦手なチャート状態: " + " / ".join(text for _, text in weak[:3]))

    for symbol, factor in sorted(profile.symbol_factors.items()):
        stats = profile.symbol_stats.get(symbol, {})
        evaluated = stats.get("evaluated", 0)
        hits = stats.get("hits", 0)
        rate = hits / evaluated if evaluated else 0.0
        notes.append(
            f"⚠️ {symbol}: 直近の的中率{rate:.0%}({evaluated}件中{hits}件)"
            f" → 確信度を×{factor:.2f}に減衰して慎重に評価"
        )
    return notes


# ------------------------------------------------- 反省レポート(失敗理由の分類)


@dataclass(frozen=True)
class FailureTemplate:
    """失敗理由テンプレート1個。appliesは判断1件がこの条件に該当するかを返す。

    戻り値None=判定に必要な特徴量が記録されておらず評価不能(分母に入れない)。
    """

    key: str
    label_ja: str
    advice_ja: str
    applies: Callable[[EvaluatedCall], bool | None]


def _feature_of(call: EvaluatedCall, key: str) -> float | None:
    value = call.features.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _htf_against(interval_key: str) -> Callable[[EvaluatedCall], bool | None]:
    """上位足レーティングが判断方向と逆(±0.25以上逆向き)だったか。"""

    def check(call: EvaluatedCall) -> bool | None:
        rating = _feature_of(call, interval_key)
        if rating is None or call.direction not in ("long", "short"):
            return None
        if call.direction == "long":
            return rating <= -0.25
        return rating >= 0.25

    return check


def _rsi_extreme_follow(call: EvaluatedCall) -> bool | None:
    """買われすぎ圏でのロング/売られすぎ圏でのショート(過熱への追随)。"""
    rsi = _feature_of(call, "rsi_1h")
    if rsi is None or call.direction not in ("long", "short"):
        return None
    return rsi >= 65.0 if call.direction == "long" else rsi <= 35.0


def _tech_news_conflict(call: EvaluatedCall) -> bool | None:
    """テクニカルとニュースが強く対立する中での判断(briefingの対立閾値と同一)。"""
    tech, news = call.tech_score, call.news_score
    if tech == 0 or news == 0:
        return False  # どちらかが意見なしなら「対立」ではない
    return tech * news < 0 and min(abs(tech), abs(news)) >= CONFLICT_THRESHOLD


def _range_market_call(call: EvaluatedCall) -> bool | None:
    """レンジ相場(ADX20未満)でのトレンド方向判断。"""
    adx = _feature_of(call, "adx_1h")
    return adx < 20.0 if adx is not None else None


def _weak_tf_agreement(call: EvaluatedCall) -> bool | None:
    """時間足の過半が全体の向きと不一致のままの判断。"""
    agreement = _feature_of(call, "tf_agreement")
    return agreement < 0.5 if agreement is not None else None


def _low_data_quality(call: EvaluatedCall) -> bool | None:
    """根拠データが7割未満しか揃っていない状態での判断。"""
    if call.data_quality is None:
        return None
    return call.data_quality < 0.7


FAILURE_TEMPLATES: tuple[FailureTemplate, ...] = (
    FailureTemplate(
        "htf_against_4h",
        "上位足(4h)逆行に逆らった判断",
        "4時間足の流れに逆らう取引は分が悪い — 上位足の順行を待つ",
        _htf_against("rating_4h"),
    ),
    FailureTemplate(
        "htf_against_1d",
        "上位足(1d)逆行に逆らった判断",
        "日足の流れに逆らう取引は分が悪い — 上位足の順行を待つ",
        _htf_against("rating_1d"),
    ),
    FailureTemplate(
        "rsi_extreme_follow",
        "RSI極端圏への追随(過熱圏での順張り)",
        "過熱圏への飛び乗りより押し目・戻りを待つ",
        _rsi_extreme_follow,
    ),
    FailureTemplate(
        "tech_news_conflict",
        "テクニカルとニュースの対立を押し切った判断",
        "根拠が割れたときは見送りが基本",
        _tech_news_conflict,
    ),
    FailureTemplate(
        "range_trend_call",
        "レンジ相場(ADX20未満)でのトレンド判断",
        "方向感が続かない地合いでは確信度を抑える",
        _range_market_call,
    ),
    FailureTemplate(
        "weak_tf_agreement",
        "時間足の過半が逆行する中での判断",
        "時間足がそろうまで待つ",
        _weak_tf_agreement,
    ),
    FailureTemplate(
        "low_data_quality",
        "低データ品質(70%未満)での判断",
        "根拠が欠けた判断は記録上も外しやすい",
        _low_data_quality,
    ),
)


def reflection_report_ja(
    calls: Sequence[EvaluatedCall],
    min_samples: int = MIN_REFLECTION_SAMPLES,
    notable_delta: float = REFLECTION_NOTABLE_DELTA,
    max_items: int = REFLECTION_MAX_ITEMS,
) -> list[str]:
    """外した判断を失敗理由テンプレートに分類し、目立って外しやすい条件を報告する。

    条件ごとに「該当する判断の的中率」を全体と比べ、min_samples件以上たまり、
    かつ全体よりnotable_delta以上低く50%も下回る条件だけを悪い順にmax_items件
    表示する。サンプル不足・目立った差なしなら空リスト(=何も報告しない)。
    """
    scored = [call for call in calls if call.outcome in ("hit", "miss")]
    if len(scored) < min_samples:
        return []
    overall = sum(1 for call in scored if call.outcome == "hit") / len(scored)
    findings: list[tuple[float, str]] = []
    for template in FAILURE_TEMPLATES:
        matched = [call for call in scored if template.applies(call) is True]
        if len(matched) < min_samples:
            continue
        rate = sum(1 for call in matched if call.outcome == "hit") / len(matched)
        if rate > overall - notable_delta or rate >= 0.5:
            continue
        findings.append(
            (
                rate,
                f"・{template.label_ja}: 的中率{rate:.0%}"
                f"({len(matched)}件、全体{overall:.0%}) — {template.advice_ja}",
            )
        )
    if not findings:
        return []
    findings.sort()
    return ["🪞 反省レポート(外しやすい判断パターン):"] + [text for _, text in findings[:max_items]]


def save_profile(profile: LearnedProfile, path: str | Path) -> None:
    """プロファイルをJSONへ保存する(毎回上書き)。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": profile.generated_at,
        "evaluated": profile.evaluated,
        "hits": profile.hits,
        "flat": profile.flat,
        "tech_weight": profile.tech_weight,
        "news_weight": profile.news_weight,
        "tech_hit_rate": profile.tech_hit_rate,
        "news_hit_rate": profile.news_hit_rate,
        "conviction_brier": profile.conviction_brier,
        "conviction_brier_base": profile.conviction_brier_base,
        "bins": [
            {"low": b.low, "high": b.high, "evaluated": b.evaluated, "hits": b.hits}
            for b in profile.bins
        ],
        "symbol_stats": profile.symbol_stats,
        "symbol_factors": profile.symbol_factors,
        "condition_stats": profile.condition_stats,
        "condition_factors": profile.condition_factors,
        "notes_ja": profile.notes_ja,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile(path: str | Path) -> LearnedProfile:
    """保存済みプロファイルを読む。無い/壊れている場合は既定プロファイル。"""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LearnedProfile()
    if not isinstance(payload, dict):
        return LearnedProfile()
    try:
        tech_weight = float(payload.get("tech_weight", TECH_WEIGHT))
        news_weight = float(payload.get("news_weight", NEWS_WEIGHT))
        if not (TECH_WEIGHT_MIN <= tech_weight <= TECH_WEIGHT_MAX):
            tech_weight, news_weight = TECH_WEIGHT, NEWS_WEIGHT
        bins = [
            ConvictionBin(
                low=int(b["low"]),
                high=int(b["high"]),
                evaluated=int(b["evaluated"]),
                hits=int(b["hits"]),
            )
            for b in payload.get("bins", [])
        ]
        symbol_factors = {
            str(symbol): max(SYMBOL_FACTOR_MIN, min(1.0, float(factor)))
            for symbol, factor in dict(payload.get("symbol_factors", {})).items()
        }
        symbol_stats = {
            str(symbol): {"evaluated": int(s.get("evaluated", 0)), "hits": int(s.get("hits", 0))}
            for symbol, s in dict(payload.get("symbol_stats", {})).items()
        }
        # 状態×方向のセル構造。方向キーがlong/short以外(旧形式含む)は読み飛ばす
        condition_stats = {
            str(key): {
                str(label): {
                    str(direction): {
                        "evaluated": int(cell.get("evaluated", 0)),
                        "hits": int(cell.get("hits", 0)),
                    }
                    for direction, cell in dict(directions).items()
                    if direction in ("long", "short") and isinstance(cell, dict)
                }
                for label, directions in dict(buckets).items()
                if isinstance(directions, dict)
            }
            for key, buckets in dict(payload.get("condition_stats", {})).items()
        }
        condition_factors = {
            str(key): {
                str(label): {
                    str(direction): max(CONDITION_FACTOR_MIN, min(1.0, float(factor)))
                    for direction, factor in dict(directions).items()
                    if direction in ("long", "short") and isinstance(factor, (int, float))
                }
                for label, directions in dict(buckets).items()
                if isinstance(directions, dict)
            }
            for key, buckets in dict(payload.get("condition_factors", {})).items()
        }
        tech_hit_rate = payload.get("tech_hit_rate")
        news_hit_rate = payload.get("news_hit_rate")
        raw_brier = payload.get("conviction_brier")
        raw_brier_base = payload.get("conviction_brier_base")
        return LearnedProfile(
            generated_at=str(payload.get("generated_at", "")),
            evaluated=int(payload.get("evaluated", 0)),
            hits=int(payload.get("hits", 0)),
            flat=int(payload.get("flat", 0)),
            tech_weight=tech_weight,
            news_weight=news_weight,
            tech_hit_rate=float(tech_hit_rate) if tech_hit_rate is not None else None,
            news_hit_rate=float(news_hit_rate) if news_hit_rate is not None else None,
            conviction_brier=float(raw_brier) if raw_brier is not None else None,
            conviction_brier_base=float(raw_brier_base) if raw_brier_base is not None else None,
            bins=bins,
            symbol_stats=symbol_stats,
            symbol_factors=symbol_factors,
            condition_stats=condition_stats,
            condition_factors=condition_factors,
            notes_ja=[str(note) for note in payload.get("notes_ja", [])],
        )
    except (KeyError, TypeError, ValueError):
        return LearnedProfile()
