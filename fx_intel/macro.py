"""マクロ市場データ層 — OHLC・米金利・ドル指数・VIX・CFTC COTの取得と品質ゲート。

APIキー不要の公開ソースのみを使う:

- Stooq        日次OHLC(FXペア)      https://stooq.com/q/d/l/?s=usdjpy&i=d
- FRED CSV     米10年/2年金利・VIX・広義ドル指数  fredgraph.csv(キー不要)
- CFTC Socrata COTレガシー先物(投機筋ポジション)  publicreporting.cftc.gov

ミッションクリティカル設計の原則:

1. 劣化はしても死なない — 各ソースは独立に失敗でき、失敗は warnings に
   記録されて snapshot.coverage() に反映される。呼び出し側(briefing)は
   カバレッジをデータ品質ゲートに使い、確信度を減衰する。
2. キャッシュ優先 — 公開ソースはレート制限があるため、TTL付きの
   ローカルキャッシュ(logs/macro_cache.json)を必ず経由する。
   ネットワーク失敗時も期限切れ・未来・時刻不明cacheは使用しない。
3. staleness ゲート — 日次系列は最終観測が7日超、COTは21日超で
   stale扱いとし、鮮度の落ちたデータが新鮮な顔で判断に混ざるのを防ぐ。
4. パースは純粋関数 — parse_stooq_csv / parse_fred_csv / parse_cot_json は
   ネットワーク非依存で、フィクスチャ文字列からテストできる。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, UTC
from pathlib import Path
from collections.abc import Mapping

import requests

USER_AGENT = "fx-codex-intel/1.0 (+https://github.com/fuuki1)"
FETCH_ATTEMPTS = 2
FETCH_RETRY_WAIT_SECONDS = 1.5
FETCH_TIMEOUT_SECONDS = 20.0

STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
CFTC_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# FREDの系列ID → スナップショット内のキーと日本語ラベル
FRED_SERIES: dict[str, tuple[str, str]] = {
    "VIXCLS": ("vix", "VIX(恐怖指数)"),
    "DGS10": ("us10y", "米10年金利"),
    "DGS2": ("us2y", "米2年金利"),
    "DTWEXBGS": ("usd_index", "広義ドル指数"),
}

# CFTCレガシーCOTの通貨先物コード(CME/ICE)。USDはICEドル指数先物
COT_CONTRACT_CODES: dict[str, str] = {
    "EUR": "099741",
    "JPY": "097741",
    "GBP": "096742",
    "CHF": "092741",
    "CAD": "090741",
    "AUD": "232741",
    "NZD": "112741",
    "USD": "098662",
}
_CODE_TO_CURRENCY = {code: ccy for ccy, code in COT_CONTRACT_CODES.items()}

DEFAULT_CACHE_TTL_HOURS = 6.0  # 日次系列は1日1回更新なので6時間で十分新鮮
COT_CACHE_TTL_HOURS = 24.0  # COTは週次(金曜発表)なので1日1回で十分
SERIES_STALE_DAYS = 7  # 日次系列: 最終観測がこれより古ければstale
COT_STALE_DAYS = 21  # COT: 週次+発表ラグ(約3日)を考慮した上限

# リスクレジーム判定の閾値(規則は透明に、判定理由を必ず文字列で返す)
VIX_RISK_OFF_LEVEL = 25.0
VIX_RISK_ON_LEVEL = 15.0
VIX_SPIKE_5D_PCT = 20.0  # 5営業日で+20%超はリスクオフ票
US10Y_PLUNGE_5D = -0.15  # 5営業日で-15bp超の金利低下は質への逃避票
USD_SQUEEZE_5D_PCT = 1.5  # ドル指数が5営業日で+1.5%超はドル資金逼迫票

# 通貨のリスク感応度: 負=安全通貨(リスクオフで買われる)、正=リスク通貨
RISK_SENSITIVITY: dict[str, float] = {
    "JPY": -1.0,
    "CHF": -0.8,
    "USD": -0.5,
    "EUR": 0.0,
    "GBP": 0.3,
    "CAD": 0.5,
    "AUD": 0.8,
    "NZD": 0.8,
}


@dataclass(frozen=True)
class SeriesPoint:
    when: date
    value: float


@dataclass
class MacroSeries:
    """日次時系列1本(昇順ソート済み)。"""

    key: str
    label_ja: str
    points: list[SeriesPoint] = field(default_factory=list)

    def last(self) -> SeriesPoint | None:
        return self.points[-1] if self.points else None

    def change(self, points_back: int) -> float | None:
        """直近値と points_back 観測前の値の差(観測ベース=営業日ベース)。"""
        if len(self.points) <= points_back:
            return None
        return self.points[-1].value - self.points[-1 - points_back].value

    def change_pct(self, points_back: int) -> float | None:
        if len(self.points) <= points_back:
            return None
        past = self.points[-1 - points_back].value
        if past == 0:
            return None
        return (self.points[-1].value - past) / abs(past) * 100.0

    def is_stale(self, now: datetime, max_age_days: int = SERIES_STALE_DAYS) -> bool:
        last = self.last()
        if last is None:
            return True
        # A negative age is not freshness: it is a point-in-time violation.  Keep
        # this invariant on the value object as well as the network/cache loader so
        # manually constructed and deserialized snapshots cannot bypass it.
        if any(point.when > now.date() for point in self.points):
            return True
        return (now.date() - last.when).days > max_age_days


@dataclass(frozen=True)
class CotReport:
    """投機筋(非商業)ポジションの1通貨ぶんの最新レポート。"""

    currency: str
    report_date: date
    net_position: int  # 非商業ロング − 非商業ショート(枚)
    open_interest: int
    prev_net_position: int | None = None  # 前週の純ポジション(変化の把握用)

    @property
    def net_ratio(self) -> float:
        """純ポジション ÷ 建玉。-1.0〜+1.0程度に収まる正規化値。"""
        if self.open_interest <= 0:
            return 0.0
        return self.net_position / self.open_interest

    def is_stale(self, now: datetime, max_age_days: int = COT_STALE_DAYS) -> bool:
        if self.report_date > now.date():
            return True
        return (now.date() - self.report_date).days > max_age_days


@dataclass
class MacroSnapshot:
    """マクロデータの取得結果。欠けているものは warnings に理由が残る。"""

    fetched_at: datetime
    series: dict[str, MacroSeries] = field(default_factory=dict)
    cot: dict[str, CotReport] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def fresh_series(self, key: str) -> MacroSeries | None:
        """stale でない系列だけを返す(staleは無いのと同じ扱い)。"""
        found = self.series.get(key)
        if found is None or found.is_stale(self.fetched_at):
            return None
        return found

    def fresh_cot(self, currency: str) -> CotReport | None:
        found = self.cot.get(currency)
        if found is None or found.is_stale(self.fetched_at):
            return None
        return found

    def coverage(self) -> float:
        """新鮮に取れているデータの割合(0.0〜1.0)。品質ゲートの入力。

        系列4本(VIX/10年/2年/ドル指数)で0.7、COTで0.3の重み。
        """
        fresh = sum(1 for key in FRED_SERIES.values() if self.fresh_series(key[0]) is not None)
        series_part = fresh / len(FRED_SERIES) if FRED_SERIES else 0.0
        cot_fresh = sum(1 for ccy in COT_CONTRACT_CODES if self.fresh_cot(ccy) is not None)
        cot_part = cot_fresh / len(COT_CONTRACT_CODES) if COT_CONTRACT_CODES else 0.0
        return round(0.7 * series_part + 0.3 * cot_part, 3)

    def regime(self) -> tuple[str, str]:
        """クロスアセット実データからリスクレジームを判定する。

        戻り値は (risk_on / risk_off / neutral, 判定理由の日本語)。
        語彙ベースの雰囲気ではなく、VIX水準・VIX急騰・金利急低下・
        ドル指数急騰という監査可能な固定規則の多数決で決める。
        データが無ければ判定しない(neutral + 理由)。
        """
        votes_off: list[str] = []
        votes_on: list[str] = []

        vix = self.fresh_series("vix")
        vix_last = vix.last() if vix is not None else None
        if vix is not None and vix_last is not None:
            level = vix_last.value
            if level >= VIX_RISK_OFF_LEVEL:
                votes_off.append(f"VIX {level:.1f}が警戒水準({VIX_RISK_OFF_LEVEL:.0f})以上")
            spike = vix.change_pct(5)
            if spike is not None and spike >= VIX_SPIKE_5D_PCT:
                votes_off.append(f"VIXが5営業日で+{spike:.0f}%急騰")
            if level <= VIX_RISK_ON_LEVEL and (spike is None or spike < VIX_SPIKE_5D_PCT):
                votes_on.append(f"VIX {level:.1f}が低位({VIX_RISK_ON_LEVEL:.0f}以下)で安定")

        us10y = self.fresh_series("us10y")
        if us10y is not None:
            move = us10y.change(5)
            if move is not None and move <= US10Y_PLUNGE_5D:
                votes_off.append(f"米10年金利が5営業日で{move * 100:+.0f}bp低下(質への逃避)")

        usd_index = self.fresh_series("usd_index")
        if usd_index is not None:
            move_pct = usd_index.change_pct(5)
            if move_pct is not None and move_pct >= USD_SQUEEZE_5D_PCT:
                votes_off.append(f"ドル指数が5営業日で+{move_pct:.1f}%上昇(ドル資金逼迫)")

        if not votes_off and not votes_on:
            return "neutral", "マクロデータ不足または明確なシグナルなし"
        if len(votes_off) > len(votes_on):
            return "risk_off", "、".join(votes_off)
        if len(votes_on) > len(votes_off):
            return "risk_on", "、".join(votes_on)
        return "neutral", "リスクオン/オフの材料が拮抗"


# ---------------------------------------------------------------- パース(純粋関数)


def parse_stooq_csv(text: str) -> list[SeriesPoint]:
    """StooqのOHLC CSVから終値系列を読む(昇順)。

    ヘッダは Date,Open,High,Low,Close[,Volume]。壊れた行はスキップ。
    """
    points: list[SeriesPoint] = []
    lines = text.strip().splitlines()
    for line in lines[1:]:  # ヘッダを飛ばす
        cells = line.split(",")
        if len(cells) < 5:
            continue
        try:
            when = date.fromisoformat(cells[0].strip())
            close = float(cells[4])
        except ValueError:
            continue
        points.append(SeriesPoint(when=when, value=close))
    points.sort(key=lambda p: p.when)
    return points


def parse_stooq_ohlc_csv(text: str) -> list[dict]:
    """StooqのCSVをOHLC辞書の列(昇順)として読む。バックテスト連携用。"""
    rows: list[dict] = []
    for line in text.strip().splitlines()[1:]:
        cells = line.split(",")
        if len(cells) < 5:
            continue
        try:
            rows.append(
                {
                    "date": date.fromisoformat(cells[0].strip()),
                    "open": float(cells[1]),
                    "high": float(cells[2]),
                    "low": float(cells[3]),
                    "close": float(cells[4]),
                }
            )
        except ValueError:
            continue
    rows.sort(key=lambda r: r["date"])
    return rows


def parse_fred_csv(text: str) -> list[SeriesPoint]:
    """FREDのfredgraph.csvを読む。欠測は "." で入るためスキップ(昇順)。"""
    points: list[SeriesPoint] = []
    for line in text.strip().splitlines()[1:]:
        cells = line.split(",")
        if len(cells) < 2:
            continue
        raw_value = cells[1].strip()
        if raw_value in (".", ""):
            continue
        try:
            when = date.fromisoformat(cells[0].strip())
            value = float(raw_value)
        except ValueError:
            continue
        points.append(SeriesPoint(when=when, value=value))
    points.sort(key=lambda p: p.when)
    return points


def parse_cot_json(payload: object) -> dict[str, CotReport]:
    """CFTC SocrataのJSON配列から通貨別の最新COTレポートを組み立てる。

    同一通貨の複数レポートが混ざっている前提で、最新を採用し
    直前週があれば prev_net_position に載せる。数値はSocrata仕様で
    文字列のため、変換できない行はスキップ。
    """
    if not isinstance(payload, list):
        return {}
    per_currency: dict[str, list[tuple[date, int, int]]] = {}
    for row in payload:
        if not isinstance(row, Mapping):
            continue
        code = str(row.get("cftc_contract_market_code", "")).strip()
        currency = _CODE_TO_CURRENCY.get(code)
        if currency is None:
            continue
        try:
            report_date = date.fromisoformat(str(row.get("report_date_as_yyyy_mm_dd", ""))[:10])
            longs = int(float(row.get("noncomm_positions_long_all", "")))
            shorts = int(float(row.get("noncomm_positions_short_all", "")))
            open_interest = int(float(row.get("open_interest_all", "")))
        except (TypeError, ValueError):
            continue
        per_currency.setdefault(currency, []).append((report_date, longs - shorts, open_interest))

    result: dict[str, CotReport] = {}
    for currency, rows in per_currency.items():
        rows.sort(key=lambda r: r[0])
        latest = rows[-1]
        prev_net = rows[-2][1] if len(rows) >= 2 else None
        result[currency] = CotReport(
            currency=currency,
            report_date=latest[0],
            net_position=latest[1],
            open_interest=latest[2],
            prev_net_position=prev_net,
        )
    return result


# ---------------------------------------------------------------- キャッシュ付き取得


def _load_cache(cache_path: Path) -> dict:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # キャッシュ保存の失敗は致命的ではない(次回は再取得になるだけ)


def _cache_age_hours(entry: Mapping, now: datetime) -> float | None:
    try:
        fetched_at = datetime.fromisoformat(str(entry.get("fetched_at", "")))
    except ValueError:
        return None
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        return None
    age = (now - fetched_at.astimezone(UTC)).total_seconds() / 3600.0
    return age if age >= 0 else None


def _fetch_text(url: str, session: requests.Session | None = None) -> str:
    """再試行付きのテキスト取得。最終失敗時は例外を投げる。"""
    http = session or requests
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            response = http.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return response.text
        except Exception as error:  # noqa: BLE001 - 外部API起因
            last_error = error
            if attempt + 1 < FETCH_ATTEMPTS:
                time.sleep(FETCH_RETRY_WAIT_SECONDS)
    raise RuntimeError(f"{url} の取得に失敗: {last_error}")


def _cached_fetch(
    cache: dict,
    key: str,
    url: str,
    now: datetime,
    ttl_hours: float,
    warnings: list[str],
    session: requests.Session | None = None,
) -> str | None:
    """TTLキャッシュ経由の取得。期限切れ・曖昧・未来cacheは使用しない。"""
    entry = cache.get(key)
    if isinstance(entry, Mapping):
        age = _cache_age_hours(entry, now)
        if age is not None and age <= ttl_hours and isinstance(entry.get("body"), str):
            return entry["body"]
    try:
        body = _fetch_text(url, session=session)
    except RuntimeError as error:
        if isinstance(entry, Mapping) and isinstance(entry.get("body"), str):
            age = _cache_age_hours(entry, now)
            age_label = "時刻不正" if age is None else f"約{age:.0f}時間前"
            warnings.append(
                f"マクロ取得失敗かつcacheを鮮度証明できないため拒否"
                f"({key}, {age_label}): {error}"
            )
            return None
        warnings.append(f"マクロ取得失敗({key}): {error}")
        return None
    cache[key] = {"fetched_at": now.isoformat(), "body": body}
    return body


def _cot_query_url(limit: int = 200) -> str:
    codes = ",".join(f"'{code}'" for code in sorted(COT_CONTRACT_CODES.values()))
    where = f"cftc_contract_market_code in({codes})"
    return (
        f"{CFTC_COT_URL}?$where={requests.utils.quote(where)}"
        f"&$order=report_date_as_yyyy_mm_dd%20DESC&$limit={limit}"
    )


def fetch_macro_snapshot(
    cache_path: str | Path,
    now: datetime | None = None,
    ttl_hours: float = DEFAULT_CACHE_TTL_HOURS,
    include_cot: bool = True,
    session: requests.Session | None = None,
) -> MacroSnapshot:
    """マクロスナップショットを取得する(全ソース独立・失敗は警告に降格)。

    キャッシュが新鮮ならネットワークに一切触れないため、テストでは
    キャッシュを事前に仕込むことでオフライン検証できる。
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("macro snapshot now must be timezone-aware")
    now = now.astimezone(UTC)
    cache_file = Path(cache_path)
    cache = _load_cache(cache_file)
    warnings: list[str] = []
    snapshot = MacroSnapshot(fetched_at=now)

    for series_id, (key, label_ja) in FRED_SERIES.items():
        body = _cached_fetch(
            cache,
            f"fred_{series_id}",
            FRED_CSV_URL.format(series=series_id),
            now,
            ttl_hours,
            warnings,
            session=session,
        )
        if body is None:
            continue
        points = parse_fred_csv(body)
        if not points:
            warnings.append(f"マクロ系列 {label_ja}({series_id}) のパース結果が空")
            continue
        series = MacroSeries(key=key, label_ja=label_ja, points=points[-260:])
        last_point = series.last()
        if last_point is not None and last_point.when > now.date():
            warnings.append(
                f"マクロ系列 {label_ja} の未来観測を拒否({last_point.when.isoformat()})"
            )
            continue
        if series.is_stale(now) and last_point is not None:
            warnings.append(f"マクロ系列 {label_ja} が古い(最終観測 {last_point.when.isoformat()})")
            continue
        snapshot.series[key] = series

    if include_cot:
        body = _cached_fetch(
            cache,
            "cftc_cot",
            _cot_query_url(),
            now,
            COT_CACHE_TTL_HOURS,
            warnings,
            session=session,
        )
        if body is not None:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
                warnings.append("COTレポートのJSONパースに失敗")
            if payload is not None:
                parsed_cot = parse_cot_json(payload)
                if not parsed_cot:
                    warnings.append("COTレポートに既知の通貨先物が見つからない")
                else:
                    future = [
                        ccy for ccy, report in parsed_cot.items() if report.report_date > now.date()
                    ]
                    if future:
                        warnings.append(f"COTレポートの未来観測を拒否: {', '.join(sorted(future))}")
                    stale = [ccy for ccy, report in parsed_cot.items() if report.is_stale(now)]
                    if stale:
                        warnings.append(f"COTレポートが古い: {', '.join(sorted(stale))}")
                    snapshot.cot = {
                        ccy: report
                        for ccy, report in parsed_cot.items()
                        if ccy not in future and ccy not in stale
                    }

    snapshot.warnings = warnings
    _save_cache(cache_file, cache)
    return snapshot


