"""Regression tests for aiogram 3's frozen CallbackQuery model."""

import ast
from pathlib import Path


HANDLERS_DIR = Path(__file__).parents[1] / "antigravity-bot" / "bot" / "handlers"


def test_handlers_do_not_assign_callback_query_data() -> None:
    """Refreshing a screen must not mutate ``cq.data`` directly."""
    mutations: list[str] = []

    for path in HANDLERS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "cq"
                    and target.attr == "data"
                ):
                    mutations.append(f"{path.name}:{node.lineno}")

    assert mutations == []
