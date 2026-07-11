"""委員(マクロ・ML)の研究用shadow評価器。

新任委員(macro/ml)の意見を非影響shadowとして記録し、参考診断を表示する:

- shadow: 意見は計算・記録・表示されるが複合スコアには影響しない。
          ジャーナルの特徴量(macro_score / ml_edge)として蓄積され、
          後から独立に成績を採点できる
簡易診断は、委員の意見符号が「約24時間後の値動きと一致したか」を数え、
以下を表示する:

1. サンプル数が十分(自己相関を間引いた実効数)
2. 方向的中率が基準+マージンを上回る
3. ATR正規化期待値(expectancy)が正 — 当たり負けの値幅まで見て、
   「当たるが薄利、外すと大損」を弾く
4. 統計的有意性 — 的中率が偶然50%を超えただけでないか(二項片側検定、
   overfitting.py と同じくscipy非依存の正規近似)

この診断は重複ホライズン、terminal-price proxy、PIT未証明journalを使うlegacy指標であり、
institutionalなvalidated/paper証拠ではない。そのためこのビルドは委員を常にshadowへ固定し、
保存済みのpaper/live/未知段階もshadowへfail closedする。互換引数
``require_live_ack`` が渡されても段階遷移しない。

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。状態は logs/promotion_state.json に永続化する。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .market import open_hours_between, WEEKEND_CLOSURE
from .ml import THIN_MIN_GAP_HOURS

STAGES = ("shadow",)
MEMBERS = ("macro", "ml")

# 委員の意見スコアが記録される特徴量キー
MEMBER_FEATURE_KEY = {"macro": "macro_score", "ml": "ml_edge"}

# legacy診断の参考閾値（段階昇格には使用しない）
PROMOTE_MIN_SAMPLES = 40  # 自己相関間引き後の実効採点数
PROMOTE_MIN_HIT_RATE = 0.52  # 方向的中率の下限
PROMOTE_MIN_EXPECTANCY = 0.02  # ATR換算の1トレード期待値の下限
PROMOTE_MAX_PVALUE = 0.10  # 「偶然50%超」の確率がこれ以下なら有意

# 委員の意見が「方向あり」とみなす最小の絶対スコア(中立票を採点から除く)
OPINION_ACTIVE_THRESHOLD = 0.05

DEFAULT_STATE_PATH = "logs/promotion_state.json"


@dataclass(frozen=True)
class MemberPerformance:
    """委員1人の意見をジャーナルで独立採点した結果。"""

    member: str
    evaluated: int = 0  # 方向を持つ意見のうち採点できた数
    hits: int = 0
    expectancy_atr: float | None = None  # 意見方向のATR正規化期待値
    p_value: float | None = None  # 的中率が偶然50%超である確率(片側)

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated

    def meets_reference_thresholds(self) -> tuple[bool, list[str]]:
        """legacy参考閾値内かを返す。段階昇格の権限は持たない。"""
        reasons: list[str] = []
        if self.evaluated < PROMOTE_MIN_SAMPLES:
            reasons.append(f"実効サンプル不足({self.evaluated}/{PROMOTE_MIN_SAMPLES}件)")
        rate = self.hit_rate
        if rate is None or rate < PROMOTE_MIN_HIT_RATE:
            shown = f"{rate:.0%}" if rate is not None else "—"
            reasons.append(f"的中率不足({shown}<{PROMOTE_MIN_HIT_RATE:.0%})")
        if self.expectancy_atr is None or self.expectancy_atr < PROMOTE_MIN_EXPECTANCY:
            shown = f"{self.expectancy_atr:+.3f}" if self.expectancy_atr is not None else "—"
            reasons.append(f"期待値不足(ATR換算{shown}<{PROMOTE_MIN_EXPECTANCY:+.3f})")
        if self.p_value is None or self.p_value > PROMOTE_MAX_PVALUE:
            shown = f"{self.p_value:.2f}" if self.p_value is not None else "—"
            reasons.append(f"有意性不足(p={shown}>{PROMOTE_MAX_PVALUE:.2f})")
        return (not reasons), reasons

    def to_dict(self) -> dict[str, object]:
        return {
            "member": self.member,
            "evaluated": self.evaluated,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "expectancy_atr": self.expectancy_atr,
            "p_value": self.p_value,
        }

    @classmethod
    def from_mapping(cls, member: str, payload: object) -> MemberPerformance | None:
        if not isinstance(payload, Mapping):
            return None
        evaluated = _int(payload.get("evaluated"))
        hits = _int(payload.get("hits"))
        return cls(
            member=member,
            evaluated=evaluated,
            hits=hits,
            expectancy_atr=_float(payload.get("expectancy_atr")),
            p_value=_float(payload.get("p_value")),
        )


def _one_sided_binomial_pvalue(hits: int, n: int, p0: float = 0.5) -> float:
    """H0: 真の的中率=p0 に対し「hits以上当たる」確率の正規近似(片側)。

    overfitting.py と同じくscipy非依存。連続性補正付き。n=0は判定不能で1.0。
    """
    if n <= 0:
        return 1.0
    mean = n * p0
    std = math.sqrt(n * p0 * (1.0 - p0))
    if std == 0:
        return 1.0
    z = (hits - 0.5 - mean) / std  # 連続性補正
    # 標準正規の上側確率 = 0.5 * erfc(z / sqrt(2))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _thin_indices(stamps: Sequence[datetime], min_gap_hours: float) -> list[int]:
    """自己相関間引き: 昇順時刻列から最低min_gap_hours空いた要素の添字を返す。"""
    order = sorted(range(len(stamps)), key=lambda i: stamps[i])
    kept: list[int] = []
    last: datetime | None = None
    for i in order:
        if last is not None and (stamps[i] - last) < timedelta(hours=min_gap_hours):
            continue
        last = stamps[i]
        kept.append(i)
    return kept


def evaluate_member(
    member: str,
    entries: Sequence[Mapping],
    now: datetime | None = None,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 2.0,
    atr_fraction: float = 0.1,
) -> MemberPerformance:
    """ジャーナル生エントリから委員の意見を独立に採点する。

    委員のスコアは entry["features"][macro_score/ml_edge] に記録されている
    (shadow段階でも入る)。そのスコアの符号を「その委員が主張した方向」と
    みなし、約24時間後の同ペア終値と突き合わせて的中/期待値を測る。
    learning.evaluate_history と同じホライズン・ATR除外・自己相関間引きを使う。
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    feature_key = MEMBER_FEATURE_KEY.get(member)
    if feature_key is None:
        return MemberPerformance(member=member)

    # ペアごとの価格系列(将来価格の突き合わせ用)
    prices: dict[str, list[tuple[datetime, float]]] = {}
    parsed: list[tuple[datetime, str, float, float | None, float]] = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts > now:
            continue
        symbol = str(entry.get("symbol", ""))
        close_value = _finite_float(entry.get("close"))
        if close_value is not None:
            prices.setdefault(symbol, []).append((ts, close_value))
        features = entry.get("features")
        raw_opinion = features.get(feature_key) if isinstance(features, Mapping) else None
        opinion_value = _finite_float(raw_opinion)
        atr_value = _finite_float(entry.get("atr"))
        if atr_value is not None and atr_value <= 0:
            atr_value = None
        if opinion_value is not None and close_value is not None:
            parsed.append((ts, symbol, close_value, atr_value, opinion_value))
    for series in prices.values():
        series.sort(key=lambda point: point[0])

    scored: list[tuple[datetime, int, float]] = []  # (ts, hit(1/0), move_atr)
    for ts, symbol, entry_close, atr_value, opinion in parsed:
        if abs(opinion) < OPINION_ACTIVE_THRESHOLD:
            continue  # 中立票は方向判断していないので採点しない
        future_close = _future_close(prices.get(symbol, []), ts, horizon_hours, tolerance_hours)
        if future_close is None:
            continue
        move = future_close - entry_close
        signed_move = move if opinion > 0 else -move
        threshold = atr_fraction * atr_value if atr_value is not None else 0.0
        if abs(signed_move) <= threshold:
            continue  # 小動きは判定除外
        hit = 1 if signed_move > 0 else 0
        move_atr = signed_move / atr_value if atr_value is not None else 0.0
        scored.append((ts, hit, move_atr))

    if not scored:
        return MemberPerformance(member=member)

    kept = _thin_indices([s[0] for s in scored], THIN_MIN_GAP_HOURS)
    effective = [scored[i] for i in kept]
    evaluated = len(effective)
    hits = sum(hit for _, hit, _ in effective)
    expectancy = sum(move for _, _, move in effective) / evaluated if evaluated else None
    p_value = _one_sided_binomial_pvalue(hits, evaluated)
    return MemberPerformance(
        member=member,
        evaluated=evaluated,
        hits=hits,
        expectancy_atr=round(expectancy, 4) if expectancy is not None else None,
        p_value=round(p_value, 4),
    )


