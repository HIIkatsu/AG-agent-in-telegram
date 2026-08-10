import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'antigravity-bot'))

from bot.utils.telegram_renderer import chunk_text, part_label, render_markdown, strip_telegram_html


def test_render_markdown_escapes_html_and_formats_code():
    rendered = render_markdown('**bold** <x> `code`')
    assert '<b>bold</b>' in rendered.html
    assert '&lt;x&gt;' in rendered.html
    assert '<code>code</code>' in rendered.html


def test_chunk_text_splits_long_unbroken_lines():
    chunks = chunk_text('a' * 9001, max_len=4000)
    assert [len(c) for c in chunks] == [4000, 4000, 1001]


def test_plain_fallback_removes_telegram_tags():
    assert strip_telegram_html('<b>Hello</b> <code>world</code>') == 'Hello world'


def test_part_label_is_only_needed_for_multi_part_messages():
    assert part_label(1, 1) == ''
    assert 'Часть 2/3' in part_label(2, 3)
