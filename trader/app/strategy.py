"""④ 自作戦略ロジック（MA クロス + ATR ストップ）。

- strategy_interval_sec ごとに decide() を実行。
- シグナルがあれば webhook と同じ `signals` ストリームへ publish
  （= リスク管理・執行を共有）。
- strategy_params.json を mtime 監視してホットリロード（再起動不要）。
  auto_optimize.py が書き出す最良パラメータを自動で取り込む。

安全のため STRATEGY_ENABLED=0（既定）では一切シグナルを出さない。
シグナルはポジション状態が変化した時だけ出す（毎ループの連投を防ぐ）。
価格データは IB の historical bars を使う（取得失敗時はアイドル）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import common
import pandas as pd
from config import IB_BAR_SIZE_STR, settings
from domain import compute_idem
from logging_setup import log_extra, setup_logging
from params_gate import load_validated_params

log = setup_logging("strategy", settings.log_level)

DEFAULT_PARAMS = {"fast_window": 20, "slow_window": 60, "atr_window": 14, "atr_multiple": 2.0}
STATE_KEY = "strategy:state"  # hash: symbol -> -1/0/1
# 取得本数の余裕。ma_cross_signal は slow+1 本、compute_atr は末尾 atr_window 本を
# 使うため、必要本数は slow + atr_window。加えて欠損バーやウォームアップの取りこぼしに
# 備えて係数と定数の下駄を履かせる（取り過ぎ側は安全、取り不足はシグナル沈黙になる）。
_FETCH_HEADROOM_FACTOR = 1.5
_FETCH_HEADROOM_CONST = 20
# IB reqHistoricalData は 1 リクエストの期間に上限がある。バー間隔ごとの目安（秒）。
# ここを超える期間は要求せず頭打ちにする（過大要求でのタイムアウト/pacing 違反を避ける）。
# slow_window 上限までは、いずれのバー間隔でもこの上限内に必要本数が収まる。
_IB_MAX_DURATION_SEC = {
    5: 7_200,      # 5 secs: ~2h
    10: 14_400,
    15: 14_400,
    30: 28_800,
    60: 86_400,    # 1 min: 1D
    300: 604_800,  # 5 mins: 1W
    900: 2_592_000,
    1800: 2_592_000,
    3600: 2_592_000,  # 1 hour: ~1M
}
# ファイル欠落を表すセンチネル mtime（実 mtime とは衝突しない負値）。
# 削除が続く間、欠落イベントを一度だけ記録するために _notified_mtime に載せる。
_MISSING_MTIME = -1.0


# ============================================================================
# 純粋ロジック（テスト可能）
# ============================================================================
def compute_atr(df: pd.DataFrame, window: int) -> float:
    """ATR を返す。high/low があれば true range、無ければ close 差分の絶対値。"""
    if {"high", "low", "close"}.issubset(df.columns):
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
            axis=1,
        ).max(axis=1)
    else:
        tr = df["close"].diff().abs()
    atr = tr.rolling(window).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0


def ma_cross_signal(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    """最新バーでの目標ポジションとストップ距離を返す。

    返り値: {"target": -1|0|1, "stop_distance": float} もしくは None（データ不足）。
    """
    fast = int(params.get("fast_window", DEFAULT_PARAMS["fast_window"]))
    slow = int(params.get("slow_window", DEFAULT_PARAMS["slow_window"]))
    atr_w = int(params.get("atr_window", DEFAULT_PARAMS["atr_window"]))
    atr_m = float(params.get("atr_multiple", DEFAULT_PARAMS["atr_multiple"]))
    if fast >= slow or len(df) < slow + 1:
        return None

    fast_ma = df["close"].rolling(fast).mean().iloc[-1]
    slow_ma = df["close"].rolling(slow).mean().iloc[-1]
    if pd.isna(fast_ma) or pd.isna(slow_ma):
        return None

    target = 1 if fast_ma > slow_ma else -1 if fast_ma < slow_ma else 0
    atr = compute_atr(df, atr_w)
    return {"target": target, "stop_distance": atr * atr_m}


# ============================================================================
# パラメータのホットリロード
# ============================================================================
class ParamStore:
    """strategy_params.json を監視し、params_gate を通過した値だけをホットリロードする。

    検証（provenance・境界値・取引数など）に落ちたファイル、および削除された
    ファイルは適用しない。挙動は「直近に一度でも合格した値があるか」で分岐する:

    - 合格値がある: 汚染更新が来ても直近の合格値を維持する（params_rejected を記録）。
      合格後にファイルが削除された場合も直近合格値を維持する（params_missing を記録）。
    - 合格値が一度も無い: get() は None を返す（params_unavailable を記録）。
      呼び出し側はシグナルを出してはならない。検証されていないパラメータでの
      新規発注、および「一度も検証を通っていない状態での DEFAULT 発注」を防ぐ。

    いずれの異常も無音では継続しない（イベントを記録する）。DEFAULT_PARAMS は
    合格値のスキーマ欠落キーを穴埋めする下地としてのみ使い、フォールバックの
    発注根拠にはしない。拒否/欠落/削除の警告は同じ状態につき一度だけ。
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float = 0.0
        self._notified_mtime: float | None = None
        # None = 検証済みパラメータが一度も無い（= 発注不可）。
        self.params: dict[str, Any] | None = None

    def get(self) -> dict[str, Any] | None:
        """有効な検証済みパラメータを返す。無ければ None（呼び出し側は発注しない）。"""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            # ファイルが無い。削除が続く限り一度だけ記録する（センチネル mtime を使う）。
            if self.params is not None:
                # 一度合格した後に削除された → 直近合格値を維持しつつ params_missing を記録。
                self._notify_once(
                    _MISSING_MTIME,
                    "params_missing",
                    "params file missing after a valid load; keeping previous validated params",
                    ["パラメータファイルが存在しない"],
                )
            else:
                # 合格値が一度も無い → 発注不可のまま params_unavailable を記録。
                self._notify_once(
                    _MISSING_MTIME,
                    "params_unavailable",
                    "no validated params available; strategy will not emit signals",
                    ["パラメータファイルが存在しない"],
                )
            return self.params
        except Exception:
            log.exception("failed to stat params file; keeping previous")
            return self.params

        if mtime == self._mtime:
            return self.params

        params, errors = load_validated_params(
            self.path, expected_bar_interval_sec=settings.strategy_bar_size_sec
        )
        if errors or params is None:
            # 拒否/読み込み不能。再検証を毎ループ走らせないよう mtime は進める（指摘5）。
            self._mtime = mtime
            if self.params is not None:
                # 直近合格値がある → それを維持（params_rejected）。
                self._notify_once(
                    mtime,
                    "params_rejected",
                    "params rejected by gate; keeping previous validated params",
                    errors,
                )
            else:
                # 合格値が一度も無い → 発注不可のまま（params_unavailable）。
                self._notify_once(
                    mtime,
                    "params_unavailable",
                    "no validated params available; strategy will not emit signals",
                    errors,
                )
            return self.params

        # 合格: 反映。DEFAULT_PARAMS はスキーマ外キー欠落時の保険として下地に敷く。
        self.params = {**DEFAULT_PARAMS, **params}
        self._mtime = mtime
        self._notified_mtime = None
        log.info("params reloaded (gate passed)", **log_extra(params=self.params))
        return self.params

    def _notify_once(self, mtime: float, kind: str, message: str, errors: list[str]) -> None:
        if self._notified_mtime == mtime:
            return
        log.warning(message, **log_extra(errors=errors, active_params=self.params))
        common.log_event(
            kind, {"path": str(self.path), "errors": errors, "active_params": self.params}
        )
        self._notified_mtime = mtime


