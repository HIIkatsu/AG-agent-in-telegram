"""Host-side Mistral image generation for task-scoped Telegram artifacts.

The AGY worker never receives a Mistral key.  It asks the short-lived local
capability broker to generate an image, and this module writes the verified
bytes directly into that task's already allocated artifact directory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)

_MAX_PROMPT_CHARS = 8_000
_IMAGE_URL_RE = re.compile(r"https://[^\s\]>)\"']+", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpeg|webp|gif|bmp));base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
_SAFE_SUFFIXES = frozenset(_IMAGE_SUFFIXES.values())


class MistralImageError(RuntimeError):
    """The configured Mistral image service did not produce a safe image."""


@dataclass(frozen=True)
class GeneratedImage:
    """One verified image written to the current task's artifact directory."""

    filename: str
    path: Path
    size_bytes: int


def _parse_allowed_hosts(raw_value: str, api_url: str) -> frozenset[str]:
    """Build a narrow download allow-list, including the configured API host."""
    hosts = {"files.mistral.ai"}
    configured_host = (urlparse(api_url).hostname or "").lower()
    if configured_host:
        hosts.add(configured_host)
    for raw_host in raw_value.split(","):
        host = raw_host.strip().lower()
        if host:
            hosts.add(host)
    return frozenset(hosts)


def _find_image_urls(value: object) -> list[str]:
    """Extract image URLs from both documented and compatible API payloads."""
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            found.extend(_IMAGE_URL_RE.findall(item))
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in {"url", "image_url", "file_url"} and isinstance(child, str):
                    found.extend(_IMAGE_URL_RE.findall(child))
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    unique: list[str] = []
    for url in found:
        if url not in unique:
            unique.append(url)
    return unique


