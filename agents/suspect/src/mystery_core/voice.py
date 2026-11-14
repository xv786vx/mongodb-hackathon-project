"""Decision -> voice settings. Pure: no API calls, no Discord, no ElevenLabs imports (AGENTS.md rules 19-21).

Inputs are facts the engine already holds: the decision, the mean confidence of the memories the suspect
retrieved, and how many times the cover story has been broken. Voice never decides access, and nothing here
looks at a player.

`truth` and `lie` use identical settings, so the voice is never a tell.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# decision -> (stability, style). Starting points to tune by ear (ARCHITECTURE.md section 8).
BASE: dict[str, tuple[float, float]] = {
    "truth": (0.80, 0.10),
    "lie": (0.80, 0.10),          # identical to truth, on purpose
    "deflect": (0.45, 0.50),
    "crack": (0.10, 0.90),        # chosen by ear: Flash barely moves with these, so crack uses CRACK_MODEL
}

DEFAULT_MODEL = "eleven_flash_v2_5"
CRACK_MODEL = "eleven_multilingual_v2"   # follows stability and style far more audibly than Flash; slower
TAG_MODEL = "eleven_v3"            # inline audio tags work only on this model
TAG_WHITELIST: dict[str, str] = {"crack": "[sighs]"}   # the only tags we ever add; the model cannot add its own
SIMILARITY = 0.75
MAX_CHARS = 600                    # speech is billed per character; one clip per reply

_TAGS = re.compile(r"\[[^\]]*\]")


@dataclass(frozen=True)
class VoiceSettings:
    model: str
    stability: float
    style: float
    similarity_boost: float = SIMILARITY

    def to_trace(self, voice_id: str) -> dict:
        """The `voice` block recorded on the trace."""
        return {"voice_id": voice_id, "model": self.model, "stability": self.stability, "style": self.style}

    def to_api(self) -> dict:
        d = asdict(self)
        d.pop("model")
        return d


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def strip_tags(text: str) -> str:
    """Remove every [bracketed] span, so the model cannot direct its own delivery."""
    return re.sub(r"\s{2,}", " ", _TAGS.sub("", text)).strip()


def settings_for(decision: str, mean_confidence: float = 1.0, cover_breaks: int = 0,
                 model: str = DEFAULT_MODEL) -> VoiceSettings:
    if decision not in BASE:
        raise ValueError(f"unknown decision: {decision!r}")
    stability, style = BASE[decision]
    if decision == "crack" and model == DEFAULT_MODEL:     # an explicit other model (such as v3) is respected
        model = CRACK_MODEL
    # A suspect repeating a fading rumour sounds unsure.
    stability *= 0.6 + 0.4 * _clamp(mean_confidence)
    # A suspect who has been broken before sounds shakier. Does not touch truth or lie.
    if decision in ("deflect", "crack"):
        stability -= 0.05 * min(max(cover_breaks, 0), 3)
    return VoiceSettings(model=model, stability=round(_clamp(stability), 3), style=round(_clamp(style), 3))


def prepare_text(reply: str, decision: str, model: str = DEFAULT_MODEL) -> str:
    """Text to speak: model brackets stripped, trimmed to the cost cap, whitelisted tag added on v3 only."""
    text = strip_tags(reply)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(" ", 1)[0] + "…"
    tag = TAG_WHITELIST.get(decision)
    if tag and model == TAG_MODEL:
        text = f"{tag} {text}"
    return text