# ---------------------------------------------------------------- ペア別マクロ見解


def cot_pair_score(base: str, quote: str, snapshot: MacroSnapshot) -> tuple[float, str] | None:
    """投機筋ポジショニング差からペアの方向スコア(-1〜+1)を出す。

    net_ratio(純ポジ÷建玉)は通貨先物でおおむね±0.3に収まるため、
    差を3倍してクリップする。どちらかの通貨のCOTが無ければ判定しない。
    """
    base_report = snapshot.fresh_cot(base)
    quote_report = snapshot.fresh_cot(quote)
    if base_report is None or quote_report is None:
        return None
    diff = base_report.net_ratio - quote_report.net_ratio
    score = max(-1.0, min(1.0, diff * 3.0))
    note = (
        f"投機筋ポジション(COT {base_report.report_date:%m/%d}): "
        f"{base} 純ポジ比率{base_report.net_ratio:+.2f} vs {quote} {quote_report.net_ratio:+.2f}"
    )
    return round(score, 3), note


def regime_pair_score(base: str, quote: str, regime: str) -> tuple[float, str] | None:
    """リスクレジームと通貨のリスク感応度からペアの方向スコアを出す。

    リスクオフでは感応度の低い(安全)通貨が買われるため、
    score = (sens(quote) − sens(base)) / 2 をリスクオフの符号とし、
    リスクオンでは反転する。中立レジームでは判定しない。
    """
    if regime not in ("risk_on", "risk_off"):
        return None
    base_sens = RISK_SENSITIVITY.get(base)
    quote_sens = RISK_SENSITIVITY.get(quote)
    if base_sens is None or quote_sens is None:
        return None
    raw = (quote_sens - base_sens) / 2.0
    score = raw if regime == "risk_off" else -raw
    score = max(-1.0, min(1.0, score))
    if abs(score) < 0.05:
        return None
    regime_ja = "リスクオフ" if regime == "risk_off" else "リスクオン"
    favored = base if score > 0 else quote
    note = f"{regime_ja}地合いでは{favored}が相対的に買われやすい"
    return round(score, 3), note


def macro_pair_view(
    symbol_base: str, symbol_quote: str, snapshot: MacroSnapshot
) -> tuple[float, float, list[str]]:
    """ペアに対するマクロ総合見解 (スコア, 確信度, 根拠一覧) を返す。

    COT(重み0.6)とレジーム整合(重み0.4)の加重平均。片方しか無ければ
    その分だけで判定し、確信度を下げる。どちらも無ければ (0, 0, [])。
    """
    parts: list[tuple[float, float, str]] = []  # (score, weight, note)
    cot = cot_pair_score(symbol_base, symbol_quote, snapshot)
    if cot is not None:
        parts.append((cot[0], 0.6, cot[1]))
    regime, regime_note = snapshot.regime()
    aligned = regime_pair_score(symbol_base, symbol_quote, regime)
    if aligned is not None:
        parts.append((aligned[0], 0.4, f"{aligned[1]}({regime_note})"))
    if not parts:
        return 0.0, 0.0, []
    total_weight = sum(weight for _, weight, _ in parts)
    score = sum(score * weight for score, weight, _ in parts) / total_weight
    confidence = total_weight  # 両方そろって1.0
    return round(score, 3), round(confidence, 3), [note for _, _, note in parts]