def _find_inline_images(value: object) -> list[tuple[bytes, str]]:
    """Extract base64/data-URL images returned instead of downloadable URLs."""
    found: list[tuple[bytes, str]] = []

    def append_data(content_type: str, raw_b64: str) -> None:
        try:
            data = base64.b64decode("".join(raw_b64.split()), validate=True)
        except (binascii.Error, ValueError):
            return
        if data and _looks_like_image(data):
            found.append((data, content_type.lower()))

    def visit(item: object) -> None:
        if isinstance(item, str):
            for match in _DATA_IMAGE_RE.finditer(item):
                append_data(match.group(1), match.group(2))
        elif isinstance(item, dict):
            content_type = str(item.get("mime_type") or item.get("content_type") or "image/png")
            for key in ("b64_json", "base64", "image_base64", "data"):
                child = item.get(key)
                if isinstance(child, str):
                    data_url = _DATA_IMAGE_RE.search(child)
                    if data_url:
                        append_data(data_url.group(1), data_url.group(2))
                    else:
                        append_data(content_type, child)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _safe_image_suffix(content_type: str | None, url: str) -> str:
    """Pick one Telegram-compatible suffix without trusting remote filenames."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in _IMAGE_SUFFIXES:
        return _IMAGE_SUFFIXES[normalized]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in _SAFE_SUFFIXES else ".png"


def _looks_like_image(data: bytes) -> bool:
    """Reject HTML/JSON error pages returned by an otherwise successful CDN."""
    return data.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"RIFF")
    )


class MistralImageClient:
    """Minimal async client for Mistral's image-generation chat tool."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        model: str,
        allowed_download_hosts: Iterable[str],
        timeout_seconds: int,
        max_file_bytes: int,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_url = api_url.strip()
        self._model = model.strip()
        self._allowed_download_hosts = frozenset(
            str(host).strip().lower()
            for host in allowed_download_hosts
            if str(host).strip()
        )
        self._timeout_seconds = max(1, timeout_seconds)
        self._max_file_bytes = max(1, max_file_bytes)

    @classmethod
    def from_settings(cls) -> MistralImageClient:
        api_key = (settings.mistral_api_key or "").strip()
        if not api_key:
            raise MistralImageError("MISTRAL_API_KEY is not configured")
        api_url = settings.mistral_image_api_url.strip()
        parsed = urlparse(api_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise MistralImageError("MISTRAL_IMAGE_API_URL must be an HTTPS URL")
        model = settings.mistral_image_model.strip()
        if not model:
            raise MistralImageError("MISTRAL_IMAGE_MODEL is not configured")
        return cls(
            api_key=api_key,
            api_url=api_url,
            model=model,
            allowed_download_hosts=_parse_allowed_hosts(
                settings.mistral_image_download_hosts,
                api_url,
            ),
            timeout_seconds=settings.mistral_image_timeout_seconds,
            max_file_bytes=int(settings.artifact_max_size_mb) * 1024 * 1024,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    async def _post_completion(self, session: aiohttp.ClientSession, prompt: str) -> object:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Generate exactly one image for this request. Do not return a text-only "
                        f"answer. Request: {prompt}"
                    ),
                }
            ],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "any",
        }
        async with session.post(self._api_url, headers=self._headers(), json=payload) as response:
            raw = await response.text()
            if response.status >= 400:
                # Do not surface a provider response body to the agent: a
                # custom endpoint may include implementation details or other
                # sensitive diagnostics in it.  The HTTP status is enough for
                # the user-facing error while the provider owns deeper logs.
                raise MistralImageError(
                    f"Mistral image API returned HTTP {response.status}"
                )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MistralImageError("Mistral image API returned invalid JSON") from exc

    def _validate_download_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise MistralImageError("Mistral returned an invalid image URL")
        if parsed.hostname.lower() not in self._allowed_download_hosts:
            raise MistralImageError("Mistral returned an image URL outside the configured allow-list")

    async def _download_image(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> tuple[bytes, str | None]:
        self._validate_download_url(url)
        async with session.get(url, headers=self._headers()) as response:
            if response.status >= 400:
                raise MistralImageError(
                    f"Mistral image download returned HTTP {response.status}"
                )
            content_length = response.content_length
            if content_length is not None and content_length > self._max_file_bytes:
                raise MistralImageError("Mistral image exceeds the configured artifact size limit")
            data = await response.content.read(self._max_file_bytes + 1)
            if len(data) > self._max_file_bytes:
                raise MistralImageError("Mistral image exceeds the configured artifact size limit")
            content_type = response.headers.get("Content-Type")
        if not data or not _looks_like_image(data):
            raise MistralImageError("Mistral download did not contain an image file")
        return data, content_type

    async def generate(self, prompt: str, artifact_dir: str | Path) -> list[GeneratedImage]:
        """Generate and persist Mistral output in the one trusted task directory."""
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise MistralImageError("image prompt cannot be empty")
        if len(normalized_prompt) > _MAX_PROMPT_CHARS:
            raise MistralImageError("image prompt is too long")
        target = Path(artifact_dir)
        if not target.is_dir() or target.is_symlink():
            raise MistralImageError("task artifact directory is unavailable")

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                response = await self._post_completion(session, normalized_prompt)
                urls = _find_image_urls(response)
                if urls:
                    data, content_type = await self._download_image(session, urls[0])
                else:
                    inline_images = _find_inline_images(response)
                    if not inline_images:
                        raise MistralImageError("Mistral did not return an image file")
                    data, content_type = inline_images[0]
                    if len(data) > self._max_file_bytes:
                        raise MistralImageError("Mistral image exceeds the configured artifact size limit")
        except asyncio.TimeoutError as exc:
            raise MistralImageError("Mistral image request timed out") from exc
        except aiohttp.ClientError as exc:
            logger.warning("Mistral image request failed: %s", type(exc).__name__)
            raise MistralImageError("Mistral image request failed") from exc

        source_url = urls[0] if urls else ""
        filename = f"mistral-image-1{_safe_image_suffix(content_type, source_url)}"
        path = target / filename
        try:
            file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(data)
        except OSError as exc:
            raise MistralImageError("cannot save generated image into task artifacts") from exc
        logger.info("Mistral image saved for Telegram delivery: %s (%d bytes)", filename, len(data))
        return [GeneratedImage(filename=filename, path=path, size_bytes=len(data))]


async def generate_mistral_image(prompt: str, artifact_dir: str | Path) -> dict[str, object]:
    """Capability-broker entry point returning no remote URL or secret data."""
    images = await MistralImageClient.from_settings().generate(prompt, artifact_dir)
    return {
        "status": "saved_for_telegram",
        "files": [image.filename for image in images],
        "count": len(images),
    }
