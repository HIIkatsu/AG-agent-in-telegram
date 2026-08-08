"""Markdown to Telegram HTML converter.

Uses only universally-supported characters. No box-drawing or exotic Unicode.
"""

from __future__ import annotations

import html
import re


def md_to_html(text: str) -> str:
    """Convert markdown to Telegram-safe HTML."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    result: list[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            result.append(_code_block(part))
        else:
            result.append(_convert_text(part))
    output = re.sub(r"\n{3,}", "\n\n", "".join(result))
    return output.strip()


def _code_block(block: str) -> str:
    inner = block[3:-3]
    nl = inner.find("\n")
    if nl != -1:
        lang = inner[:nl].strip()
        code = inner[nl + 1:]
    else:
        lang, code = "", inner
    escaped = html.escape(code.rstrip())
    if lang:
        return f'\n<pre><code class="language-{html.escape(lang)}">{escaped}</code></pre>\n'
    return f"\n<pre><code>{escaped}</code></pre>\n"


def _convert_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        out.append(_line(line))
    return "\n".join(out)


def _line(line: str) -> str:
    s = line.strip()
    if not s:
        return ""

    # Horizontal rule
    if re.match(r"^[-*_]{3,}\s*$", s):
        return ""

    # Headers
    m = re.match(r"^(#{1,6})\s+(.+)$", s)
    if m:
        content = _inline(html.escape(m.group(2)))
        return f"\n<b>{content}</b>\n"

    # Unordered list
    m = re.match(r"^(\s*)[-*+]\s+(.+)$", s)
    if m:
        indent = "  " if m.group(1) else ""
        return f"{indent}  {_inline(html.escape(m.group(2)))}"

    # Ordered list
    m = re.match(r"^(\s*)\d+[.)]\s+(.+)$", s)
    if m:
        return f"  {_inline(html.escape(m.group(2)))}"

    # Blockquote
    m = re.match(r"^>\s*(.*)$", s)
    if m:
        return f"<blockquote>{_inline(html.escape(m.group(1)))}</blockquote>"

    return _inline(html.escape(s))


def _inline(text: str) -> str:
    """Bold, italic, code, links, strikethrough."""
    # Protect inline code
    codes: list[str] = []

    def _save(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00C{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _save, text)

    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Bold+Italic
    text = re.sub(r"\*{3}(.+?)\*{3}", r"<b><i>\1</i></b>", text)
    # Bold
    text = re.sub(r"\*{2}(.+?)\*{2}", r"<b>\1</b>", text)
    # Italic
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Restore code
    for i, c in enumerate(codes):
        text = text.replace(f"\x00C{i}\x00", c)

    return text


def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > max_len:
            if cur:
                chunks.append(cur)
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [text[:max_len]]
