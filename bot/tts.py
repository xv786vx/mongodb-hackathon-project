"""ElevenLabs text-to-speech. Returns audio bytes in memory: nothing is written to disk or served from a URL (rule 20)."""
from __future__ import annotations

import logging

import httpx

from bot.config import Settings
from mystery_core.voice import VoiceSettings, prepare_text

log = logging.getLogger("bot.tts")

API = "https://api.elevenlabs.io/v1/text-to-speech"
OUTPUT_FORMAT = "mp3_44100_128"


class TtsError(Exception):
    pass


async def speak(cfg: Settings, voice_id: str, reply: str, decision: str, settings: VoiceSettings,
                *, transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """One clip per suspect reply. The reply text is the only content sent, plus the voice id and settings."""
    if not cfg.elevenlabs_api_key:
        raise TtsError("ELEVENLABS_API_KEY is not set")
    text = prepare_text(reply, decision, settings.model)
    if not text:
        raise TtsError("nothing to say")
    body = {"text": text, "model_id": settings.model, "voice_settings": settings.to_api()}
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as c:
            r = await c.post(f"{API}/{voice_id}", params={"output_format": OUTPUT_FORMAT},
                             headers={"xi-api-key": cfg.elevenlabs_api_key}, json=body)
    except httpx.HTTPError as e:
        raise TtsError(f"request failed: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        # Status only. Bodies and headers can carry the key or reply text.
        raise TtsError(f"elevenlabs returned {r.status_code}")
    log.info("clip for voice %s: %d bytes", voice_id, len(r.content))
    return r.content
