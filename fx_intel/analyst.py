"""自前分析エンジン — 外部LLM APIに依存しないヘッドライン解釈と市況統合。

「Claude級の汎用知能」をローカルで再現することはできない。代わりに、
FXヘッドライン解釈という狭いタスクに特化した決定論的エンジンを提供する。
LLMと違い、同じ入力からは必ず同じ判断が出て、判断根拠がすべて
コードとして監査できる。ミッションクリティカルな売買助言では
この再現性・監査可能性がブラックボックスの言語能力より重要になる。

sentiment.py の単純語彙カウントとの違い:

1. フレーズ重み付け — 「surprise rate hike」(0.9)と「resilient」(0.3)を
   同じ1票にしない。材料の強さで差をつける
2. 否定の理解 — 「not hawkish」「rules out rate hike」は極性を反転
3. ヘッジの割引 — 「may」「could」「speculation」を含む見出しは×0.7
4. 強調の増幅 — 「sharply」「unexpectedly」「soars」は×1.3
5. 鮮度減衰 — 古い見出しほど寄与を半減期12時間で減衰
6. 合意度×物量の確信度 — 見出し同士が同方向を向いているか(合意度)と
   証拠の量(物量)から確信度を算出し、実効スコア = バイアス×確信度とする
   (Claude API経路と同じ契約。薄い材料の強い数値が下流に流れない)
7. テーマ抽出と日本語コメント — 金融政策/インフレ/雇用/景気/地政学の
   分類から、通貨ごとの主要テーマと一言コメントを生成
8. レジーム判定のデータ化 — 語彙の雰囲気ではなく、macro.py の
   VIX・金利・ドル指数の実データ規則を優先する(無ければ語彙で代替)

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, UTC
from collections.abc import Mapping, Sequence

from .macro import MacroSnapshot
from .news import KNOWN_CURRENCIES, NewsItem
from .sentiment import CurrencySentiment, MarketAnalysis, pair_move_scores

ENGINE_NAME = "analyst"

RECENCY_HALF_LIFE_HOURS = 12.0  # この時間で見出しの寄与が半分になる
EVIDENCE_SCALE = 2.5  # 重み合計がこの値で|バイアス|が1.0に達する
VOLUME_SHRINK_K = 3.0  # 確信度の物量項 n/(n+K)
HEDGE_DAMPEN = 0.7
NEGATION_FLIP = -0.6  # 否定は単純反転でなく弱めて反転(「not hawkish」≠「dovish」)
INTENSIFIER_BOOST = 1.3
NEGATION_WINDOW_CHARS = 32  # フレーズ直前のこの範囲に否定語があれば反転

# ソース信頼度(未知ソースは0.8)。専門メディアを優先する
SOURCE_WEIGHTS = {"fxstreet": 1.0, "reuters": 1.0, "bloomberg": 1.0}
DEFAULT_SOURCE_WEIGHT = 0.8

# (フレーズ, 重み, テーマ) — タグ付けされた通貨に対して通貨高方向
POSITIVE_PHRASES: tuple[tuple[str, float, str], ...] = (
    ("surprise rate hike", 0.9, "policy"),
    ("emergency rate hike", 0.9, "policy"),
    ("intervention", 0.8, "policy"),
    ("rate hike", 0.6, "policy"),
    ("rate hikes", 0.6, "policy"),
    ("raise rates", 0.6, "policy"),
    ("raises rates", 0.6, "policy"),
    ("higher rates", 0.5, "policy"),
    ("hawkish", 0.6, "policy"),
    ("tightening", 0.5, "policy"),
    ("tighten", 0.5, "policy"),
    ("hot inflation", 0.5, "inflation"),
    ("hotter than expected", 0.5, "inflation"),
    ("sticky inflation", 0.4, "inflation"),
    ("beats expectations", 0.5, "growth"),
    ("beat expectations", 0.5, "growth"),
    ("better than expected", 0.5, "growth"),
    ("above expectations", 0.5, "growth"),
    ("surge in hiring", 0.5, "employment"),
    ("strong jobs", 0.5, "employment"),
    ("jobs growth", 0.4, "employment"),
    ("upbeat", 0.3, "growth"),
    ("resilient", 0.3, "growth"),
    ("bullish", 0.3, "flow"),
    ("strengthens", 0.3, "flow"),
    ("strengthen", 0.3, "flow"),
    ("strong", 0.25, "growth"),
)

# (フレーズ, 重み, テーマ) — タグ付けされた通貨に対して通貨安方向
NEGATIVE_PHRASES: tuple[tuple[str, float, str], ...] = (
    ("emergency rate cut", 0.9, "policy"),
    ("surprise rate cut", 0.9, "policy"),
    ("rate cut", 0.6, "policy"),
    ("rate cuts", 0.6, "policy"),
    ("cut rates", 0.6, "policy"),
    ("cuts rates", 0.6, "policy"),
    ("lower rates", 0.5, "policy"),
    ("dovish", 0.6, "policy"),
    ("easing", 0.5, "policy"),
    ("loosen", 0.5, "policy"),
    ("recession", 0.6, "growth"),
    ("misses expectations", 0.5, "growth"),
    ("miss expectations", 0.5, "growth"),
    ("worse than expected", 0.5, "growth"),
    ("below expectations", 0.5, "growth"),
    ("softer than expected", 0.5, "inflation"),
    ("cooling inflation", 0.4, "inflation"),
    ("disinflation", 0.4, "inflation"),
    ("job losses", 0.5, "employment"),
    ("unemployment rises", 0.5, "employment"),
    ("layoffs", 0.4, "employment"),
    ("slowdown", 0.4, "growth"),
    ("cooling", 0.3, "growth"),
    ("downbeat", 0.3, "growth"),
    ("bearish", 0.3, "flow"),
    ("weakens", 0.3, "flow"),
    ("weaken", 0.3, "flow"),
    ("weak", 0.25, "growth"),
)

NEGATION_TERMS = (
    "not ",
    "no ",
    "won't",
    "will not",
    "unlikely",
    "denies",
    "denied",
    "pushes back",
    "pushed back",
    "rules out",
    "ruled out",
    "refrains from",
    "holds off",
    "delays",
    "delay ",
    "postpones",
)

HEDGE_TERMS = (
    "may ",
    "might ",
    "could ",
    "expected to",
    "likely to",
    "speculation",
    "rumor",
    "rumour",
    "considers",
    "considering",
    "mulls",
    "debate",
)

INTENSIFIER_TERMS = (
    "sharply",
    "surges",
    "surge ",
    "soars",
    "plunges",
    "plunge ",
    "aggressive",
    "unexpectedly",
    "shock",
    "record ",
)

# レジーム判定の語彙フォールバック(マクロ実データが無いときだけ使う)
RISK_OFF_TERMS = (
    "risk-off",
    "risk off",
    "safe haven",
    "safe-haven",
    "flight to quality",
    "sell-off",
    "selloff",
    "escalation",
    "war ",
    "conflict",
    "tariff",
    "sanctions",
    "crisis",
    "contagion",
)
RISK_ON_TERMS = (
    "risk-on",
    "risk on",
    "risk appetite",
    "record high",
    "rally",
    "rallies",
    "relief",
    "optimism",
    "de-escalation",
    "trade deal",
)

THEME_JA = {
    "policy": "金融政策",
    "inflation": "インフレ",
    "employment": "雇用",
    "growth": "景気",
    "flow": "為替フロー",
    "risk": "地政学・リスク",
}

# (テーマ, 方向) → 一言コメントのテンプレート
COMMENT_JA: dict[tuple[str, int], str] = {
    ("policy", 1): "利上げ・タカ派観測が支え",
    ("policy", -1): "利下げ・ハト派観測が重し",
    ("inflation", 1): "インフレ上振れで金利観測が上方向",
    ("inflation", -1): "インフレ鈍化で緩和観測",
    ("employment", 1): "雇用の強さが追い風",
    ("employment", -1): "雇用の弱さが逆風",
    ("growth", 1): "経済指標の上振れが支え",
    ("growth", -1): "景気減速懸念が重し",
    ("flow", 1): "買いフローが優勢",
    ("flow", -1): "売りフローが優勢",
    ("risk", 1): "リスク回避の受け皿として買われやすい",
    ("risk", -1): "リスクイベントが重し",
}


@dataclass
class _CurrencyEvidence:
    """通貨1つぶんの証拠の集計途中経過。"""

    weighted_sum: float = 0.0
    weighted_abs: float = 0.0
    effective_items: float = 0.0  # 鮮度・ソース重み込みの実効見出し数
    headline_count: int = 0
    theme_weights: dict[str, float] = field(default_factory=dict)


def _source_weight(source: str) -> float:
    return SOURCE_WEIGHTS.get(source.strip().lower(), DEFAULT_SOURCE_WEIGHT)


def _recency_weight(published: datetime, now: datetime) -> float:
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    return math.pow(0.5, age_hours / RECENCY_HALF_LIFE_HOURS)


def _phrase_hits(
    lowered: str, phrases: Sequence[tuple[str, float, str]], polarity: float
) -> list[tuple[float, str]]:
    """テキスト中のフレーズを否定検出付きで採点する。

    戻り値は (符号付き寄与, テーマ) の一覧。同じフレーズの複数出現は
    先頭の1回だけ数える(見出しの反復で寄与が膨らむのを防ぐ)。
    """
    hits: list[tuple[float, str]] = []
    for phrase, weight, theme in phrases:
        index = lowered.find(phrase)
        if index < 0:
            continue
        contribution = polarity * weight
        window = lowered[max(0, index - NEGATION_WINDOW_CHARS) : index]
        if any(term in window for term in NEGATION_TERMS):
            contribution *= NEGATION_FLIP
        hits.append((contribution, theme))
    return hits


def _item_modifier(lowered: str) -> float:
    """見出し全体に掛かる修飾係数(ヘッジで割引、強調で増幅)。"""
    modifier = 1.0
    if any(term in lowered for term in HEDGE_TERMS):
        modifier *= HEDGE_DAMPEN
    if any(term in lowered for term in INTENSIFIER_TERMS):
        modifier *= INTENSIFIER_BOOST
    return modifier


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.lower())


def score_headlines(
    items: Sequence[NewsItem],
    currencies: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, CurrencySentiment]:
    """見出し一式から通貨別センチメントを推定する(本エンジンの中核)。

    実効スコア = バイアス × 確信度。確信度は
    合意度(証拠がどれだけ同方向か) × 物量(実効見出し数のシュリンク)。
    """
    now = now or datetime.now(UTC)
    universe = set(currencies) if currencies else set(KNOWN_CURRENCIES)
    evidence = {ccy: _CurrencyEvidence() for ccy in universe}

    for item in items:
        lowered = _normalize(item.text)
        item_weight = _source_weight(item.source) * _recency_weight(item.published, now)
        if item_weight <= 0.01:
            continue
        modifier = _item_modifier(lowered)
        hits = _phrase_hits(lowered, POSITIVE_PHRASES, +1.0)
        hits += _phrase_hits(lowered, NEGATIVE_PHRASES, -1.0)
        move_scores = pair_move_scores(item.text)

        touched = (set(item.currencies) | set(move_scores)) & universe
        for ccy in touched:
            agg = evidence[ccy]
            agg.headline_count += 1
            agg.effective_items += item_weight
            # 語彙フレーズはタグ付けされた通貨に配分
            if ccy in item.currencies:
                for contribution, theme in hits:
                    value = contribution * modifier * item_weight
                    agg.weighted_sum += value
                    agg.weighted_abs += abs(value)
                    agg.theme_weights[theme] = agg.theme_weights.get(theme, 0.0) + abs(value)
            # 「USD/JPY rises」型の構文は方向が明確なので重め(0.7)に加算
            move = move_scores.get(ccy, 0.0)
            if move:
                value = (0.7 if move > 0 else -0.7) * item_weight
                agg.weighted_sum += value
                agg.weighted_abs += abs(value)
                agg.theme_weights["flow"] = agg.theme_weights.get("flow", 0.0) + abs(value)

    result: dict[str, CurrencySentiment] = {}
    for ccy in sorted(universe):
        agg = evidence[ccy]
        sentiment = CurrencySentiment(currency=ccy, headline_count=agg.headline_count)
        if agg.weighted_abs > 0:
            agreement = abs(agg.weighted_sum) / agg.weighted_abs  # 0〜1: 証拠の一枚岩度
            volume = agg.effective_items / (agg.effective_items + VOLUME_SHRINK_K)
            confidence = round(agreement * volume, 3)
            bias = max(-1.0, min(1.0, agg.weighted_sum / EVIDENCE_SCALE))
            sentiment.score = round(bias * confidence, 3)
            sentiment.confidence = confidence
            top_themes = sorted(agg.theme_weights.items(), key=lambda kv: -kv[1])[:3]
            sentiment.themes = [THEME_JA.get(theme, theme) for theme, _ in top_themes]
            if top_themes and abs(sentiment.score) >= 0.05:
                direction = 1 if agg.weighted_sum > 0 else -1
                sentiment.comment = COMMENT_JA.get((top_themes[0][0], direction), "")
            # 集計の生カウント(表示・デバッグ用)
            sentiment.positives = sum(1 for c, _ in _count_signs(agg) if c > 0)
            sentiment.negatives = sum(1 for c, _ in _count_signs(agg) if c < 0)
        result[ccy] = sentiment
    return result


def _count_signs(agg: _CurrencyEvidence) -> list[tuple[float, None]]:
    """positives/negatives表示用の擬似カウント(合計符号ベース)。"""
    if agg.weighted_sum > 0:
        return [(1.0, None)]
    if agg.weighted_sum < 0:
        return [(-1.0, None)]
    return []


def detect_regime_from_headlines(items: Sequence[NewsItem]) -> tuple[str, str]:
    """語彙ベースのレジーム判定(マクロ実データが無いときのフォールバック)。"""
    off_hits = 0
    on_hits = 0
    for item in items:
        lowered = _normalize(item.text)
        off_hits += sum(1 for term in RISK_OFF_TERMS if term in lowered)
        on_hits += sum(1 for term in RISK_ON_TERMS if term in lowered)
    if off_hits >= on_hits + 2:
        return "risk_off", f"リスク回避語彙{off_hits}件 vs 選好{on_hits}件"
    if on_hits >= off_hits + 2:
        return "risk_on", f"リスク選好語彙{on_hits}件 vs 回避{off_hits}件"
    return "neutral", "語彙からは方向感なし"


def _build_summary(
    scores: Mapping[str, CurrencySentiment],
    regime: str,
    regime_note: str,
    item_count: int,
) -> str:
    """市況要約(日本語2〜3行)を組み立てる。"""
    ranked = sorted(
        (s for s in scores.values() if abs(s.score) >= 0.05),
        key=lambda s: -abs(s.score),
    )
    lines: list[str] = []
    if ranked:
        strongest = ranked[0]
        direction_ja = "買われやすい" if strongest.score > 0 else "売られやすい"
        line = f"直近{item_count}本の見出しでは{strongest.currency}が最も{direction_ja}"
        if strongest.comment:
            line += f"({strongest.comment})"
        lines.append(line)
        if len(ranked) >= 2:
            second = ranked[1]
            direction_ja = "強気" if second.score > 0 else "弱気"
            lines.append(f"次点は{second.currency}({direction_ja} {second.score:+.2f})")
    else:
        lines.append(f"直近{item_count}本の見出しに明確な方向感のある材料なし")
    regime_ja = {"risk_on": "リスクオン", "risk_off": "リスクオフ", "neutral": "中立"}[regime]
    lines.append(f"地合いは{regime_ja}({regime_note})")
    return "\n".join(lines)


def analyze_headlines(
    items: Sequence[NewsItem],
    currencies: Sequence[str],
    now: datetime | None = None,
    macro: MacroSnapshot | None = None,
) -> MarketAnalysis:
    """自前エンジンによる市況分析。sentiment.analyze_market から呼ばれる。

    レジームはマクロ実データ(VIX・金利・ドル指数)があればそれを優先し、
    無ければ見出し語彙のフォールバックで判定する。
    """
    now = now or datetime.now(UTC)
    scores = score_headlines(items, currencies, now=now)

    if macro is not None and macro.coverage() > 0:
        regime, regime_note = macro.regime()
        regime_note = f"実データ判定: {regime_note}"
    else:
        regime, regime_note = detect_regime_from_headlines(items)
        regime_note = f"語彙判定: {regime_note}"

    return MarketAnalysis(
        currencies=scores,
        regime=regime,
        summary=_build_summary(scores, regime, regime_note, len(items)),
        engine=ENGINE_NAME,
    )
