"""Load stable and machine-local agent instructions without touching workspaces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INSTRUCTIONS_PATH = BOT_ROOT / "INSTRUCTIONS.md"
DEFAULT_LOCAL_INSTRUCTIONS_PATH = BOT_ROOT / "INSTRUCTIONS.local.md"


@dataclass(frozen=True)
class InstructionBundle:
    """Resolved runtime instructions and their reproducibility metadata."""

    content: str
    sha256: str
    source_names: tuple[str, ...]


def load_instruction_bundle(
    base_path: Path = DEFAULT_INSTRUCTIONS_PATH,
    local_path: Path = DEFAULT_LOCAL_INSTRUCTIONS_PATH,
) -> InstructionBundle:
    """Combine tracked defaults with optional ignored local context."""
    sections: list[str] = []
    source_names: list[str] = []

    for path in (base_path, local_path):
        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        if not content:
            continue
        sections.append(content)
        source_names.append(path.name)

    combined = "\n\n".join(sections)
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return InstructionBundle(
        content=combined,
        sha256=digest,
        source_names=tuple(source_names),
    )


@lru_cache(maxsize=1)
def get_instruction_bundle() -> InstructionBundle:
    """Return one immutable instruction snapshot for the current bot process."""
    return load_instruction_bundle()
