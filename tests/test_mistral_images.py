"""Tests for the host-side Mistral image capability."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "antigravity-bot"))
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")

from bot.services.mistral_images import (  # noqa: E402
    MistralImageClient,
    MistralImageError,
    _find_image_urls,
    _parse_allowed_hosts,
)

_PNG = b"\x89PNG\r\n\x1a\nexample-image"


def test_extracts_a_mistral_image_url_from_tool_completion_payload() -> None:
    payload = {
        "choices": [
            {
                "messages": [
                    {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": "https://files.mistral.ai/generated/image-1.png",
                            }
                        ]
                    }
                ]
            }
        ]
    }

    assert _find_image_urls(payload) == [
        "https://files.mistral.ai/generated/image-1.png"
    ]


def test_extracts_image_url_from_choice_messages_shape() -> None:
    payload = {
        "choices": [
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "[Image: https://files.mistral.ai/generated/abc123.png]",
                    }
                ]
            }
        ]
    }

    assert _find_image_urls(payload) == [
        "https://files.mistral.ai/generated/abc123.png"
    ]


def test_generated_image_is_written_only_to_the_task_artifact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MistralImageClient(
        api_key="private-key",
        api_url="https://api.mistral.ai/v1/chat/completions",
        model="mistral-small-latest",
        allowed_download_hosts={"files.mistral.ai", "api.mistral.ai"},
        timeout_seconds=30,
        max_file_bytes=1024,
    )

    async def fake_completion(_session, _prompt: str) -> object:
        return {"image_url": "https://files.mistral.ai/generated/image-1.png"}

    async def fake_download(_session, url: str) -> tuple[bytes, str | None]:
        assert url == "https://files.mistral.ai/generated/image-1.png"
        return _PNG, "image/png"

    monkeypatch.setattr(client, "_post_completion", fake_completion)
    monkeypatch.setattr(client, "_download_image", fake_download)

    artifacts = tmp_path / "task-91"
    artifacts.mkdir()
    images = asyncio.run(client.generate("Ночной город в стиле киберпанк", artifacts))

    assert [image.filename for image in images] == ["mistral-image-1.png"]
    assert (artifacts / "mistral-image-1.png").read_bytes() == _PNG


def test_mistral_request_forces_the_image_tool(monkeypatch) -> None:
    client = MistralImageClient(
        api_key="private-key",
        api_url="https://api.mistral.ai/v1/chat/completions",
        model="mistral-small-latest",
        allowed_download_hosts={"files.mistral.ai"},
        timeout_seconds=30,
        max_file_bytes=1024,
    )

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self) -> str:
            return '{"image_url":"https://files.mistral.ai/generated/one.png"}'

    class _Session:
        def __init__(self) -> None:
            self.payload = None

        def post(self, _url, *, headers, json):
            self.payload = json
            return _Response()

    session = _Session()
    asyncio.run(client._post_completion(session, "кот"))

    assert session.payload["tools"] == [{"type": "image_generation"}]
    assert session.payload["tool_choice"] == "any"


def test_rejects_download_url_outside_mistral_allow_list() -> None:
    client = MistralImageClient(
        api_key="private-key",
        api_url="https://api.mistral.ai/v1/chat/completions",
        model="mistral-small-latest",
        allowed_download_hosts={"files.mistral.ai"},
        timeout_seconds=30,
        max_file_bytes=1024,
    )

    with pytest.raises(MistralImageError, match="allow-list"):
        client._validate_download_url("https://example.invalid/image.png")


def test_custom_mistral_endpoint_host_is_allowed_for_its_image_download() -> None:
    hosts = _parse_allowed_hosts(
        "images.custom-mistral.example",
        "https://gateway.custom-mistral.example/v1/chat/completions",
    )

    assert hosts == {
        "files.mistral.ai",
        "gateway.custom-mistral.example",
        "images.custom-mistral.example",
    }
