"""ニュースヘッドラインから通貨センチメントを推定する。

2段構え:
1. 語彙ベーススコアリング — APIキー不要で常に動作するフォールバック。
   金融政策(ホーク/ダブ)・経済指標(上振れ/下振れ)・値動きの語彙から
   通貨ごとに -1.0〜+1.0 のバイアスを算出する。
2. Claude API(任意) — ANTHROPIC_API_KEY があれば、ヘッドライン一式を
   機関投資家のFXストラテジスト視点で解釈させ、テーマ抽出・地合い判定・
   日本語コメントを得る。失敗時は語彙ベースに自動フォールバック。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence

import requests

from .news import KNOWN_CURRENCIES, NewsItem

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_ATTEMPTS = 2
CLAUDE_RETRY_WAIT_SECONDS = 1.5

# 記事数シュリンク: score×n/(n+K)。記事1件の"weak"だけで±1.0に振れるのを防ぐ
LEXICON_SHRINK_K = 2

# タグ付けされた通貨に対して強気(通貨高)方向の語彙
POSITIVE_TERMS = (
    "hawkish",
    "rate hike",
    "rate hikes",
    "raise rates",
    "raises rates",
    "higher rates",
    "rates higher",
    "head higher",
    "tightening",
    "tighten",
    "strong",
    "strengthens",
    "strengthen",
    "beats",
    "beat expectations",
    "better than expected",
    "above expectations",
    "upbeat",
    "bullish",
    "resilient",
    "hot inflation",
    "hotter than expected",
    "surge in hiring",
)

# タグ付けされた通貨に対して弱気(通貨安)方向の語彙
NEGATIVE_TERMS = (
    "dovish",
    "rate cut",
    "rate cuts",
    "cut rates",
    "cuts rates",
    "lower rates",
    "rates lower",
    "easing",
    "loosen",
    "weak",
    "weakens",
    "weaken",
    "misses",
    "miss expectations",
    "worse than expected",
    "below expectations",
    "downbeat",
    "bearish",
    "recession",
    "slowdown",
    "cooling",
    "softer than expected",
)

# 「PAIRが上昇/下落」の構文: ベース通貨に+、クオート通貨に−(下落なら逆)
_UP_VERBS = (
    "rises",
    "rise",
    "gains",
    "climbs",
    "jumps",
    "rallies",
    "advances",
    "extends gains",
    "hits high",
    "strengthens",
    "recovers",
)
_DOWN_VERBS = (
    "falls",
    "fall",
    "drops",
    "slides",
    "declines",
    "slips",
    "plunges",
    "tumbles",
    "extends losses",
    "hits low",
    "weakens",
    "retreats",
)

_PAIR_MOVE_RE = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b|\b([A-Z]{6})\b")


@dataclass
class CurrencySentiment:
    currency: str
    score: float = 0.0  # -1.0(弱気)〜+1.0(強気)。確信度で減衰済みの実効値
    positives: int = 0
    negatives: int = 0
    headline_count: int = 0
    themes: list[str] = field(default_factory=list)
    comment: str = ""
    confidence: float | None = None  # Claude分析時のみ(0.0〜1.0)

    @property
    def label_ja(self) -> str:
        if self.score >= 0.4:
            return "強気"
        if self.score >= 0.15:
            return "やや強気"
        if self.score <= -0.4:
            return "弱気"
        if self.score <= -0.15:
            return "やや弱気"
        return "中立"


@dataclass
class MarketAnalysis:
    """通貨別センチメントと市場全体の地合い。"""

    currencies: dict[str, CurrencySentiment]
    regime: str = "neutral"  # risk_on / risk_off / neutral
    summary: str = ""
    engine: str = "lexicon"  # lexicon / claude

    @property
    def regime_ja(self) -> str:
        return {
            "risk_on": "リスクオン",
            "risk_off": "リスクオフ",
            "neutral": "中立",
        }.get(self.regime, self.regime)


def _extract_pair(match: re.Match) -> tuple[str, str] | None:
    if match.group(3):
        base, quote = match.group(3)[:3], match.group(3)[3:]
    else:
        base, quote = match.group(1), match.group(2)
    if base in KNOWN_CURRENCIES and quote in KNOWN_CURRENCIES:
        return base, quote
    return None


def _pair_move_scores(text: str) -> dict[str, float]:
    """「USD/JPY rises」型の見出しから通貨別の方向を読む。"""
    scores: dict[str, float] = {}
    upper = text.upper()
    lowered = text.lower()
    for match in _PAIR_MOVE_RE.finditer(upper):
        pair = _extract_pair(match)
        if pair is None:
            continue
        base, quote = pair
        tail = lowered[match.end() : match.end() + 60]
        direction = 0
        if any(verb in tail for verb in _UP_VERBS):
            direction = 1
        elif any(verb in tail for verb in _DOWN_VERBS):
            direction = -1
        if direction:
            scores[base] = scores.get(base, 0.0) + direction
            scores[quote] = scores.get(quote, 0.0) - direction
    return scores


def score_headlines_lexicon(
    items: Iterable[NewsItem], currencies: Sequence[str] | None = None
) -> dict[str, CurrencySentiment]:
    """語彙ベースで通貨ごとのセンチメントを集計する。"""
    universe = set(currencies) if currencies else set(KNOWN_CURRENCIES)
    result = {ccy: CurrencySentiment(currency=ccy) for ccy in sorted(universe)}

    for item in items:
        lowered = item.text.lower()
        pos_hits = sum(1 for term in POSITIVE_TERMS if term in lowered)
        neg_hits = sum(1 for term in NEGATIVE_TERMS if term in lowered)
        move_scores = _pair_move_scores(item.text)

        touched = set(item.currencies) | set(move_scores)
        for ccy in touched & universe:
            sentiment = result[ccy]
            sentiment.headline_count += 1
            # 語彙ヒットはタグ付けされた通貨に均等に配分
            if ccy in item.currencies:
                sentiment.positives += pos_hits
                sentiment.negatives += neg_hits
            # ペアの値動き構文は方向が明確なので重み2で加算
            move = move_scores.get(ccy, 0.0)
            if move > 0:
                sentiment.positives += 2
            elif move < 0:
                sentiment.negatives += 2

    for sentiment in result.values():
        total = sentiment.positives + sentiment.negatives
        if total > 0:
            raw = (sentiment.positives - sentiment.negatives) / total
            # 根拠となる記事が少ないほど0へ収縮させ、少数記事の過大評価を防ぐ
            count = max(sentiment.headline_count, 1)
            shrink = count / (count + LEXICON_SHRINK_K)
            sentiment.score = round(raw * shrink, 3)
    return result


def load_api_key(project_root: str | Path | None = None) -> str | None:
    """ANTHROPIC_API_KEY を環境変数か .env から読む。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    env_path = Path(project_root) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _extract_json_block(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _clip_float(value: object, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def build_analysis_from_claude_json(
    parsed: Mapping | None, universe: Sequence[str]
) -> MarketAnalysis | None:
    """Claudeが返したJSONを検証してMarketAnalysisへ変換する。

    実効スコア = bias × confidence。確信の薄い判断ほど0へ減衰させ、
    「材料が薄いのに強い数値」がそのまま複合スコアへ流れるのを防ぐ。
    """
    if not parsed or not isinstance(parsed.get("currencies"), Mapping):
        return None
    result: dict[str, CurrencySentiment] = {}
    for ccy in universe:
        info = parsed["currencies"].get(ccy) or {}
        if not isinstance(info, Mapping):
            info = {}
        bias = _clip_float(info.get("bias", 0.0), -1.0, 1.0, default=0.0)
        # confidence欠落時は0.5(半信半疑)として保守的に扱う
        confidence = _clip_float(info.get("confidence", 0.5), 0.0, 1.0, default=0.5)
        result[ccy] = CurrencySentiment(
            currency=ccy,
            score=round(bias * confidence, 3),
            themes=[str(t) for t in (info.get("themes") or [])][:3],
            comment=str(info.get("comment", ""))[:80],
            confidence=round(confidence, 3),
        )
    regime = str(parsed.get("market_regime", "neutral")).strip().lower()
    if regime not in ("risk_on", "risk_off", "neutral"):
        regime = "neutral"
    return MarketAnalysis(
        currencies=result,
        regime=regime,
        summary=str(parsed.get("summary", ""))[:400],
        engine="claude",
    )


def analyze_with_claude(
    items: Sequence[NewsItem],
    currencies: Sequence[str],
    api_key: str,
    model: str | None = None,
    timeout: float = 90.0,
    session: requests.Session | None = None,
) -> MarketAnalysis | None:
    """Claude APIでヘッドライン一式をマクロ分析する。失敗時はNone。"""
    if not items:
        return None
    model = model or os.environ.get("FX_INTEL_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    universe = sorted(set(currencies))
    headline_lines = [
        f"- [{item.published.strftime('%m-%d %H:%M')}Z/{item.source}] {item.title}"
        for item in items[:50]
    ]
    schema_example = {
        "currencies": {
            ccy: {
                "bias": 0.0,
                "confidence": 0.0,
                "themes": ["テーマ"],
                "comment": "一言コメント",
            }
            for ccy in universe
        },
        "market_regime": "risk_on | risk_off | neutral",
        "summary": "市場全体の3行以内の要約",
    }
    prompt = (
        "あなたは機関投資家のFXデスクのシニアストラテジストです。"
        "以下の直近ニュースヘッドラインを読み、各通貨の短期(1〜3営業日)バイアスを判定してください。\n"
        "- bias: -1.0(明確な通貨安要因)〜+1.0(明確な通貨高要因)\n"
        "- confidence: 0.0〜1.0(材料の量と一貫性)\n"
        "- themes: その通貨を動かしている主要テーマ(最大3件、日本語)\n"
        "- comment: 日本語の一言コメント(40字以内)\n"
        "材料がない通貨は bias=0, confidence=0 とすること。"
        "推測で強い数値を出さないこと。\n\n"
        f"対象通貨: {', '.join(universe)}\n\n"
        "ヘッドライン:\n" + "\n".join(headline_lines) + "\n\n"
        "次のJSONだけを出力してください(前後に文章を付けない):\n"
        + json.dumps(schema_example, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    http = session or requests
    body = None
    for attempt in range(CLAUDE_ATTEMPTS):
        try:
            response = http.post(ANTHROPIC_API_URL, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            break
        except Exception:  # noqa: BLE001 - API失敗は再試行→語彙ベースへフォールバック
            if attempt + 1 < CLAUDE_ATTEMPTS:
                time.sleep(CLAUDE_RETRY_WAIT_SECONDS)
    if body is None:
        return None
    text = "".join(
        block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"
    )
    return build_analysis_from_claude_json(_extract_json_block(text), universe)


def analyze_market(
    items: Sequence[NewsItem],
    currencies: Sequence[str],
    use_llm: bool = True,
    api_key: str | None = None,
    model: str | None = None,
) -> MarketAnalysis:
    """LLM分析を試み、使えなければ語彙ベースで返す。"""
    if use_llm:
        key = api_key or load_api_key()
        if key:
            analysis = analyze_with_claude(items, currencies, key, model=model)
            if analysis is not None:
                # ヘッドライン件数は語彙ベース集計から補完する
                counts = score_headlines_lexicon(items, currencies)
                for ccy, sentiment in analysis.currencies.items():
                    if ccy in counts:
                        sentiment.headline_count = counts[ccy].headline_count
                return analysis
    return MarketAnalysis(
        currencies=score_headlines_lexicon(items, currencies),
        engine="lexicon",
    )


def pair_bias(base: str, quote: str, currencies: Mapping[str, CurrencySentiment]) -> float:
    """ペアの方向バイアス = (ベース通貨スコア − クオート通貨スコア) / 2。"""
    base_score = currencies.get(base, CurrencySentiment(base)).score
    quote_score = currencies.get(quote, CurrencySentiment(quote)).score
    return round(max(-1.0, min(1.0, (base_score - quote_score) / 2.0)), 3)
