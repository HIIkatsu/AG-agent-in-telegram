"""Voice transcription: OGG → WAV → faster-whisper (lazy-loaded)."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# ── Lazy-loaded singleton ────────────────────────────────────────────────
_model = None
_model_lock = asyncio.Lock()


async def _get_model():
    """Load the Whisper model on first call (not at import time)."""
    global _model
    if _model is not None:
        return _model

    async with _model_lock:
        # double-check after acquiring lock
        if _model is not None:
            return _model

        from bot.config import settings

        model_name = settings.whisper_model
        logger.info("Loading faster-whisper model '%s' (lazy, first voice call)…", model_name)

        loop = asyncio.get_running_loop()

        def _load():
            from faster_whisper import WhisperModel

            return WhisperModel(model_name, device="cpu", compute_type="int8")

        _model = await loop.run_in_executor(None, _load)
        logger.info("Whisper model '%s' loaded.", model_name)
        return _model


# ── Public API ───────────────────────────────────────────────────────────

async def transcribe_voice(ogg_path: str) -> str:
    """Full-length OGG→text transcription.  No 30-second truncation."""
    wav_path = ogg_path.rsplit(".", 1)[0] + ".wav"

    try:
        # 1. OGG → WAV (16 kHz mono) via ffmpeg
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", ogg_path,
            "-ar", "16000", "-ac", "1", "-f", "wav", wav_path, "-y",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

        # 2. Transcribe — iterate ALL segments
        model = await _get_model()
        loop = asyncio.get_running_loop()

        def _run():
            segments, info = model.transcribe(
                wav_path,
                language="ru",
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=300,
                ),
            )
            text_parts: list[str] = []
            for seg in segments:
                text_parts.append(seg.text.strip())
            return " ".join(text_parts), info.language

        text, lang = await loop.run_in_executor(None, _run)
        logger.info(
            "Transcribed %s: lang=%s, %d chars", ogg_path, lang, len(text)
        )
        return text

    finally:
        for p in (ogg_path, wav_path):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
