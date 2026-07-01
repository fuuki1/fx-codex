"""アドバイザリー・ダッシュボード（分析ループ + Web 表示 + Discord 通知）。

**実売買はしない。** OANDA から USDJPY のローソクを定期取得し、MTF(下位足=タイミング /
上位足=トレンド)で売買タイミングを助言する。分析中のチャート（ローソク + MA + 売買マーカー +
損切り/利確ライン）を Web で表示し、好機に達したら Discord へ通知する。Mac mini 上で
24/365 常駐させる前提（docker restart:always / launchd watchdog）。

構成:
  - バックグラウンド thread が analyzer ループを回し、最新スナップショットを保持。
  - FastAPI が `/`（チャート）・`/api/state`（JSON）・`/health` を提供。
  - 通知は状態変化時のみ（アラート嵐を防ぐ・common.notify を throttle 無しで使用）。
"""
from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import analysis
import common
import indicators as ind
import numpy as np
import oanda
import pandas as pd
from config import settings
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from logging_setup import setup_logging

log = setup_logging("dashboard", settings.log_level)

# 最新の分析スナップショット（thread 間で共有）。
_state_lock = threading.Lock()
_state: dict[str, Any] = {"ok": False, "reason": "starting", "updated": 0.0}
_last_alert_key: str | None = None


def params_from_settings() -> dict[str, Any]:
    return {
        "fast_window": settings.analyzer_fast_window,
        "slow_window": settings.analyzer_slow_window,
        "atr_window": settings.analyzer_atr_window,
        "atr_multiple": settings.analyzer_atr_multiple,
        "rr_target": settings.analyzer_rr_target,
        # レジーム対応・多因子合議のパラメータ
        "er_window": settings.analyzer_er_window,
        "rsi_window": settings.analyzer_rsi_window,
        "roc_window": settings.analyzer_roc_window,
        "bb_window": settings.analyzer_bb_window,
        "donchian_window": settings.analyzer_donchian_window,
        "atr_lookback": settings.analyzer_atr_lookback,
        "signal_threshold": settings.analyzer_signal_threshold,
        "adx_window": settings.analyzer_atr_window,  # ADX は ATR と同窓を既定に
        "adx_trend": settings.analyzer_adx_trend,
        "adx_range": settings.analyzer_adx_range,
        "er_trend": settings.analyzer_er_trend,
        "er_range": settings.analyzer_er_range,
    }


