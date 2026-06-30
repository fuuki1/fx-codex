"""プロ級リスクエンジン（純粋ロジック・外部 I/O 非依存）。

このモジュールは「トッププロが共通して重視するのは予測ではなくリスク管理」という
リサーチの結論を、再現可能なルールとして実装する中核である。すべて純粋関数／
データクラスで構成し、Redis/DB/ブローカー無しで単体テストできる。実際の I/O（口座残高・
損益・建玉・カレンダー読み込み・通知・Kill switch 操作）は呼び出し側 risk.py が担い、
ここには「状態を入力 → 判断を出力」だけを置く。

実装する統制と、その根拠（添付リサーチの対応箇所）:
  - リスク基準のポジションサイジング ……… サイズは確信ではなくストップ距離で決める
      （Kovner「サイズはストップで決まる」/ 1 取引リスクは口座の一定割合）。
  - 連敗スロットル ……………………………… 連敗時はサイズを縮小し、一定連敗で新規停止
      （Lipschutz「連敗時はサイズを落として自信を再建」/ 個人モデルの 3 連敗半減・5 連敗停止）。
  - 日次／週次の損失上限 ……………………… ドローダウン中に「退場しない」ための強制停止。
  - 相関エクスポージャ上限 …………………… 高相関ポジションを 1 つの巨大ポジションとして扱う
      （Lipschutz）。通貨レッグに分解して同方向の積み増しを抑える。
  - 同時保有数の上限 ………………………… 「最大 3 つ」。実は同じ賭けの重ね張りを防ぐ。
  - イベント・ブラックアウト ………………… 重要指標前後の新規を抑止（Marcus「反応を見る」/
      個人モデル「CPI・NFP・FOMC 前後は新規を制限」）。

判断は「上から順に 1 つでも引っかかれば却下」。各却下には機械可読な reason を付ける。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from domain import within_session

# 却下理由（ログ・テスト・通知で機械可読にするため定数化）
R_BLACKOUT = "event_blackout"
R_THIN_LIQUIDITY = "thin_liquidity_window"
R_SESSION = "out_of_session"
R_DRAWDOWN = "max_drawdown_exceeded"
R_DAILY_LOSS = "daily_loss_exceeded"
R_WEEKLY_LOSS = "weekly_loss_exceeded"
R_LOSS_STREAK = "loss_streak_halt"
R_NO_REASON = "missing_trade_reason"
R_REWARD_RISK = "reward_risk_too_low"
R_NO_TARGET = "missing_target_for_rr"
R_NO_STOP = "missing_stop_for_sizing"
R_STOP_TOO_WIDE = "stop_too_wide_for_risk"
R_QTY_LIMIT = "qty_over_limit"
R_MAX_POSITIONS = "max_concurrent_positions"
R_CURRENCY_EXPOSURE = "currency_exposure"


# ============================================================================
# パラメータ / 状態 / 判断の型
# ============================================================================
@dataclass(frozen=True)
class RiskParams:
    """静的なリスク設定（config から構築）。数値はリサーチを個人向けに保守変換した既定。"""

    # サイジング
    sizing_enabled: bool = False        # 既定 OFF（明示有効化するまで qty はシグナルのまま）
    account_equity: float = 1_000_000.0  # 口座残高（口座通貨。既定は仮値、必ず実値を設定）
    risk_per_trade_pct: float = 0.5      # 1 取引で許容する口座割合（%）
    require_stop_for_sizing: bool = False  # True なら stop 無しシグナルは却下
    lot_step: float = 1000.0             # 発注ロットの最小刻み（切り捨て）
    min_lot: float = 1000.0              # これ未満になるサイズは発注しない

    # 数量上限
    max_position_qty: float = 10_000.0

    # ポートフォリオ強制停止
    max_daily_loss: float = 50_000.0
    max_weekly_loss: float = 0.0         # 0 で無効

    # 連敗スロットル（reduce_at から段階的に縮小し、halt_at で停止）
    loss_streak_reduce_at: int = 3       # この連敗数からサイズ縮小を開始
    loss_streak_reduce_factor: float = 0.5
    loss_streak_halt_at: int = 5         # この連敗数で新規停止（0 で無効）

    # 相関・集中
    max_concurrent_positions: int = 3    # 同時に持てる別銘柄ポジション数（0 で無効）
    max_currency_exposure: float = 0.0   # 1 通貨あたりの純エクスポージャ上限（0 で無効）

    # 非対称性・規律・ドローダウン（第2層）
    min_reward_risk: float = 0.0         # 報酬/リスク比の下限（0 で無効）
    require_target_for_rr: bool = False  # True で利確目標の無いシグナルを却下
    require_reason: bool = False         # True で根拠（reason）の無いシグナルを却下
    max_drawdown: float = 0.0            # 実現損益の高値からの DD 額の上限（口座通貨, 0 で無効）

    enforce_session: bool = True


@dataclass(frozen=True)
class RiskState:
    """評価時に必要な外部状態（risk.py が I/O で集める）。"""

    now: object = None                    # datetime（within_session 用、None で現在時刻）
    day_pnl: float = 0.0                  # 当日の実現損益
    week_pnl: float = 0.0                 # 今週の実現損益
    recent_pnls: list[float] = field(default_factory=list)  # 直近の確定トレード損益（新しい順）
    open_positions: list[tuple[str, float]] = field(default_factory=list)  # (symbol, 符号付き数量)
    blackout_windows: list[tuple[float, float, str]] = field(default_factory=list)
    thin_liquidity_windows: list[tuple[int, int]] = field(default_factory=list)  # (開始分, 終了分) UTC
    equity_drawdown: float = 0.0          # 実現損益の高値からの現在ドローダウン額（口座通貨, >=0）
    value_per_point: float = 1.0          # この銘柄で価格 1.0 動いたときの 1 単位あたり損益（口座通貨）


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = ""
    sized_qty: float = 0.0
    intended_risk: float = 0.0            # 想定最大損失（口座通貨）。R 倍数計算の分母。
    trip_kill_switch: bool = False        # 日次／週次／連敗停止で True → 呼び出し側が Kill switch ON
    loss_streak: int = 0
    details: dict = field(default_factory=dict)


# ============================================================================
# 純粋ヘルパー
# ============================================================================
def decompose_pair(symbol: str) -> tuple[str, str] | None:
    """6 文字英字の FX シンボルを (base, quote) に分解。FX でなければ None。"""
    s = (symbol or "").upper()
    if len(s) == 6 and s.isalpha():
        return s[:3], s[3:]
    return None


def order_currency_legs(symbol: str, signed_qty: float) -> dict[str, float]:
    """符号付き数量（+ = base ロング）を通貨レッグへ分解。

    USDJPY を +1000 持つ = USD を +1000 / JPY を -1000。FX でなければ空。
    """
    pair = decompose_pair(symbol)
    if pair is None:
        return {}
    base, quote = pair
    return {base: signed_qty, quote: -signed_qty}


def net_currency_exposure(open_positions: list[tuple[str, float]]) -> dict[str, float]:
    """建玉一覧（symbol, 符号付き数量）を通貨ごとの純エクスポージャへ集約。"""
    exp: dict[str, float] = {}
    for symbol, signed in open_positions:
        for ccy, amt in order_currency_legs(symbol, signed).items():
            exp[ccy] = exp.get(ccy, 0.0) + amt
    return exp


def loss_streak(recent_pnls: list[float]) -> int:
    """直近（新しい順）の確定損益から連敗数を数える。

    損失(<0)で加算、勝ち(>0)で打ち切り、引き分け(==0)は無視（スキップ）。
    """
    streak = 0
    for pnl in recent_pnls:
        if pnl > 0:
            break
        if pnl < 0:
            streak += 1
    return streak


def streak_size_factor(streak: int, reduce_at: int, reduce_factor: float) -> float:
    """連敗数に応じたサイズ係数（段階的縮小・停止判定は別途 evaluate で行う）。

    reduce_at から連敗 1 つごとに reduce_factor を累乗で掛ける（例 0.5: 3連敗→0.5, 4連敗→0.25）。
    連敗が深まるほどサイズを落として再建する（Lipschutz）。
    """
    if reduce_at > 0 and streak >= reduce_at:
        return reduce_factor ** (streak - reduce_at + 1)
    return 1.0


def in_thin_liquidity(now: object, windows: list[tuple[int, int]]) -> bool:
    """現在（UTC 分）が薄商い窓に入っていれば True。start>end は日跨ぎとして扱う。"""
    if not windows:
        return False
    m = _minute_of_day_utc(now)
    for start, end in windows:
        if start <= end:
            if start <= m < end:
                return True
        elif m >= start or m < end:   # 日跨ぎ（例 23:30-00:30）
            return True
    return False


def reward_risk_ratio(tp_distance: float | None, stop_distance: float | None) -> float | None:
    """報酬/リスク比。利確距離・ストップ距離のどちらかが無効なら None。"""
    if not (tp_distance and tp_distance > 0 and stop_distance and stop_distance > 0):
        return None
    return tp_distance / stop_distance


def in_blackout(now_ts: float, windows: list[tuple[float, float, str]]) -> str | None:
    """now_ts がいずれかのブラックアウト窓に入っていればラベルを返す。"""
    for start, end, label in windows:
        if start <= now_ts <= end:
            return label or "blackout"
    return None


def floor_to_step(x: float, step: float) -> float:
    """発注ロット刻みへ切り捨て（過大発注を避けるため常に下方向）。"""
    if step <= 0:
        return x
    return math.floor(x / step) * step


def position_size(
    *,
    equity: float,
    risk_pct: float,
    stop_distance: float,
    value_per_point: float,
    lot_step: float,
    factor: float = 1.0,
) -> float:
    """リスク基準サイズを計算する。

    許容損失額 = 口座残高 × risk_pct% × factor（連敗時の縮小係数）。
    ストップ到達時の損失 = qty × stop_distance × value_per_point なので、
    qty = 許容損失額 ÷ (stop_distance × value_per_point) を満たす最大ロットへ切り捨てる。
    """
    if equity <= 0 or risk_pct <= 0 or stop_distance <= 0 or value_per_point <= 0:
        return 0.0
    risk_amount = equity * (risk_pct / 100.0) * factor
    raw = risk_amount / (stop_distance * value_per_point)
    return floor_to_step(raw, lot_step)


# ============================================================================
# 評価（オーケストレーション・純粋）
# ============================================================================
def evaluate(sig: dict, state: RiskState, params: RiskParams) -> RiskDecision:
    """シグナル＋状態＋設定から発注可否とサイズを決める純粋関数。

    注意: Kill switch とレート制限は Redis 副作用のため呼び出し側（risk.py）で扱う。
    ここでは「データに基づく判断」だけを返す。
    """
    symbol = str(sig.get("symbol", ""))
    asset = str(sig.get("asset", ""))
    side = str(sig.get("side", "")).upper()
    req_qty = float(sig.get("qty", 0) or 0)
    sd_raw = sig.get("stop_distance")
    stop_distance = float(sd_raw) if sd_raw not in (None, "") else None
    tp_raw = sig.get("tp_distance")
    tp_distance = float(tp_raw) if tp_raw not in (None, "") else None
    reason_text = str(sig.get("reason") or "").strip()

    streak = loss_streak(state.recent_pnls)

    def reject(reason: str, *, kill: bool = False, **detail: object) -> RiskDecision:
        return RiskDecision(
            approved=False, reason=reason, trip_kill_switch=kill,
            loss_streak=streak, details=dict(detail),
        )

    # 1) イベント・ブラックアウト（重要指標前後の新規を抑止）
    now_ts = _as_timestamp(state.now)
    label = in_blackout(now_ts, state.blackout_windows)
    if label is not None:
        return reject(R_BLACKOUT, label=label)

    # 2) 薄商い時間帯（流動性が薄い時間は新規しない）
    if in_thin_liquidity(state.now, state.thin_liquidity_windows):
        return reject(R_THIN_LIQUIDITY)

    # 3) 取引セッション（時間外は新規しない）
    if params.enforce_session and not within_session(asset, symbol, state.now):
        return reject(R_SESSION, asset=asset, symbol=symbol)

    # 4) 最大ドローダウン（実現損益の高値からの DD が上限超で強制停止 → Kill switch）
    if params.max_drawdown > 0 and state.equity_drawdown >= abs(params.max_drawdown):
        return reject(R_DRAWDOWN, kill=True, drawdown=state.equity_drawdown, limit=params.max_drawdown)

    # 5) 日次損失（超過で強制停止 → Kill switch）
    if state.day_pnl <= -abs(params.max_daily_loss):
        return reject(R_DAILY_LOSS, kill=True, day_pnl=state.day_pnl, limit=params.max_daily_loss)

    # 6) 週次損失（超過で強制停止 → Kill switch）
    if params.max_weekly_loss > 0 and state.week_pnl <= -abs(params.max_weekly_loss):
        return reject(R_WEEKLY_LOSS, kill=True, week_pnl=state.week_pnl, limit=params.max_weekly_loss)

    # 7) 連敗による新規停止（レビューに回す）。縮小係数は下のサイジングで使う。
    if params.loss_streak_halt_at > 0 and streak >= params.loss_streak_halt_at:
        return reject(R_LOSS_STREAK, kill=True, streak=streak, halt_at=params.loss_streak_halt_at)
    factor = streak_size_factor(streak, params.loss_streak_reduce_at, params.loss_streak_reduce_factor)

    # 8) 根拠の必須化（理由を文章化できないなら入らない）
    if params.require_reason and not reason_text:
        return reject(R_NO_REASON)

    # 9) 非対称性（報酬/リスク比）。「外れても小さく当たれば大きい」を満たさなければ却下。
    if params.min_reward_risk > 0:
        rr = reward_risk_ratio(tp_distance, stop_distance)
        if rr is None:
            if params.require_target_for_rr:
                return reject(R_NO_TARGET)
        elif rr < params.min_reward_risk:
            return reject(R_REWARD_RISK, reward_risk=round(rr, 3), min=params.min_reward_risk)

    # 10) サイジング（確信ではなくストップ距離と口座リスクで数量を決める）
    sized, intended_risk = _decide_size(
        req_qty=req_qty, stop_distance=stop_distance, factor=factor,
        vpp=state.value_per_point, params=params,
    )
    if sized is None:  # サイジング不能（ストップ必須なのに無い）
        return reject(R_NO_STOP)
    if sized <= 0:     # リスク予算に対してストップが広すぎ、最小ロットに満たない
        return reject(
            R_STOP_TOO_WIDE, stop_distance=stop_distance, min_lot=params.min_lot, factor=factor,
        )

    # 11) 数量上限（サイジング後の最終確認）
    if sized > params.max_position_qty:
        return reject(R_QTY_LIMIT, qty=sized, limit=params.max_position_qty)

    # 12) 同時保有数の上限（新規銘柄のみカウント）
    open_symbols = {s for s, q in state.open_positions if q != 0}
    if (
        params.max_concurrent_positions > 0
        and symbol not in open_symbols
        and len(open_symbols) >= params.max_concurrent_positions
    ):
        return reject(R_MAX_POSITIONS, open=len(open_symbols), limit=params.max_concurrent_positions)

    # 13) 通貨エクスポージャ上限（高相関ポジションを 1 つの賭けとして合算）
    if params.max_currency_exposure > 0:
        ccy = _exposure_breach(symbol, side, sized, state.open_positions, params.max_currency_exposure)
        if ccy is not None:
            return reject(R_CURRENCY_EXPOSURE, currency=ccy, limit=params.max_currency_exposure)

    return RiskDecision(
        approved=True, sized_qty=sized, intended_risk=intended_risk, loss_streak=streak,
        details={"factor": factor, "requested_qty": req_qty},
    )


def _decide_size(
    *, req_qty: float, stop_distance: float | None, factor: float, vpp: float, params: RiskParams
) -> tuple[float | None, float]:
    """最終発注数量と想定リスク額を返す。

    返り値 (sized, intended_risk):
      - sized is None  … サイジング不能（require_stop なのにストップ無し）→ 却下
      - sized == 0     … 最小ロット未満 → 却下
    """
    has_stop = stop_distance is not None and stop_distance > 0

    if params.sizing_enabled:
        if not has_stop:
            if params.require_stop_for_sizing:
                return None, 0.0
            # ストップ無し（手動シグナル等）: 縮小係数だけ適用して上限でクランプ
            sized = floor_to_step(min(req_qty * factor, params.max_position_qty), params.lot_step)
            return (sized if sized >= params.min_lot else 0.0), 0.0
        sized = position_size(
            equity=params.account_equity, risk_pct=params.risk_per_trade_pct,
            stop_distance=stop_distance, value_per_point=vpp, lot_step=params.lot_step, factor=factor,
        )
        sized = min(sized, params.max_position_qty)
        if sized < params.min_lot:
            return 0.0, 0.0
        intended_risk = sized * stop_distance * vpp
        return sized, intended_risk

    # サイジング無効: 既存挙動を維持しつつ、連敗縮小だけは効かせる
    sized = req_qty * factor
    intended_risk = sized * stop_distance * vpp if has_stop else 0.0
    return sized, intended_risk


def _exposure_breach(
    symbol: str,
    side: str,
    qty: float,
    open_positions: list[tuple[str, float]],
    cap: float,
) -> str | None:
    """この発注で上限超過する通貨があれば、その通貨コードを返す。

    既に上限を超えている通貨でも、エクスポージャを「減らす」発注は許す
    （手仕舞い・反対売買を妨げない）。
    """
    pair = decompose_pair(symbol)
    if pair is None:
        return None
    signed = qty if side == "BUY" else -qty
    current = net_currency_exposure(open_positions)
    legs = order_currency_legs(symbol, signed)
    for ccy, delta in legs.items():
        cur = current.get(ccy, 0.0)
        projected = cur + delta
        if abs(projected) > cap and abs(projected) > abs(cur):
            return ccy
    return None


def _as_timestamp(now: object) -> float:
    """datetime / epoch / None を UNIX 秒へ。None は現在時刻。"""
    import time
    from datetime import datetime

    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    return time.time()


def _minute_of_day_utc(now: object) -> int:
    """datetime / epoch / None を UTC の「その日の分（0..1439）」へ。"""
    from datetime import UTC, datetime

    if isinstance(now, datetime):
        u = now.astimezone(UTC)
    else:
        u = datetime.fromtimestamp(_as_timestamp(now), tz=UTC)
    return u.hour * 60 + u.minute
