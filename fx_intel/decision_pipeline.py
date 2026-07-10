"""9段チェックリスト型の意思決定パイプライン。

ユーザーが求めた「機関投資家デスクの発注前チェックリスト」を、順序どおりの
明示的なゲート列として実装する。各ステップは CheckStep として結果(ok/warn/
block)と理由を残すため、なぜその判断になったかを1判断まるごと監査できる。

    1. MAクロス            — 自作MAクロス戦略の目線があるか
    2. 市場レジーム判定     — リスクオン/オフ等のレジームと方向の整合
    3. 上位足との整合       — 上位足(4h/1d)がエントリー方向と揃っているか
    4. ボラティリティ確認   — ATRが過小/過大でないか(SL/TP算出可能か)
    5. 流動性・スプレッド確認 — スプレッドがSL距離に対して許容内か
    6. ニュース・金利・イベント確認 — 高影響イベント窓・カレンダー欠損
    7. 期待値計算           — TP/SL込みの期待R(確信度と勝率から素の期待値)
    8. 執行コスト控除       — スプレッド+スリッページを期待Rから差し引く
    9. ポジションサイズ決定 — 口座リスク%とSL距離からロットを算出

このモジュールは build_trade_plan(=リスクオフィサーの決定論ゲート)の
上位互換ラッパー。build_trade_plan が既に計算している値(方向・確信度・
SL/TP・イベント窓・品質)を順序付きチェックリストに写像し、コードに未実装
だった 5(スプレッド)/8(執行コスト)/9(サイズ)の3ステップを足す。

追加のサードパーティ依存は無し(標準ライブラリのみ。Mac miniの軽量venvに
そのまま移設できる — analyst/macro/gbm 等と同じ方針)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from collections.abc import Callable, Mapping, Sequence

from .briefing import (
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_RISK_PCT,
    DEFAULT_TARGET1_R,
    ScoreComponent,
    TargetRAdjuster,
    TradePlan,
    build_trade_plan,
)
from .market import is_market_open
from .technicals import PairTechnicals

# --- 実行コスト/流動性のしきい値 ---------------------------------------------
# スプレッドがSL距離のこの割合を超えたら「流動性が薄い/コスト過大」で警告。
# さらに BLOCK 側の閾値を超えたら新規エントリーを見送る。
SPREAD_WARN_FRACTION = 0.10  # SL距離の10%
SPREAD_BLOCK_FRACTION = 0.25  # SL距離の25%(これ以上はエッジをコストが食い潰す)

# 想定スリッページ。約定は次足始値・成行前提なので、スプレッドに上乗せする。
# スプレッド1本ぶんを既定のスリッページ見積りとする(保守的)。
DEFAULT_SLIPPAGE_SPREADS = 1.0

# ボラティリティ(ATR%)の許容レンジ。過小はダマシ/コスト負け、過大は
# ストップ幅が広がりすぎてサイズが取れない。方向判断は変えず警告に留める。
ATR_PCT_MIN = 0.02  # 0.02%未満は動意薄
ATR_PCT_MAX = 1.50  # 1.5%超は異常なボラ

# 期待値の素点(勝率×TP - 敗率×SL)を確信度から見積もる際の勝率換算。
# conviction=100 で勝率 WIN_RATE_AT_FULL、conviction=0 で 0.5(五分)。
WIN_RATE_AT_FULL = 0.62

STATUS_EMOJI = {"ok": "✅", "warn": "⚠️", "block": "⛔", "skip": "➖"}


@dataclass
class CheckStep:
    """チェックリスト1ステップの結果。"""

    order: int
    key: str
    label_ja: str
    status: str  # "ok" / "warn" / "block" / "skip"
    note: str = ""

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI.get(self.status, "•")

    def line_ja(self) -> str:
        body = f"{self.emoji} {self.order}. {self.label_ja}"
        return f"{body} — {self.note}" if self.note else body

    def to_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "key": self.key,
            "label_ja": self.label_ja,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class DecisionChecklist:
    """9ステップぶんの結果と最終判断のまとめ。"""

    symbol: str
    steps: list[CheckStep] = field(default_factory=list)
    # 8以降で確定する実務値
    expected_r: float | None = None  # 執行コスト控除前の素の期待R
    net_expected_r: float | None = None  # スプレッド+スリッページ控除後
    execution_cost_r: float | None = None  # 控除したコスト(R換算)
    position_units: float | None = None  # ポジションサイズ(通貨単位/ロット)
    expectancy_source: str = ""
    probability_calibrated: bool = False

    @property
    def blocked(self) -> bool:
        return any(step.status == "block" for step in self.steps)

    @property
    def passed(self) -> bool:
        """全ステップが ok(見送り系ステップの block が無い)か。"""
        return not self.blocked

    def summary_ja(self) -> str:
        return "\n".join(step.line_ja() for step in self.steps)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "blocked": self.blocked,
            "expected_r": self.expected_r,
            "net_expected_r": self.net_expected_r,
            "execution_cost_r": self.execution_cost_r,
            "position_units": self.position_units,
            "expectancy_source": self.expectancy_source,
            "probability_calibrated": self.probability_calibrated,
            "steps": [step.to_dict() for step in self.steps],
        }


# --- 個別ステップの純粋関数(テストしやすいよう外だし) -----------------------


def _spread_at(tech: PairTechnicals, interval: str = "1h") -> float | None:
    """指定時間足のスプレッド(価格差)。無ければ None。"""
    view = tech.views.get(interval)
    if view is None:
        return None
    return view.spread


def estimate_expected_r(direction: str, conviction: int, target1_r: float) -> float:
    """確信度から素の期待R(執行コスト控除前)を見積もる。

    勝率 p = 0.5 + (conviction/100) * (WIN_RATE_AT_FULL - 0.5)
    期待R = p * target1_r - (1 - p) * 1.0   (負ければ -1R=SLに触れる想定)

    確信度が高いほど勝率が上がり、TPが遠い(target1_r大)ほど1勝の重みが増す。
    あくまで方向的中を含んだ素の理論値で、実測の期待R(maximization.py)が
    あればそちらが優先される(この関数はフォールバックの説明用途)。
    """
    if direction not in ("long", "short"):
        return 0.0
    p = 0.5 + (conviction / 100.0) * (WIN_RATE_AT_FULL - 0.5)
    p = max(0.0, min(1.0, p))
    return round(p * target1_r - (1.0 - p) * 1.0, 3)


def execution_cost_in_r(
    spread: float | None,
    stop_distance: float | None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
) -> float | None:
    """スプレッド+スリッページをR(=SL距離)換算で返す。

    1トレードのコスト = スプレッド * (1 + slippage_spreads)  [価格]
    R換算 = コスト / SL距離
    SL距離やスプレッドが不明なら None。
    """
    if spread is None or stop_distance is None or stop_distance <= 0:
        return None
    cost_price = spread * (1.0 + max(0.0, slippage_spreads))
    return round(cost_price / stop_distance, 4)


def position_units(
    account_balance: float | None,
    risk_pct: float,
    stop_distance: float | None,
) -> float | None:
    """口座残高・リスク%・SL距離(価格)からポジションサイズ(通貨単位)を出す。

    許容損失額 = 残高 * risk_pct/100
    サイズ     = 許容損失額 / SL距離
    残高が不明なら None(サイズはライブ発注側=executorが確定する想定)。
    """
    if account_balance is None or account_balance <= 0:
        return None
    if stop_distance is None or stop_distance <= 0:
        return None
    risk_amount = account_balance * (risk_pct / 100.0)
    return round(risk_amount / stop_distance, 2)


def build_checklist(
    plan: TradePlan,
    tech: PairTechnicals,
    *,
    now: datetime | None = None,
    account_balance: float | None = None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
    realized_expectancy_r: float | None = None,
    calibrated_win_probability: float | None = None,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
) -> DecisionChecklist:
    """完成した TradePlan を 9ステップのチェックリストへ写像する。

    plan は build_trade_plan(=リスクオフィサー)が既に決めた最終判断。ここでは
    「どのステップがなぜそう判定されたか」を順序どおりに開示し、コードに未実装
    だったスプレッド/執行コスト/サイズの3ステップを追記する。

    realized_expectancy_r があれば期待値ステップにその実測値を使う
    (maximization.py の実測期待R)。無ければ確信度からの素点を使う。
    """
    now = now or datetime.now(UTC)
    checklist = DecisionChecklist(symbol=plan.symbol)
    steps = checklist.steps
    directional = plan.direction in ("long", "short")

    # 1. MAクロス -------------------------------------------------------------
    ma_side = tech.ma_side("1h")
    if ma_side is None:
        steps.append(
            CheckStep(1, "ma_cross", "MAクロス", "warn", "MAクロスの目線を確定できず(MA未取得)")
        )
    else:
        steps.append(
            CheckStep(
                1,
                "ma_cross",
                "MAクロス",
                "ok",
                f"1h MAクロスは{_side_ja(ma_side)}目線 ({plan.ma_note})",
            )
        )

    # 2. 市場レジーム判定 -----------------------------------------------------
    if not is_market_open(now):
        steps.append(
            CheckStep(2, "regime", "市場レジーム判定", "block", "FX市場休場中(週末クローズ)")
        )
    else:
        steps.append(
            CheckStep(
                2,
                "regime",
                "市場レジーム判定",
                "ok",
                "市場オープン中。レジーム整合は委員会スコアに反映済み",
            )
        )

    # 3. 上位足との整合 -------------------------------------------------------
    agreement = tech.agreement_ratio()
    higher_side = tech.ma_side("4h") or tech.ma_side("1d")
    if agreement is None:
        steps.append(
            CheckStep(
                3,
                "htf_alignment",
                "上位足との整合",
                "warn",
                "上位足の向きを判定できず(全時間足中立/未取得)",
            )
        )
    elif directional and higher_side is not None and higher_side != plan.direction:
        steps.append(
            CheckStep(
                3,
                "htf_alignment",
                "上位足との整合",
                "warn",
                f"上位足({_side_ja(higher_side)})がエントリー方向({_side_ja(plan.direction)})と逆行 — 一致度{agreement:.0%}",
            )
        )
    else:
        steps.append(
            CheckStep(
                3, "htf_alignment", "上位足との整合", "ok", f"時間足の向き一致度 {agreement:.0%}"
            )
        )

    # 4. ボラティリティ確認 ---------------------------------------------------
    atr_pct = None
    close = plan.close
    if plan.atr is not None and close:
        atr_pct = plan.atr / close * 100.0
    if plan.atr is None or plan.atr <= 0:
        steps.append(
            CheckStep(
                4, "volatility", "ボラティリティ確認", "block", "ATR(1h)取得失敗 — SL/TP算出不能"
            )
        )
    elif atr_pct is not None and atr_pct < ATR_PCT_MIN:
        steps.append(
            CheckStep(
                4,
                "volatility",
                "ボラティリティ確認",
                "warn",
                f"ボラ過小(ATR {atr_pct:.3f}%) — 値動き乏しくコスト負けしやすい",
            )
        )
    elif atr_pct is not None and atr_pct > ATR_PCT_MAX:
        steps.append(
            CheckStep(
                4,
                "volatility",
                "ボラティリティ確認",
                "warn",
                f"ボラ過大(ATR {atr_pct:.2f}%) — ストップ幅が広くサイズを絞る必要",
            )
        )
    else:
        label = f"ATR {atr_pct:.3f}%" if atr_pct is not None else f"ATR {plan.atr:.5f}"
        steps.append(CheckStep(4, "volatility", "ボラティリティ確認", "ok", f"ボラ正常 ({label})"))

    # 5. 流動性・スプレッド確認 -----------------------------------------------
    spread = _spread_at(tech, "1h")
    stop_distance = None
    if plan.stop is not None and close is not None:
        stop_distance = abs(close - plan.stop)
    if spread is None:
        steps.append(
            CheckStep(
                5, "spread", "流動性・スプレッド確認", "warn", "スプレッド不明(bid/ask未取得)"
            )
        )
    elif stop_distance is None or stop_distance <= 0:
        steps.append(
            CheckStep(
                5,
                "spread",
                "流動性・スプレッド確認",
                "skip",
                "SL距離未確定のためスプレッド比を評価せず",
            )
        )
    else:
        frac = spread / stop_distance
        if frac >= SPREAD_BLOCK_FRACTION:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "block",
                    f"スプレッドがSL距離の{frac:.0%} — コストがエッジを食い潰す",
                )
            )
        elif frac >= SPREAD_WARN_FRACTION:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "warn",
                    f"スプレッドがSL距離の{frac:.0%} — 流動性やや薄い",
                )
            )
        else:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "ok",
                    f"スプレッドはSL距離の{frac:.0%} — 許容内",
                )
            )

    # 6. ニュース・金利・イベント確認 -----------------------------------------
    event_note = next(
        (w for w in plan.warnings if "イベント" in w or "カレンダー" in w),
        "",
    )
    if not operational_data_ok:
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "block",
                "運用データ鮮度ゲート: "
                + (operational_data_reason or "正常性を証明できず新規リスク停止"),
            )
        )
    elif plan.direction == "standby":
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "block",
                event_note or "高影響イベント窓のため新規は様子見",
            )
        )
    elif event_note:
        steps.append(CheckStep(6, "event", "ニュース・金利・イベント確認", "warn", event_note))
    else:
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "ok",
                "警戒イベント窓なし・カレンダー取得済み",
            )
        )

    # 7. 期待値計算 -----------------------------------------------------------
    target1_r = _target1_r(plan)
    if realized_expectancy_r is not None:
        expected_r = round(realized_expectancy_r, 3)
        exp_src = "実測(TP/SL履歴)"
        expectancy_valid = True
    elif calibrated_win_probability is not None and directional:
        if not 0.0 <= calibrated_win_probability <= 1.0:
            raise ValueError("calibrated_win_probability must be in [0, 1]")
        expected_r = round(
            calibrated_win_probability * target1_r - (1.0 - calibrated_win_probability),
            3,
        )
        exp_src = "分離期間で較正済み確率"
        expectancy_valid = True
        checklist.probability_calibrated = True
    elif directional:
        expected_r = estimate_expected_r(plan.direction, plan.conviction, target1_r)
        exp_src = "未較正の確信度ヒューリスティック(参考値)"
        expectancy_valid = False
    else:
        expected_r = None
        exp_src = ""
        expectancy_valid = False
    checklist.expected_r = expected_r
    checklist.expectancy_source = exp_src
    if not directional:
        steps.append(
            CheckStep(
                7, "expectancy", "期待値計算", "skip", "方向判断が無いため期待値評価をスキップ"
            )
        )
    elif expected_r is None:
        steps.append(CheckStep(7, "expectancy", "期待値計算", "warn", "期待値を算出できず"))
    elif not expectancy_valid:
        steps.append(
            CheckStep(
                7,
                "expectancy",
                "期待値計算",
                "block",
                f"期待{expected_r:+.2f}R({exp_src}) — 未較正値を発注ゲートへ使用不可",
            )
        )
    elif expected_r <= 0:
        steps.append(
            CheckStep(
                7,
                "expectancy",
                "期待値計算",
                "block",
                f"期待{expected_r:+.2f}R({exp_src}) — 期待値が非正",
            )
        )
    else:
        steps.append(
            CheckStep(7, "expectancy", "期待値計算", "ok", f"期待{expected_r:+.2f}R({exp_src})")
        )

    # 8. 執行コスト控除 -------------------------------------------------------
    cost_r = execution_cost_in_r(spread, stop_distance, slippage_spreads)
    checklist.execution_cost_r = cost_r
    if not directional or expected_r is None:
        steps.append(
            CheckStep(
                8, "execution_cost", "執行コスト控除", "skip", "期待値が無いため控除評価をスキップ"
            )
        )
    elif cost_r is None:
        checklist.net_expected_r = None
        steps.append(
            CheckStep(
                8,
                "execution_cost",
                "執行コスト控除",
                "block",
                "スプレッド/SL距離不明でコストを控除できないため発注不可",
            )
        )
    else:
        net = round(expected_r - cost_r, 3)
        checklist.net_expected_r = net
        if net <= 0:
            steps.append(
                CheckStep(
                    8,
                    "execution_cost",
                    "執行コスト控除",
                    "block",
                    f"執行コスト {cost_r:.2f}R控除後の期待{net:+.2f}R — コスト負け",
                )
            )
        else:
            steps.append(
                CheckStep(
                    8,
                    "execution_cost",
                    "執行コスト控除",
                    "ok",
                    f"コスト {cost_r:.2f}R控除後の純期待{net:+.2f}R",
                )
            )

    # 9. ポジションサイズ決定 -------------------------------------------------
    units = position_units(account_balance, plan.risk_pct, stop_distance)
    checklist.position_units = units
    if not directional or checklist.blocked:
        steps.append(
            CheckStep(
                9,
                "position_size",
                "ポジションサイズ決定",
                "skip",
                "エントリー見送り(前段でブロック/方向無し)のためサイズ算出せず",
            )
        )
    elif units is None:
        if account_balance is None:
            steps.append(
                CheckStep(
                    9,
                    "position_size",
                    "ポジションサイズ決定",
                    "ok",
                    f"口座リスク{plan.risk_pct:.1f}%/1トレード。実サイズは発注側で残高から確定",
                )
            )
        else:
            steps.append(
                CheckStep(
                    9,
                    "position_size",
                    "ポジションサイズ決定",
                    "warn",
                    "SL距離未確定のためサイズを算出できず",
                )
            )
    else:
        steps.append(
            CheckStep(
                9,
                "position_size",
                "ポジションサイズ決定",
                "ok",
                f"{units:,.0f}通貨単位(残高の{plan.risk_pct:.1f}%リスク / SL距離基準)",
            )
        )

    return checklist


def run_pipeline(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping,
    windows: Sequence,
    news_items: Sequence,
    *,
    now: datetime | None = None,
    account_balance: float | None = None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
    realized_expectancy_r: float | None = None,
    calibrated_win_probability: float | None = None,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    extra_components: Sequence[ScoreComponent] = (),
    expectancy_adjuster: Callable[[str, str, int], tuple[float, str, bool]] | None = None,
    target_r_adjuster: TargetRAdjuster | None = None,
    **plan_kwargs,
) -> tuple[TradePlan, DecisionChecklist]:
    """build_trade_plan を走らせ、その結果をチェックリストに写像して両方返す。

    既存の build_trade_plan の全機能(学習調整・委員会・期待値ガード・TP/SL
    承認)をそのまま通したうえで、順序付きチェックリストを付ける薄いラッパー。
    """
    plan = build_trade_plan(
        symbol,
        tech,
        currency_scores,
        windows,
        news_items,
        now=now,
        atr_multiple=atr_multiple,
        risk_pct=risk_pct,
        calendar_ok=calendar_ok,
        operational_data_ok=operational_data_ok,
        operational_data_reason=operational_data_reason,
        extra_components=extra_components,
        expectancy_adjuster=expectancy_adjuster,
        target_r_adjuster=target_r_adjuster,
        **plan_kwargs,
    )
    checklist = build_checklist(
        plan,
        tech,
        now=now,
        account_balance=account_balance,
        slippage_spreads=slippage_spreads,
        realized_expectancy_r=realized_expectancy_r,
        calibrated_win_probability=calibrated_win_probability,
        operational_data_ok=operational_data_ok,
        operational_data_reason=operational_data_reason,
    )
    return plan, checklist


def _side_ja(side: str) -> str:
    return {"long": "ロング", "short": "ショート"}.get(side, side)


def _target1_r(plan: TradePlan) -> float:
    policy = plan.target_policy or {}
    value = policy.get("target1_r")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return DEFAULT_TARGET1_R
