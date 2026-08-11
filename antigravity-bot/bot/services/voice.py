"""Cloud voice transcription with async ffmpeg conversion and fallback STT."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
WIT_DICTATION_URL = "https://api.wit.ai/dictation?v=20240215"
HTTP_TIMEOUT_SECONDS = 60


class TranscriptionError(RuntimeError):
    """Raised when a cloud STT provider cannot transcribe audio."""


async def _convert_ogg_to_wav(ogg_path: str) -> str:
    """Convert Telegram OGG/Opus to mono 16 kHz WAV without blocking the event loop."""
    wav_path = str(Path(ogg_path).with_suffix(".wav"))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        ogg_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        wav_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        details = stderr.decode("utf-8", errors="replace").strip()
        raise TranscriptionError(f"ffmpeg exited with code {proc.returncode}: {details}")
    return wav_path


async def transcribe_with_groq(audio_path: str) -> str:
    """Transcribe an audio file via Groq's OpenAI-compatible transcription API."""
    from bot.config import settings

    api_key = settings.groq_api_key
    if not api_key:
        raise TranscriptionError("GROQ_API_KEY is not set")

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    form = aiohttp.FormData()
    form.add_field("model", GROQ_MODEL)
    form.add_field("language", "ru")
    with open(audio_path, "rb") as audio_file:
        form.add_field(
            "file",
            audio_file,
            filename=Path(audio_path).name,
            content_type="audio/wav",
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                GROQ_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                data=form,
            ) as response:
                payload = await response.text()
                if response.status >= 400:
                    raise TranscriptionError(f"Groq returned HTTP {response.status}: {payload[:500]}")
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise TranscriptionError("Groq returned invalid JSON") from exc

    text = str(data.get("text", "")).strip()
    if not text:
        raise TranscriptionError("Groq returned an empty transcription")
    return text


async def transcribe_with_wit(audio_path: str) -> str:
    """Fallback transcription via Wit.ai dictation API."""
    from bot.config import settings

    token = settings.wit_ai_token
    if not token:
        raise TranscriptionError("WIT_AI_TOKEN is not set")

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "audio/wav",
        "Accept": "application/json",
    }
    with open(audio_path, "rb") as audio_file:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(WIT_DICTATION_URL, headers=headers, data=audio_file) as response:
                payload = await response.text()
                if response.status >= 400:
                    raise TranscriptionError(f"Wit.ai returned HTTP {response.status}: {payload[:500]}")

    return _extract_wit_text(payload)


def _extract_wit_text(payload: str) -> str:
    """Extract final text from Wit.ai dictation JSON or newline-delimited JSON."""
    candidates: list[str] = []
    for raw_line in payload.splitlines() or [payload]:
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = data.get("text") or data.get("_text")
        if text:
            candidates.append(str(text).strip())

    text = " ".join(part for part in candidates if part).strip()
    if not text:
        raise TranscriptionError("Wit.ai returned an empty transcription")
    return text


async def transcribe_voice(ogg_path: str) -> str:
    """Convert Telegram OGG to WAV, transcribe with Groq, then fallback to Wit.ai."""
    wav_path: str | None = None
    try:
        wav_path = await _convert_ogg_to_wav(ogg_path)
        try:
            text = await transcribe_with_groq(wav_path)
            logger.info("Transcribed %s with Groq: %d chars", ogg_path, len(text))
            return text
        except (aiohttp.ClientError, asyncio.TimeoutError, TranscriptionError) as exc:
            logger.warning("Groq transcription failed, falling back to Wit.ai: %s", exc)
            text = await transcribe_with_wit(wav_path)
            logger.info("Transcribed %s with Wit.ai fallback: %d chars", ogg_path, len(text))
            return text
    finally:
        for path in (ogg_path, wav_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Failed to remove temporary audio file %s: %s", path, exc)
