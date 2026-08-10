"""Single Telegram rendering pipeline: Markdown-ish text -> safe Telegram HTML.

The bot receives mixed agent output (plain text, Markdown and occasional raw HTML).
This module centralizes escaping, lightweight markdown conversion, chunking and
plain-text fallback so handlers do not maintain competing renderers.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


MAX_TELEGRAM_TEXT = 4000


@dataclass(frozen=True)
class RenderedText:
    html: str
    plain: str


def render_markdown(text: str) -> RenderedText:
    """Render user/agent text to Telegram-safe HTML plus a plain fallback."""
    if not text:
        return RenderedText("", "")

    text = _strip_internal_paths(text.replace("\ufffd", ""))
    parts = re.split(r"(```[\s\S]*?```)", text)
    rendered: list[str] = []
    plain_parts: list[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            rendered.append(_render_fenced_code(part))
            plain_parts.append(part)
        else:
            rendered.append(_render_markdown_text(part))
            plain_parts.append(part)

    html_text = re.sub(r"\n{3,}", "\n\n", "".join(rendered)).strip()
    plain_text = html.unescape(strip_telegram_html(html_text)).strip()
    return RenderedText(html_text, plain_text)


def chunk_text(text: str, max_len: int = MAX_TELEGRAM_TEXT) -> list[str]:
    """Split text into Telegram-sized chunks without losing long lines."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        current = line
    if current:
        chunks.append(current)
    return chunks or [text[:max_len]]


def part_label(index: int, total: int) -> str:
    return f"<i>(Часть {index}/{total})</i>\n\n" if total > 1 else ""


def strip_telegram_html(text: str) -> str:
    """Fallback for Telegram parse failures: remove tags safely and preserve text."""
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre|blockquote)(?:\s+[^>]*)?>", "", text)
    text = re.sub(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _strip_internal_paths(text: str) -> str:
    text = re.sub(r"\[.*?\]\(file://[^\)]+\)", "", text)
    text = re.sub(r"/tmp/workspaces/\d+/[a-f0-9-]+/?", "", text)
    text = re.sub(r"/root/\.gemini/[^\s\n`\)]+", "", text)
    return text


def _render_fenced_code(block: str) -> str:
    inner = block[3:-3]
    nl = inner.find("\n")
    if nl >= 0:
        lang = inner[:nl].strip()
        code = inner[nl + 1 :]
    else:
        lang = ""
        code = inner
    cls = f' class="language-{html.escape(lang)}"' if lang else ""
    return f"\n<pre><code{cls}>{html.escape(code.rstrip())}</code></pre>\n"


def _render_markdown_text(text: str) -> str:
    lines = []
    for raw in text.split("\n"):
        line = html.escape(raw.rstrip())
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            lines.append(f"<b>{_render_inline(m.group(2))}</b>")
            continue
        m = re.match(r"^&gt;\s*(.*)$", stripped)
        if m:
            lines.append(f"<blockquote>{_render_inline(m.group(1))}</blockquote>")
            continue
        m = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if m:
            lines.append(f"• {_render_inline(m.group(1))}")
            continue
        m = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if m:
            lines.append(f"• {_render_inline(m.group(1))}")
            continue
        lines.append(_render_inline(line))
    return "\n".join(lines)


def _render_inline(text: str) -> str:
    code_blocks: list[str] = []

    def save_code(match: re.Match) -> str:
        code_blocks.append(f"<code>{html.escape(html.unescape(match.group(1)))}</code>")
        return f"\x00CODE{len(code_blocks)-1}\x00"

    text = re.sub(r"`([^`\n]+)`", save_code, text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    for idx, code in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{idx}\x00", code)
    return text
