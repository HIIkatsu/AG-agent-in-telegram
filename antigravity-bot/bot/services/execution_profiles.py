"""Pure helpers for selecting and preparing chat/code execution profiles."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal

ExecutionProfile = Literal["chat", "code"]

# Conversation continuity itself is retained by ``agy --continue``. This budget is
# for supplemental project memory: large enough for a useful set of recent/relevant
# notes, but still independent from heavyweight pinned file context.
CHAT_MEMORY_CHAR_BUDGET = 2_000
CODE_MEMORY_CHAR_BUDGET = 8_000

_CODE_INTENT_RE = re.compile(
    r"\b(?:"
    r"файл(?:а|е|ов|ы)?|папк(?:а|е|у|и)|репозитор(?:ий|ия|ии)|проект(?:а|е|ом)?|"
    r"создай|создать|измени|изменить|исправь|исправить|удали|добавь|реализуй|"
    r"запусти|запуск|тест(?:ы|ов)?|проверь\s+проект|diff|commit|коммит|deploy|деплой|"
    r"код(?:а|е)?|скрипт|терминал|команд(?:а|у|ы)|"
    r"file|folder|repository|repo|project|create|edit|modify|fix|delete|implement|"
    r"run|tests?|check\s+the\s+project|patch|deployment"
    r")\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[\w-]{3,}", re.UNICODE)


def classify_execution_profile(text: str, *, has_attachments: bool = False) -> ExecutionProfile:
    """Classify a message conservatively using deterministic, cheap rules."""
    if has_attachments or _CODE_INTENT_RE.search(text):
        return "code"
    return "chat"


def effective_mode(session_mode: str, profile: ExecutionProfile) -> str:
    """Choose the agent mode while allowing code requests from Chat mode."""
    if profile == "chat":
        return "chat"
    return "code" if session_mode == "chat" else session_mode


def effective_web_policy(session_policy: str | None, mode_default: str) -> str:
    """Return the binary web policy, accepting legacy enabled values."""
    policy = (session_policy or "").strip().lower()
    if policy in {"on", "required"}:
        return "on"
    if policy in {"off", "auto"}:
        return "off"
    return "on" if mode_default in {"on", "required"} else "off"


def select_relevant_notes(
    notes: Sequence[Mapping[str, object]], query: str, *, char_budget: int
) -> list[str]:
    """Select memory notes by keyword overlap, bounded by a hard character budget."""
    query_tokens = {token.lower() for token in _TOKEN_RE.findall(query)}
    ranked: list[tuple[int, int, str]] = []
    for index, row in enumerate(notes):
        note = str(row.get("note", "")).strip()
        if not note:
            continue
        overlap = len(query_tokens & {token.lower() for token in _TOKEN_RE.findall(note)})
        ranked.append((overlap, -index, note))
    ranked.sort(reverse=True)

    selected: list[str] = []
    used = 0
    for _overlap, _index, note in ranked:
        available = char_budget - used - (2 if selected else 0)
        if available <= 0:
            break
        bounded = note[:available].rstrip()
        if not bounded:
            break
        selected.append(bounded)
        used += len(bounded) + (2 if len(selected) > 1 else 0)
    return selected