# ============================================================================
# 価格取得（IB historical bars, 取得失敗時は None）
# ============================================================================
def required_bars(slow_window: int, atr_window: int) -> int:
    """シグナル生成に必要な最小バー数に余裕を加えた取得本数。

    ma_cross_signal は slow+1 本、compute_atr は末尾 atr_window 本を使う。
    欠損バー等に備えて係数・定数の下駄を履かせる。**ゲート受理範囲**（PARAM_BOUNDS）の
    上限 slow_window でもデータ不足でシグナルが沈黙しないことを保証するのが目的。
    """
    need = slow_window + atr_window
    return int(need * _FETCH_HEADROOM_FACTOR) + _FETCH_HEADROOM_CONST


def duration_str(bars: int, bar_size_sec: int) -> str:
    """必要バー数とバー間隔から IB reqHistoricalData の durationStr を組み立てる。

    バー間隔ごとの 1 リクエスト上限（_IB_MAX_DURATION_SEC）で頭打ちにし、
    秒 → 日 → 週へ単位を繰り上げる（IB は "N S"/"N D"/"N W" を受理する）。
    IB の実バーは営業時間により歯抜けになるため、期間は必要秒数の 2 倍を要求して
    余裕を持たせる（過大要求は上限でクリップされる）。
    """
    span_sec = bars * bar_size_sec * 2
    cap = _IB_MAX_DURATION_SEC.get(bar_size_sec, 86_400)
    span_sec = min(span_sec, cap)
    if span_sec <= 86_400:
        return f"{max(span_sec, 60)} S"
    days = -(-span_sec // 86_400)  # ceil
    if days <= 7:
        return f"{days} D"
    weeks = -(-days // 7)  # ceil
    return f"{weeks} W"


def fetch_prices(
    ib: Any,
    symbol: str,
    asset: str,
    *,
    slow_window: int,
    atr_window: int,
    bar_size_sec: int | None = None,
) -> pd.DataFrame | None:
    bar_size_sec = bar_size_sec if bar_size_sec is not None else settings.strategy_bar_size_sec
    bar_size_str = IB_BAR_SIZE_STR[bar_size_sec]
    bars = required_bars(slow_window, atr_window)
    try:
        if asset.lower() in ("fx", "forex"):
            from ib_async import Forex

            contract: Any = Forex(symbol)
        else:
            from ib_async import Stock

            contract = Stock(symbol, "SMART", "USD")
        data = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration_str(bars, bar_size_sec),
            barSizeSetting=bar_size_str,
            whatToShow="MIDPOINT",
            useRTH=False,
        )
        if not data:
            return None
        df = pd.DataFrame([{"high": b.high, "low": b.low, "close": b.close} for b in data])
        # ゲート受理範囲でも履歴が足りなければ沈黙する前に検知する（無音の沈黙を防ぐ）。
        if len(df) < slow_window + 1:
            log.warning(
                "insufficient bars for slow_window; signal will be silent",
                **log_extra(
                    got=len(df), need=slow_window + 1, slow_window=slow_window,
                    bar_size_sec=bar_size_sec,
                ),
            )
        return df
    except Exception:
        log.exception("fetch_prices failed")
        return None


# ============================================================================
# シグナル発行（状態変化時のみ）
# ============================================================================
def _actual_position(ib: Any, symbol: str) -> float | None:
    """IB の実建玉（符号付き数量）を返す。読めなければ None（Redis 状態へフォールバック）。

    Forex の contract.symbol は基軸通貨のみ（USDJPY → "USD"）のため、
    localSymbol（"USD.JPY"）と symbol+currency の連結でも照合する。
    一致する建玉が無い場合は 0.0（フラット）を返す。
    """
    try:
        total = 0.0
        found = False
        for p in ib.positions():
            c = p.contract
            local = str(getattr(c, "localSymbol", "") or "").replace(".", "").upper()
            pair = (
                str(getattr(c, "symbol", "") or "") + str(getattr(c, "currency", "") or "")
            ).upper()
            plain = str(getattr(c, "symbol", "") or "").upper()
            if symbol.upper() in (local, pair, plain):
                total += float(p.position)
                found = True
        return total if found else 0.0
    except Exception:
        log.exception("failed to read actual position", **log_extra(symbol=symbol))
        return None


def emit_if_changed(
    symbol: str,
    asset: str,
    target: int,
    stop_distance: float,
    price: float,
    actual_position: float | None = None,
) -> None:
    """目標方向（target）へ建玉を寄せる差分注文を signals へ発行する。

    数量は「目標建玉（target×STRATEGY_QTY）− 現在建玉」の差分。反転（+1↔-1）は
    2 倍量になり、フラット化で止まらず実際にドテンする（従来は固定 STRATEGY_QTY を
    送っていたため、反転シグナルでも建玉は 0 になるだけで、Redis の状態(-1/+1)と
    実建玉が乖離し、さらにブラケットの子ストップが「存在しない建玉」を守る形で残る
    → 到達時に意図しない新規ポジションを作っていた）。

    現在建玉は IB の実建玉（actual_position）を優先し、読めない時だけ Redis の
    前回状態から推定する（保護ストップ約定後など、状態と実建玉が乖離していても
    過不足のない数量になる）。position_qty には発注後の想定建玉サイズを載せる
    （executor が保護ストップの数量に使う。qty はドテン時 2 倍のため使えない）。
    """
    prev: str | None = common.sync(common.r().hget(STATE_KEY, symbol))
    prev_state = int(prev) if prev is not None else 0
    if target == prev_state or target == 0:
        return
    if stop_distance <= 0 or price <= 0:
        # ATR が計算できない（データ不足等）状態でストップ無し発注はしない
        log.warning(
            "no valid stop -> skip signal",
            **log_extra(symbol=symbol, stop_distance=stop_distance, price=price),
        )
        return

    if actual_position is not None:
        current = actual_position
        if current != prev_state * settings.strategy_qty:
            # 状態乖離（保護ストップ約定・手動介入・部分約定等）。観測ログに残す。
            log.warning(
                "position state divergence",
                **log_extra(symbol=symbol, redis_state=prev_state, actual=current),
            )
            common.log_event(
                "position_divergence",
                {"symbol": symbol, "redis_state": prev_state, "actual_position": current},
            )
    else:
        current = prev_state * settings.strategy_qty

    target_qty = target * settings.strategy_qty
    delta = target_qty - current
    if delta == 0:
        # 既に目標建玉（乖離時にありうる）。状態だけ実態へ同期して終わる。
        common.r().hset(STATE_KEY, symbol, str(target))
        return
    if (delta > 0) != (target > 0):
        # 現建玉が目標を同方向に超過（手動介入疑い）。自動で減らさず人に上げる。
        log.error(
            "position exceeds target -> manual check",
            **log_extra(symbol=symbol, actual=current, target_qty=target_qty),
        )
        common.notify(
            f"⚠️ 実建玉が戦略の目標を超過: {symbol} 実建玉={current:g} 目標={target_qty:g}。"
            f"自動発注を見送りました。手動で確認してください。",
            key=f"position_over:{symbol}",
        )
        return

    side = "BUY" if delta > 0 else "SELL"
    raw = {
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": abs(delta),
        "position_qty": settings.strategy_qty,
        "type": "MARKET",
        "ts": time.time(),
        "price": price,
        "stop_distance": stop_distance,
    }
    sig = {
        "source": "strategy",
        **raw,
        "idem": compute_idem({**raw, "id": f"strat-{symbol}-{int(time.time())}"}),
    }
    common.log_event("signal_received", sig)
    common.publish(common.STREAM_SIGNALS, sig)
    # redis は値を文字列化して保存する。hget 側は int(prev) で読み戻す（str で明示）。
    common.r().hset(STATE_KEY, symbol, str(target))
    log.info(
        "strategy signal",
        **log_extra(symbol=symbol, side=side, target=target, qty=abs(delta)),
    )


def main() -> None:
    stop = common.install_signal_handlers()
    params = ParamStore(settings.strategy_params_file)

    if not settings.strategy_enabled:
        log.info("strategy disabled (STRATEGY_ENABLED=0) -> heartbeat only")
        while not stop.is_set():
            common.heartbeat("strategy")
            stop.wait(settings.strategy_interval_sec)
        return

    ib = None
    try:
        from ib_async import IB

        ib = IB()
        ib.connect(settings.ib_host, settings.ib_port, clientId=settings.ib_client_id + 70, timeout=15)
        log.info("strategy connected to IB")
        try:
            # 実建玉の購読（emit_if_changed の差分サイジングに使う）。失敗しても
            # Redis 状態ベースのサイジングで動けるため、接続自体は落とさない。
            ib.reqPositions()
        except Exception:
            log.exception("reqPositions failed -> state-based sizing fallback")
    except Exception:
        log.exception("strategy could not connect to IB -> idle loop")

    while not stop.is_set():
        common.heartbeat("strategy")
        try:
            active_params = params.get()
            # 検証済みパラメータが無い間はシグナルを出さない（未検証パラメータや
            # 一度も検証を通っていない DEFAULT での新規発注を防ぐ）。価格取得もしない。
            if active_params is not None and ib is not None and ib.isConnected():
                df = fetch_prices(
                    ib,
                    settings.strategy_symbol,
                    settings.strategy_asset,
                    slow_window=int(active_params["slow_window"]),
                    atr_window=int(active_params["atr_window"]),
                )
                if df is not None:
                    sig = ma_cross_signal(df, active_params)
                    if sig is not None:
                        emit_if_changed(
                            settings.strategy_symbol,
                            settings.strategy_asset,
                            sig["target"],
                            sig["stop_distance"],
                            float(df["close"].iloc[-1]),
                            actual_position=_actual_position(ib, settings.strategy_symbol),
                        )
                ib.sleep(0.2)
        except Exception:
            log.exception("strategy loop error")
        stop.wait(settings.strategy_interval_sec)

    if ib is not None and ib.isConnected():
        ib.disconnect()
    log.info("strategy stopped")


if __name__ == "__main__":
    main()