# ============================================================================
# チャート用ペイロード（Lightweight Charts 形式）
# ============================================================================
def build_chart_payload(ltf: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """ローソク + fast/slow EMA + クロス・マーカーを Lightweight Charts 形式で返す。"""
    fast = int(params.get("fast_window", 20))
    slow = int(params.get("slow_window", 60))
    df = ltf.reset_index(drop=True)
    times = [int(x.timestamp()) for x in df["time"]]
    fast_ma = ind.ema(df["close"], fast)
    slow_ma = ind.ema(df["close"], slow)

    candles = [
        {
            "time": times[i], "open": float(df["open"][i]), "high": float(df["high"][i]),
            "low": float(df["low"][i]), "close": float(df["close"][i]),
        }
        for i in range(len(df))
    ]

    def line(series: pd.Series) -> list[dict[str, Any]]:
        return [
            {"time": times[i], "value": float(series.iloc[i])}
            for i in range(len(df))
            if pd.notna(series.iloc[i])
        ]

    diff = fast_ma - slow_ma
    markers: list[dict[str, Any]] = []
    for i in range(1, len(df)):
        a, b = diff.iloc[i - 1], diff.iloc[i]
        if pd.isna(a) or pd.isna(b):
            continue
        sa = int(np.sign(a))
        sb = int(np.sign(b))
        if sb != 0 and sa != sb:
            up = sb > 0
            markers.append(
                {
                    "time": times[i],
                    "position": "belowBar" if up else "aboveBar",
                    "color": "#26a69a" if up else "#ef5350",
                    "shape": "arrowUp" if up else "arrowDown",
                    "text": "BUY" if up else "SELL",
                }
            )
    return {"candles": candles, "fast_ma": line(fast_ma), "slow_ma": line(slow_ma),
            "markers": markers, "fast_window": fast, "slow_window": slow}


# ============================================================================
# 分析ループ
# ============================================================================
def run_once() -> None:
    params = params_from_settings()
    ltf = oanda.fetch_candles(
        settings.oanda_instrument, settings.analyzer_ltf_granularity, settings.analyzer_candles
    )
    htf = oanda.fetch_candles(
        settings.oanda_instrument, settings.analyzer_htf_granularity, settings.analyzer_candles
    )
    if ltf is None or htf is None or ltf.empty or htf.empty:
        reason = (
            "OANDA トークン未設定（.env の OANDA_API_TOKEN）"
            if not settings.oanda_api_token
            else "OANDA からデータを取得できません（接続/銘柄設定を確認）"
        )
        with _state_lock:
            _state.update(ok=False, reason=reason, updated=time.time())
        return

    symbol = settings.oanda_instrument.replace("_", "")
    rec = analysis.analyze(ltf, htf, params, symbol=symbol)
    chart = build_chart_payload(ltf, params)
    with _state_lock:
        _state.update(
            ok=True, reason="", updated=time.time(),
            symbol=symbol,
            htf=settings.analyzer_htf_granularity, ltf=settings.analyzer_ltf_granularity,
            recommendation=rec.to_dict(), chart=chart,
        )
    _maybe_notify(rec)


def _maybe_notify(rec: analysis.Recommendation) -> None:
    """状態（action + strength）が変わったら Discord へ通知（WAIT は通知しない）。"""
    global _last_alert_key
    key = f"{rec.action}:{rec.strength}"
    if rec.action == "WAIT":
        _last_alert_key = key
        return
    if key == _last_alert_key:
        return
    _last_alert_key = key
    common.notify(format_discord(rec), throttle=False)


def format_discord(rec: analysis.Recommendation) -> str:
    icon = "🟢" if rec.action == "BUY" else "🔴"
    tag = "好機（クロス発生）" if rec.strength == "strong" else "セットアップ（押し目/戻り待ち）"
    symbol = settings.oanda_instrument.replace("_", "")
    if rec.stop and rec.take_profit:
        levels = (
            f"現値 {rec.last_price:.3f} / 損切り {rec.stop:.3f} / 利確 {rec.take_profit:.3f}"
            f"（{rec.rr:.1f}R）"
        )
    else:
        levels = f"現値 {rec.last_price:.3f}"
    lines = [
        f"{icon} **{rec.action}** シグナル [{tag}] {symbol}",
        levels,
        "・" + "\n・".join(rec.reasons),
        "※ これは助言です。発注はしません。",
    ]
    return "\n".join(lines)


def _loop(stop: threading.Event) -> None:
    log.info("analyzer loop started")
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            log.exception("analyzer loop error")
        stop.wait(settings.analyzer_interval_sec)
    log.info("analyzer loop stopped")


# ============================================================================
# FastAPI
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(stop,), daemon=True, name="analyzer")
    thread.start()
    app.state.stop = stop
    log.info("dashboard started", extra={})
    yield
    stop.set()


app = FastAPI(title="fx-advisor", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> JSONResponse:
    with _state_lock:
        ok = bool(_state.get("ok"))
        updated = _state.get("updated", 0.0)
    # 分析が一定時間更新されていなければ degraded（監視で拾える）。
    fresh = (time.time() - updated) < max(settings.analyzer_interval_sec * 4, 120)
    healthy = ok and fresh
    return JSONResponse({"status": "ok" if healthy else "degraded", "analysis_ok": ok},
                        status_code=200 if healthy else 503)


@app.get("/api/state")
def api_state() -> JSONResponse:
    with _state_lock:
        return JSONResponse(dict(_state))


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_PAGE)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.dashboard_port,
                log_config=None, access_log=False)


