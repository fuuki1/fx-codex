"""Discord delivery helpers for long-form text reports."""

from __future__ import annotations

from collections.abc import Sequence

DISCORD_CONTENT_LIMIT = 2000
DEFAULT_CHUNK_LIMIT = 1900


def split_markdown_for_discord(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    """Split Markdown into Discord-safe content chunks.

    The splitter prefers section and paragraph boundaries.  If a single
    paragraph is still too long, it falls back to hard slicing.
    """
    if limit <= 0 or limit > DISCORD_CONTENT_LIMIT:
        raise ValueError("limit must be between 1 and Discord's content limit")
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    blocks = text.split("\n\n")
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        for start in range(0, len(block), limit):
            part = block[start : start + limit].strip()
            if part:
                chunks.append(part)
    if current:
        chunks.append(current)
    return chunks


def build_discord_text_payloads(
    text: str,
    *,
    username: str = "fx-codex 売買分析通知",
    limit: int = DEFAULT_CHUNK_LIMIT,
) -> list[dict]:
    """Build Discord webhook payloads for a long text report."""
    chunks = split_markdown_for_discord(text, limit=limit)
    total = len(chunks)
    payloads = []
    for index, chunk in enumerate(chunks, start=1):
        suffix = f"\n\n({index}/{total})" if total > 1 else ""
        content = chunk
        if len(content) + len(suffix) > DISCORD_CONTENT_LIMIT:
            content = content[: DISCORD_CONTENT_LIMIT - len(suffix)]
        payloads.append(
            {
                "username": username,
                "content": content + suffix,
                "allowed_mentions": {"parse": []},
            }
        )
    return payloads


def payload_contents(payloads: Sequence[dict]) -> list[str]:
    """Return text contents for tests and dry-run diagnostics."""
    return [str(payload.get("content", "")) for payload in payloads]
