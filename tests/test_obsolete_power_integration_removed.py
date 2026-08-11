"""Ensure the retired cloud power integration cannot return accidentally."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_FILES = [
    ROOT / "antigravity-bot" / "requirements.txt",
    ROOT / "antigravity-bot" / ".env.example",
    ROOT / "antigravity-bot" / "INSTRUCTIONS.md",
    ROOT / "antigravity-bot" / "bot" / "config.py",
    ROOT / "antigravity-bot" / "bot" / "services" / "power_manager.py",
    ROOT / "antigravity-bot" / "bot" / "services" / "power_tool.py",
]


def test_retired_cloud_power_integration_is_absent_from_runtime() -> None:
    retired_terms = ("micloud", "xiaomi", "hard_wake_up", "list_devices")

    for path in RUNTIME_FILES:
        content = path.read_text(encoding="utf-8").casefold()
        for term in retired_terms:
            assert term.casefold() not in content, f"{term!r} remains in {path}"