# ============================================================================
# フロントエンド（Lightweight Charts / TradingView 製・無料）。ポーリング更新。
# ============================================================================
_PAGE = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FX アドバイザー</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0e1117;color:#e6e6e6;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  header{padding:12px 16px;border-bottom:1px solid #232838;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
  header h1{font-size:16px;margin:0}
  .muted{color:#8b93a7;font-size:12px}
  #banner{margin:12px 16px;padding:14px 16px;border-radius:10px;font-size:15px;line-height:1.7;border:1px solid #232838}
  .BUY{background:#0f2e22;border-color:#1f5e46}
  .SELL{background:#2e1416;border-color:#5e2427}
  .WAIT{background:#171b26;border-color:#2a3040}
  #banner .act{font-size:20px;font-weight:700}
  #banner ul{margin:8px 0 0;padding-left:20px}
  #wrap{margin:0 16px 16px}
  #chart{height:60vh;border:1px solid #232838;border-radius:10px;overflow:hidden}
  .lvls{display:flex;gap:18px;flex-wrap:wrap;margin-top:6px;font-variant-numeric:tabular-nums}
  .lvls b{color:#fff}
</style></head>
<body>
<header>
  <h1>FX アドバイザー <span id="sym" class="muted"></span></h1>
  <span id="tf" class="muted"></span>
  <span id="updated" class="muted"></span>
  <span class="muted">※ 助言のみ・発注はしません</span>
</header>
<div id="banner" class="WAIT"><span class="act">読み込み中…</span></div>
<div id="wrap"><div id="chart"></div></div>
<script>
const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout:{background:{color:'#0e1117'},textColor:'#c7ccd8'},
  grid:{vertLines:{color:'#1a1f2b'},horzLines:{color:'#1a1f2b'}},
  timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#232838'},
  rightPriceScale:{borderColor:'#232838'},
});
const candle = chart.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',
  wickUpColor:'#26a69a',wickDownColor:'#ef5350',borderVisible:false});
const fastLine = chart.addLineSeries({color:'#f0b90b',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
const slowLine = chart.addLineSeries({color:'#2962ff',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
let priceLines = [];
new ResizeObserver(()=>chart.applyOptions({})).observe(document.getElementById('chart'));

function fmt(x){return (x==null)?'-':Number(x).toFixed(3);}
function sgn(x){return (x==null)?'-':(x>=0?'+':'')+Number(x).toFixed(2);}

function render(s){
  const b = document.getElementById('banner');
  document.getElementById('sym').textContent = s.symbol||'';
  document.getElementById('tf').textContent = s.ltf&&s.htf ? ('タイミング '+s.ltf+' / トレンド '+s.htf) : '';
  document.getElementById('updated').textContent = s.updated ? ('更新 '+new Date(s.updated*1000).toLocaleTimeString()) : '';
  if(!s.ok){
    b.className='WAIT'; b.innerHTML='<span class="act">待機</span> <span class="muted">'+(s.reason||'')+'</span>';
    return;
  }
  const r = s.recommendation;
  b.className = r.action;
  let html = '<span class="act">'+(r.action==='BUY'?'🟢 BUY':r.action==='SELL'?'🔴 SELL':'⚪ WAIT')+'</span>';
  html += ' <span class="muted">'+(r.strength==='strong'?'好機（クロス発生）':r.strength==='setup'?'セットアップ':'')+'</span>';
  // レジーム・確信度・スコア（分析の"中身"）
  html += '<div class="lvls"><span>レジーム <b>'+(r.regime||'-')+'</b>（上位 '+(r.regime_htf||'-')+'）</span>'
        + '<span>確信度 <b>'+Math.round((r.conviction||0)*100)+'%</b></span>'
        + '<span>ADX <b>'+(r.adx!=null?r.adx:'-')+'</b> 効率比 <b>'+(r.efficiency_ratio!=null?r.efficiency_ratio:'-')+'</b></span>'
        + '<span>合議 下位 <b>'+sgn(r.score_ltf)+'</b> / 上位 <b>'+sgn(r.score_htf)+'</b></span></div>';
  if(r.factors){
    html += '<div class="lvls"><span class="muted">因子</span>'
          + Object.keys(r.factors).map(k=>'<span>'+k+' <b>'+sgn(r.factors[k])+'</b></span>').join('') + '</div>';
  }
  if(r.action!=='WAIT'){
    html += '<div class="lvls"><span>現値 <b>'+fmt(r.last_price)+'</b></span>'
          + '<span>損切り <b>'+fmt(r.stop)+'</b></span>'
          + '<span>利確 <b>'+fmt(r.take_profit)+'</b></span>'
          + '<span>R:R <b>'+(r.rr?Number(r.rr).toFixed(1):'-')+'</b></span></div>';
  }
  html += '<ul>'+ (r.reasons||[]).map(x=>'<li>'+x+'</li>').join('') +'</ul>';
  b.innerHTML = html;

  const c = s.chart;
  candle.setData(c.candles);
  fastLine.setData(c.fast_ma);
  slowLine.setData(c.slow_ma);
  candle.setMarkers(c.markers||[]);
  priceLines.forEach(pl=>candle.removePriceLine(pl)); priceLines=[];
  if(r.action!=='WAIT'){
    const add=(price,color,title)=>{ if(price!=null) priceLines.push(candle.createPriceLine(
      {price:Number(price),color:color,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:title})); };
    add(r.entry,'#c7ccd8','ENTRY'); add(r.stop,'#ef5350','STOP'); add(r.take_profit,'#26a69a','TP');
  }
}

async function tick(){
  try{ const s = await (await fetch('/api/state',{cache:'no-store'})).json(); render(s); }
  catch(e){ /* keep last view */ }
}
tick(); setInterval(tick, 5000);
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
