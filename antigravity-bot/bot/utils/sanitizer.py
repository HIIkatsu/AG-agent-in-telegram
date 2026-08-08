"""Text Sanitizer & Incremental Stream Decoder for Telegram HTML."""

from __future__ import annotations

import codecs
import html
import re


class IncrementalStreamDecoder:
    """Incremental UTF-8 byte stream decoder to prevent \\ufffd artifacts across chunk boundaries."""

    def __init__(self, encoding: str = "utf-8", errors: str = "ignore") -> None:
        self.decoder = codecs.getincrementaldecoder(encoding)(errors=errors)

    def decode(self, chunk: bytes, final: bool = False) -> str:
        return self.decoder.decode(chunk, final=final)


def clean_telegram_markdown(text: str) -> str:
    """Convert Markdown text to valid Telegram HTML format."""
    if not text:
        return ""

    # 0. Clean any replacement character artifacts (\ufffd / diamond symbols)
    text = text.replace("\ufffd", "").replace("\uFFFD", "")

    # 1. Strictly strip raw file:// markdown link artifacts and technical paths
    text = re.sub(r"\[.*?\]\(file://[^\)]+\)", "", text)
    text = re.sub(r"/tmp/workspaces/\d+/[a-f0-9-]+/?", "", text)
    text = re.sub(r"/root/\.gemini/[^\s\n\`\)]+", "", text)

    # 2. Convert LaTeX symbols
    text = text.replace(r"\times", "×")
    text = re.sub(r"\^\\circ|\\circ|\\degree", "°", text)
    text = text.replace(r"\approx", "≈")
    text = text.replace(r"\le", "≤").replace(r"\leq", "≤")
    text = text.replace(r"\ge", "≥").replace(r"\geq", "≥")

    # 3. Extract and protect code blocks & inline code before HTML escaping
    code_blocks: list[str] = []

    def save_fenced_code(m: re.Match) -> str:
        lang = m.group(1).strip()
        code_content = m.group(2)
        escaped = html.escape(code_content.strip())
        lang_attr = f' class="language-{lang}"' if lang else ""
        placeholder = f"___CODE_BLOCK_{len(code_blocks)}___"
        code_blocks.append(f"<pre><code{lang_attr}>{escaped}</code></pre>")
        return placeholder

    # Fenced code blocks ```lang\ncode```
    text = re.sub(r"```(\w*)\n?(.*?)```", save_fenced_code, text, flags=re.DOTALL)

    def save_inline_code(m: re.Match) -> str:
        code_content = m.group(1)
        escaped = html.escape(code_content)
        placeholder = f"___CODE_BLOCK_{len(code_blocks)}___"
        code_blocks.append(f"<code>{escaped}</code>")
        return placeholder

    # Inline code `code`
    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 4. Escape HTML entities for the remaining plain text
    text = html.escape(text)

    # 5. Convert Markdown formatting to HTML
    # Headers: ### Header -> <b>Header</b>
    text = re.sub(r"^#{1,6}\s+(.*)$", r"<b>\1</b>\n", text, flags=re.MULTILINE)

    # Bold: **bold** -> <b>bold</b>
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)

    # Italic: *italic* -> <i>italic</i>
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)

    # Bullet lists: * item or - item -> • item
    text = re.sub(r"^\s*[\*\-]\s+(.*)$", r"• \1", text, flags=re.MULTILINE)

    # 6. Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"___CODE_BLOCK_{i}___", block)

    # 7. Collapse excess blank lines and strip trailing whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
