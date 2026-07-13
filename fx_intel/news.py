"""FXニュースヘッドラインの収集と通貨タグ付け。

FXStreetのRSSとGoogle NewsのRSS(通貨ペア検索)からヘッドラインを
集め、本文中のキーワードから関連通貨をタグ付けする。
外部依存はrequestsのみで、RSSは標準ライブラリで解析する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from email.utils import parsedate_to_datetime
from collections.abc import Iterable, Sequence
from urllib.parse import quote
from xml.etree import ElementTree

import requests

FXSTREET_RSS_URL = "https://www.fxstreet.com/rss/news"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

USER_AGENT = "fx-codex-intel/1.0 (+https://github.com/fuuki1)"

KNOWN_CURRENCIES = {"USD", "JPY", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF"}

# 通貨ごとの関連キーワード(小文字で照合)
CURRENCY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "USD": (
        r"\busd\b",
        "dollar",
        "greenback",
        r"\bfed\b",
        "fomc",
        "powell",
        "nonfarm",
        "payrolls",
        r"u\.s\.",
        "united states",
        "treasur",
    ),
    "JPY": (
        r"\bjpy\b",
        r"\byen\b",
        r"\bboj\b",
        "bank of japan",
        "ueda",
        "japan",
        "tankan",
        "tokyo cpi",
    ),
    "EUR": (
        r"\beur\b",
        r"\beuro\b",
        r"\becb\b",
        "lagarde",
        "eurozone",
        "euro zone",
        "germany",
        "german",
        r"\bbund\b",
    ),
    "GBP": (
        r"\bgbp\b",
        "pound",
        "sterling",
        r"\bboe\b",
        "bank of england",
        r"\buk\b",
        "britain",
        "british",
        r"\bcable\b",
        "gilt",
    ),
    "AUD": (r"\baud\b", "aussie", r"\brba\b", "australia"),
    "NZD": (r"\bnzd\b", r"\bkiwi\b", "rbnz", "new zealand"),
    "CAD": (r"\bcad\b", "loonie", r"\bboc\b", "bank of canada", "canada"),
    "CHF": (r"\bchf\b", "franc", r"\bsnb\b", "swiss"),
}

_CURRENCY_PATTERNS = {
    currency: [re.compile(pattern) for pattern in patterns]
    for currency, patterns in CURRENCY_KEYWORDS.items()
}

_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b|\b([A-Z]{6})\b")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    published: datetime  # UTC
    summary: str = ""
    currencies: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return f"{self.title} {self.summary}"


def tag_currencies(text: str) -> tuple[str, ...]:
    """テキストから関連通貨を推定する(登場順は固定順)。"""
    lowered = text.lower()
    tagged: set[str] = set()
    for currency, patterns in _CURRENCY_PATTERNS.items():
        if any(pattern.search(lowered) for pattern in patterns):
            tagged.add(currency)
    for match in _PAIR_RE.finditer(text.upper()):
        if match.group(3):
            base, quote_ccy = match.group(3)[:3], match.group(3)[3:]
        else:
            base, quote_ccy = match.group(1), match.group(2)
        if base in KNOWN_CURRENCIES and quote_ccy in KNOWN_CURRENCIES:
            tagged.add(base)
            tagged.add(quote_ccy)
    return tuple(sorted(tagged))


def _parse_pubdate(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    # RSS dates without an offset are ambiguous around DST and cannot prove when
    # the article was available.  Do not guess UTC at the source boundary.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _clean_html(text: str) -> str:
    return _TAG_RE.sub(" ", text).replace("&amp;", "&").strip()


def parse_rss(xml_text: str, source: str) -> list[NewsItem]:
    """RSS 2.0のitem一覧をNewsItemに変換する。"""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items: list[NewsItem] = []
    for node in root.iter("item"):
        title = _clean_html(node.findtext("title", default="").strip())
        if not title:
            continue
        published = _parse_pubdate(node.findtext("pubDate", default=""))
        if published is None:
            continue
        summary = _clean_html(node.findtext("description", default=""))[:300]
        text = f"{title} {summary}"
        items.append(
            NewsItem(
                title=title,
                source=source,
                link=node.findtext("link", default="").strip(),
                published=published,
                summary=summary,
                currencies=tag_currencies(text),
            )
        )
    return items


def _fetch_rss(
    url: str, source: str, timeout: float, session: requests.Session | None
) -> list[NewsItem]:
    http = session or requests
    response = http.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return parse_rss(response.text, source)


def fetch_fxstreet(
    timeout: float = 15.0, session: requests.Session | None = None
) -> list[NewsItem]:
    return _fetch_rss(FXSTREET_RSS_URL, "FXStreet", timeout, session)


def fetch_google_news(
    query: str, timeout: float = 15.0, session: requests.Session | None = None
) -> list[NewsItem]:
    url = GOOGLE_NEWS_RSS_URL.format(query=quote(query))
    return _fetch_rss(url, "GoogleNews", timeout, session)


def dedupe_and_sort(items: Iterable[NewsItem]) -> list[NewsItem]:
    """タイトルの正規化キーで重複排除し新しい順に並べる。"""
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in sorted(items, key=lambda i: i.published, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", item.title.lower())[:80]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def fetch_news_for_symbols(
    symbols: Sequence[str],
    hours_back: float = 24.0,
    max_items: int = 60,
    timeout: float = 15.0,
    session: requests.Session | None = None,
    *,
    as_of: datetime | None = None,
    max_future_skew_seconds: float = 0.0,
) -> tuple[list[NewsItem], list[str]]:
    """対象ペアに関連するニュースを収集する。

    戻り値は (ニュース一覧, 取得失敗ソースの警告一覧)。
    一部ソースが落ちていても残りで分析を継続する。
    """
    if hours_back < 0 or max_items < 1 or max_future_skew_seconds < 0:
        raise ValueError("news freshness thresholds are invalid")
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("news as_of must be timezone-aware")

    collected: list[NewsItem] = []
    warnings: list[str] = []

    try:
        collected.extend(fetch_fxstreet(timeout=timeout, session=session))
    except Exception as error:  # noqa: BLE001 - 外部フィード起因
        warnings.append(f"FXStreet RSS取得失敗: {error}")

    queried: set[str] = set()
    for symbol in symbols:
        cleaned = symbol.upper().replace("/", "")
        if len(cleaned) != 6 or cleaned in queried:
            continue
        queried.add(cleaned)
        query = f'"{cleaned[:3]}/{cleaned[3:]}" OR "{cleaned}"'
        try:
            collected.extend(fetch_google_news(query, timeout=timeout, session=session))
        except Exception as error:  # noqa: BLE001 - 外部フィード起因
            warnings.append(f"Google News({cleaned})取得失敗: {error}")

    # Resolve the cutoff only after every network request has completed.  An
    # article acquired during the run may legitimately be newer than the run's
    # start time, while a source-dated article beyond this boundary is future
    # information and must not enter the decision features.
    observed_at = (as_of or datetime.now(UTC)).astimezone(UTC)
    cutoff = observed_at - timedelta(hours=hours_back)
    future_limit = observed_at + timedelta(seconds=max_future_skew_seconds)
    ordered = dedupe_and_sort(collected)
    future_count = sum(item.published > future_limit for item in ordered)
    if future_count:
        warnings.append(f"未来時刻のニュースを隔離: {future_count}件")
    fresh = [item for item in ordered if cutoff <= item.published <= future_limit]
    return fresh[:max_items], warnings