def _future_close(
    series: Sequence[tuple[datetime, float]],
    ts: datetime,
    horizon_hours: float,
    tolerance_hours: float,
) -> float | None:
    """ts から市場オープン時間換算で horizon±tolerance に最も近い将来終値。"""
    window_lower = ts + timedelta(hours=horizon_hours - tolerance_hours)
    window_upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    best: tuple[float, float] | None = None
    for point_ts, point_close in series:
        if point_ts < window_lower:
            continue
        if point_ts > window_upper:
            break
        age = open_hours_between(ts, point_ts)
        if not (horizon_hours - tolerance_hours <= age <= horizon_hours + tolerance_hours):
            continue
        gap = abs(age - horizon_hours)
        if best is None or gap < best[0]:
            best = (gap, point_close)
    return best[1] if best is not None else None


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass
class PromotionState:
    """委員ごとの現在の段階と履歴。logs/promotion_state.json に対応。"""

    stages: dict[str, str] = field(default_factory=lambda: {m: "shadow" for m in MEMBERS})
    updated_at: str = ""
    history: list[dict] = field(default_factory=list)
    notes_ja: list[str] = field(default_factory=list)
    last_performance: dict[str, dict[str, object]] = field(default_factory=dict)

    def stage_of(self, member: str) -> str:
        stage = self.stages.get(member, "shadow")
        return stage if stage in STAGES else "shadow"

    def as_stage_map(self) -> dict[str, str]:
        """committee.deliberate の stages 引数へそのまま渡せる形。"""
        return {member: self.stage_of(member) for member in MEMBERS}


