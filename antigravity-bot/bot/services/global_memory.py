"""Build a bounded, reproducible snapshot of global user memory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.db import Database


DEFAULT_MEMORY_CHAR_BUDGET = 8_000
MAX_FACT_CHARS = 1_000


@dataclass(frozen=True)
class GlobalMemorySnapshot:
    """Effective global-memory context injected into one agent task."""

    content: str
    sha256: str
    count: int
    total_count: int
    truncated: bool


def _serialize(facts: list[dict[str, object]]) -> str:
    # Keep user data from terminating the runtime prompt's memory delimiter.
    return (
        json.dumps(facts, ensure_ascii=False, indent=2)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build_global_memory_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    char_budget: int = DEFAULT_MEMORY_CHAR_BUDGET,
) -> GlobalMemorySnapshot:
    """Serialize user facts as bounded JSON data, preserving database order."""
    if char_budget < 2:
        raise ValueError("char_budget must be at least 2 characters")

    normalized: list[dict[str, object]] = []
    total_count = 0
    truncated = False

    for row in rows:
        fact = " ".join(str(row.get("fact", "")).split())
        if not fact:
            continue
        total_count += 1
        if len(fact) > MAX_FACT_CHARS:
            fact = fact[: MAX_FACT_CHARS - 1] + "…"
            truncated = True

        candidate = [*normalized, {"id": row.get("id"), "fact": fact}]
        if len(_serialize(candidate)) > char_budget:
            truncated = True
            continue
        normalized = candidate

    content = _serialize(normalized)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return GlobalMemorySnapshot(
        content=content,
        sha256=digest,
        count=len(normalized),
        total_count=total_count,
        truncated=truncated,
    )


async def load_global_memory_snapshot(
    database: Database | None = None,
    *,
    char_budget: int = DEFAULT_MEMORY_CHAR_BUDGET,
) -> GlobalMemorySnapshot:
    """Read global user memory from SQLite for the current task."""
    if database is None:
        from bot.db import db

        database = db
    rows = await database.get_all_user_memory()
    return build_global_memory_snapshot(rows, char_budget=char_budget)
