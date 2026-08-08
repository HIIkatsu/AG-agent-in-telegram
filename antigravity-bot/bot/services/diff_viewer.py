"""VS Code Style Autonomous HTML Diff Generator using diff2html CDN."""

from __future__ import annotations

import html
import json
import os
import tempfile

_DIFF2HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VS Code Diff Viewer — Antigravity AI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/diff2html/bundles/css/diff2html.min.css" />
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/github-dark.min.css" />
  
  <style>
    :root {
      --bg-primary: #1e1e1e;
      --bg-secondary: #252526;
      --text-main: #d4d4d4;
      --accent: #007acc;
      --border-color: #333333;
    }
    body {
      background-color: var(--bg-primary);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      margin: 0;
      padding: 20px;
    }
    header {
      background: var(--bg-secondary);
      padding: 14px 20px;
      border-radius: 8px;
      border: 1px solid var(--border-color);
      margin-bottom: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    h1 {
      font-size: 16px;
      margin: 0;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .badge {
      background: var(--accent);
      color: #fff;
      font-size: 12px;
      padding: 3px 8px;
      border-radius: 4px;
      font-weight: 500;
    }
    #diff-ui {
      background: var(--bg-secondary);
      border-radius: 8px;
      border: 1px solid var(--border-color);
      padding: 12px;
    }

    /* --- VS Code Dark Theme Overrides for diff2html --- */
    .d2h-wrapper {
      background-color: var(--bg-primary) !important;
      color: var(--text-main) !important;
    }
    .d2h-file-wrapper {
      border: 1px solid var(--border-color) !important;
      border-radius: 6px !important;
      margin-bottom: 16px !important;
      background-color: var(--bg-primary) !important;
    }
    .d2h-file-header {
      background-color: #252526 !important;
      border-bottom: 1px solid var(--border-color) !important;
      padding: 8px 12px !important;
    }
    .d2h-file-name {
      color: #ffffff !important;
      font-size: 13px !important;
      font-weight: 600 !important;
    }
    
    /* Table and Cell Backgrounds */
    .d2h-diff-table, .d2h-diff-table tr, .d2h-diff-table td, .d2h-diff-table th {
      background-color: #1e1e1e !important;
      border-color: #2d2d2d !important;
    }
    .d2h-code-side-emptyplaceholder, .d2h-emptyplaceholder, .d2h-code-side-line {
      background-color: #1e1e1e !important;
      background: #1e1e1e !important;
      border-color: #2d2d2d !important;
    }

    /* Line Numbers — White on Dark */
    .d2h-code-linenumber,
    .d2h-code-side-linenumber,
    .d2h-code-linenumber *,
    .d2h-code-side-linenumber *,
    td.d2h-code-linenumber,
    td.d2h-code-side-linenumber {
      background-color: #252526 !important;
      background: #252526 !important;
      color: #ffffff !important;
      border-color: #333333 !important;
      font-family: 'Consolas', 'Cascadia Code', monospace !important;
      font-size: 12px !important;
      font-weight: 500 !important;
      text-align: right !important;
      padding-right: 8px !important;
      opacity: 1 !important;
    }

    /* CHANGED / ADDED / DELETED badges */
    .d2h-tag {
      color: #000000 !important;
      font-weight: 600 !important;
    }

    .d2h-info {
      background-color: #252526 !important;
      color: #8b949e !important;
      border-color: #333333 !important;
      font-family: 'Consolas', monospace !important;
      font-size: 12px !important;
    }

    /* Code Text */
    .d2h-code-line, .d2h-code-line-ctnt, code, pre, .hljs {
      background: transparent !important;
      background-color: transparent !important;
      color: #d4d4d4 !important;
      font-family: 'Consolas', 'Cascadia Code', 'Fira Code', monospace !important;
      font-size: 13px !important;
      line-height: 20px !important;
    }

    .d2h-code-line-prefix {
      background: transparent !important;
      font-weight: bold !important;
      padding: 0 4px !important;
    }

    /* Additions (+) */
    .d2h-ins, td.d2h-ins {
      background-color: rgba(46, 160, 67, 0.22) !important;
      border-color: rgba(46, 160, 67, 0.4) !important;
    }
    .d2h-ins .d2h-code-line-ctnt, .d2h-ins .d2h-code-line-prefix {
      color: #7ee787 !important;
    }
    .d2h-ins ins {
      background-color: rgba(46, 160, 67, 0.45) !important;
      color: #ffffff !important;
      text-decoration: none !important;
      border-radius: 3px !important;
      padding: 1px 3px !important;
    }

    /* Deletions (-) */
    .d2h-del, td.d2h-del {
      background-color: rgba(248, 81, 73, 0.22) !important;
      border-color: rgba(248, 81, 73, 0.4) !important;
    }
    .d2h-del .d2h-code-line-ctnt, .d2h-del .d2h-code-line-prefix {
      color: #ff7b72 !important;
    }
    .d2h-del del {
      background-color: rgba(248, 81, 73, 0.45) !important;
      color: #ffffff !important;
      text-decoration: none !important;
      border-radius: 3px !important;
      padding: 1px 3px !important;
    }
  </style>
</head>
<body>
  <header>
    <h1><span>📝 VS Code Code Diff</span> <span class="badge">$CHAT_TITLE$</span></h1>
    <div style="font-size: 13px; color: #888;">Antigravity AI Agent Workflow</div>
  </header>

  <div id="diff-ui"></div>

  <!-- Safe JSON data container: type=application/json is never executed by the browser -->
  <script type="application/json" id="diff-data">$DIFF_JSON$</script>

  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/diff2html/bundles/js/diff2html-ui.min.js"></script>

  <script>
    document.addEventListener('DOMContentLoaded', function () {
      var rawDiff = JSON.parse(document.getElementById('diff-data').textContent);
      var targetElement = document.getElementById('diff-ui');
      var configuration = {
        drawFileList: true,
        matching: 'lines',
        outputFormat: 'side-by-side',
        synchronisedScroll: true,
        highlight: true,
        renderNothingWhenEmpty: false,
      };
      var diff2htmlUi = new Diff2HtmlUI(targetElement, rawDiff, configuration);
      diff2htmlUi.draw();
      diff2htmlUi.highlightCode();
    });
  </script>
</body>
</html>
"""


def _safe_json_for_html(s: str) -> str:
    """Escape a JSON string so it's safe inside a <script type=application/json> tag.
    
    The only dangerous sequence is '</' which could form '</script>' and
    prematurely close the tag. We escape '<' as '\\u003c'.
    """
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def generate_diff_html_file(raw_diff: str, chat_title: str = "Сессия ИИ") -> str:
    """Generate a self-contained diff.html file using diff2html CDN side-by-side viewer."""
    diff_json = json.dumps(raw_diff)
    safe_json = _safe_json_for_html(diff_json)

    html_content = (
        _DIFF2HTML_TEMPLATE
        .replace("$CHAT_TITLE$", html.escape(chat_title))
        .replace("$DIFF_JSON$", safe_json)
    )

    tmp_dir = tempfile.gettempdir()
    output_path = os.path.join(tmp_dir, "diff.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path