def _transition(
    state: PromotionState,
    member: str,
    new_stage: str,
    reason: str,
    now: datetime,
) -> None:
    old = state.stage_of(member)
    if old == new_stage:
        return
    state.stages[member] = new_stage
    state.history.append(
        {
            "ts": now.isoformat(),
            "member": member,
            "from": old,
            "to": new_stage,
            "reason": reason,
        }
    )


def update_stages(
    state: PromotionState,
    performances: Mapping[str, MemberPerformance],
    now: datetime | None = None,
    require_live_ack: Sequence[str] = (),
) -> PromotionState:
    """legacy実績を記録し、委員段階をshadowへfail closedする。

    validated/paper/liveへの遷移はこの状態機械の権限外であり、互換引数があっても
    実行しない。

    副作用として state を書き換え、notes_ja に今回の判断理由を残す。
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    notes: list[str] = []
    for member in MEMBERS:
        perf = performances.get(member) or MemberPerformance(member=member)
        raw_stage = state.stages.get(member, "shadow")
        if raw_stage != "shadow":
            state.stages[member] = "shadow"
            state.history.append(
                {
                    "ts": now.isoformat(),
                    "member": member,
                    "from": raw_stage,
                    "to": "shadow",
                    "reason": "research buildはshadow固定",
                }
            )
        else:
            state.stages[member] = "shadow"
        ok, reasons = perf.meets_reference_thresholds()
        rate = perf.hit_rate
        rate_ja = f"{rate:.0%}" if rate is not None else "—"
        summary = (
            f"{member}: shadow | 採点{perf.evaluated}件 的中{rate_ja}"
            f" 期待値{_fmt(perf.expectancy_atr)} p={_fmt(perf.p_value)}"
        )
        diagnostic = "legacy診断閾値内" if ok else " / ".join(reasons)
        requested = " / live互換引数は無効" if member in require_live_ack else ""
        notes.append(f"🔒 {member} はshadow固定 — {diagnostic}{requested} ({summary})")
        state.last_performance[member] = perf.to_dict()

    state.updated_at = now.isoformat()
    state.notes_ja = notes
    return state


def _fmt(value: float | None) -> str:
    return f"{value:+.3f}" if value is not None else "—"


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _finite_float(value: object) -> float | None:
    parsed = _float(value)
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def summary_ja(state: PromotionState) -> str:
    """Discord表示用の昇格状況メモ。"""
    header = "運用段階: " + " / ".join(f"{m}={state.stage_of(m)}" for m in MEMBERS)
    if not state.notes_ja:
        return header
    return header + "\n" + "\n".join(state.notes_ja)


# ---------------------------------------------------------------- 保存/読込


def load_state(path: str | Path) -> PromotionState:
    """保存済み段階を読む。無い/壊れている/未知段階は全員shadowで開始。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PromotionState()
    if not isinstance(payload, dict):
        return PromotionState()
    raw_stages = payload.get("stages", {})
    stages = {}
    for member in MEMBERS:
        stage = raw_stages.get(member) if isinstance(raw_stages, Mapping) else None
        stages[member] = stage if stage in STAGES else "shadow"
    history = payload.get("history", [])
    raw_performance = payload.get("last_performance")
    last_performance = raw_performance if isinstance(raw_performance, dict) else {}
    return PromotionState(
        stages=stages,
        updated_at=str(payload.get("updated_at", "")),
        history=[h for h in history if isinstance(h, dict)][-200:],  # 履歴は直近200件に制限
        last_performance={
            member: dict(value)
            for member, value in last_performance.items()
            if member in MEMBERS and isinstance(value, Mapping)
        },
    )


def save_state(state: PromotionState, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stages": {member: state.stage_of(member) for member in MEMBERS},
        "updated_at": state.updated_at,
        "history": state.history[-200:],
        "notes_ja": state.notes_ja,
        "last_performance": {
            member: dict(state.last_performance.get(member, {})) for member in MEMBERS
        },
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def evaluate_and_update(
    entries: Sequence[Mapping],
    state: PromotionState,
    now: datetime | None = None,
    require_live_ack: Sequence[str] = (),
) -> tuple[PromotionState, dict[str, MemberPerformance]]:
    """ジャーナルから全委員を採点し、段階を更新する(fx_briefingの入口)。"""
    now = now or datetime.now(UTC)
    performances = {member: evaluate_member(member, entries, now=now) for member in MEMBERS}
    update_stages(state, performances, now=now, require_live_ack=require_live_ack)
    return state, performances
