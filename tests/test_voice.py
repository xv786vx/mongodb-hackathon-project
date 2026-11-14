import asyncio

import httpx
import pytest

from bot.config import Settings
from bot.tts import TtsError, speak
from mystery_core.voice import (BASE, MAX_CHARS, TAG_MODEL, prepare_text, settings_for, strip_tags)


def test_truth_and_lie_sound_identical():
    for conf in (1.0, 0.6, 0.2):
        for breaks in (0, 2):
            assert settings_for("truth", conf, breaks) == settings_for("lie", conf, breaks)
    assert BASE["truth"] == BASE["lie"]


def test_decisions_are_audibly_ordered():
    s = {d: settings_for(d) for d in BASE}
    assert s["truth"].stability > s["deflect"].stability > s["crack"].stability
    assert s["crack"].style > s["deflect"].style > s["truth"].style


def test_low_confidence_sounds_unsure_and_values_stay_in_range():
    assert settings_for("truth", 0.2).stability < settings_for("truth", 1.0).stability
    for d in BASE:
        for conf in (-1, 0, 1, 5):
            for breaks in (0, 10):
                v = settings_for(d, conf, breaks)
                assert 0 <= v.stability <= 1 and 0 <= v.style <= 1


def test_breaks_only_shake_deflect_and_crack():
    assert settings_for("truth", 1, 3) == settings_for("truth", 1, 0)
    assert settings_for("deflect", 1, 3).stability < settings_for("deflect", 1, 0).stability


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError):
        settings_for("whisper")


def test_model_brackets_are_stripped():
    assert strip_tags("I was [shouting] below stairs [whispers] sir.") == "I was below stairs sir."
    assert "[" not in prepare_text("Fine. [laughs] You win.", "crack", "eleven_flash_v2_5")


def test_whitelisted_tag_only_on_v3_crack():
    assert prepare_text("It was me.", "crack", TAG_MODEL).startswith("[sighs] ")
    assert not prepare_text("It was me.", "crack", "eleven_flash_v2_5").startswith("[")
    assert not prepare_text("Not me.", "lie", TAG_MODEL).startswith("[")


def test_long_replies_are_capped():
    assert len(prepare_text("word " * 500, "truth")) <= MAX_CHARS + 1


# ---- tts.py (no real network) ---------------------------------------------------------------------
CFG = Settings(elevenlabs_api_key="xi-secret")


def run(handler, reply="Hello [sighs] there."):
    return asyncio.run(speak(CFG, "voice123", reply, "deflect", settings_for("deflect"),
                             transport=httpx.MockTransport(handler)))


def test_speak_sends_only_text_voice_and_settings_and_returns_bytes():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(200, content=b"ID3audio")

    assert run(handler) == b"ID3audio"
    req = seen["req"]
    assert req.url.path == "/v1/text-to-speech/voice123"
    assert req.headers["xi-api-key"] == "xi-secret"
    body = req.read().decode()
    assert "Hello there." in body and "[sighs]" not in body       # tags stripped before sending
    assert "stability" in body and "eleven_flash_v2_5" in body


def test_speak_errors_never_leak_the_key():
    with pytest.raises(TtsError) as e:
        run(lambda r: httpx.Response(401, text="bad key xi-secret"))
    assert "xi-secret" not in str(e.value)


def test_speak_needs_a_key():
    with pytest.raises(TtsError):
        asyncio.run(speak(Settings(), "v", "hi", "truth", settings_for("truth")))


def test_crack_uses_the_stronger_model_and_others_stay_on_flash():
    from mystery_core.voice import CRACK_MODEL, DEFAULT_MODEL
    assert settings_for("crack").model == CRACK_MODEL
    assert settings_for("crack", model=TAG_MODEL).model == TAG_MODEL      # explicit choice wins
    for d in ("truth", "lie", "deflect"):
        assert settings_for(d).model == DEFAULT_MODEL
    assert settings_for("truth").model == settings_for("lie").model
