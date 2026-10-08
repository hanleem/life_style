"""TTS engine seam. Only the abstraction lives here (no real engine code, no network).

Engines run on the owner's PC only. The engine list has no cloud entry (plan D7); unknown names
fall back to the unavailable engine, never to anything that could send text elsewhere.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import logging
import math
import sys
import wave
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

SPEED_MIN, SPEED_MAX = 0.5, 2.0


class TtsError(Exception):
    """Base class for every synthesis failure surfaced to callers."""


class TtsUnavailable(TtsError):
    """The engine cannot produce audio (not installed, wrong platform, disabled, failed)."""


SYNTH_SPEED = 1.0


def synth_speed() -> float:
    """Speed the engine synthesizes at. Constant: the user's playback rate is applied client-side
    (audio.playbackRate), so it never enters the cache key."""
    return SYNTH_SPEED


def engine_id(tts) -> str:
    """Cache-key identity of an engine+voice, e.g. "windows:Microsoft Heami"."""
    return f"{tts.name}:{tts.voice_id}"


def check_speed(speed) -> float:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) \
            or not (SPEED_MIN <= speed <= SPEED_MAX):
        raise ValueError(f"속도는 {SPEED_MIN}~{SPEED_MAX} 사이 숫자여야 합니다")
    return float(speed)


def check_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("낭독할 문장이 비어 있습니다")
    return text


@runtime_checkable
class TtsEngine(Protocol):
    name: str
    voice_id: str

    def available(self) -> bool: ...

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes: ...


class FakeTts:
    """Deterministic offline engine for tests: a short WAV whose bytes depend on (text, speed)."""

    name = "fake"
    voice_id = "fake-ko"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, float]] = []   # (text, speed)

    def available(self) -> bool:
        return not self.fail

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        check_text(text)
        sp = check_speed(speed)
        self.calls.append((text, sp))
        if self.fail:
            raise TtsUnavailable("가짜 엔진이 실패하도록 설정되었습니다")
        frames = min(int(len(text) * 800 / sp), 160_000)
        seed = hashlib.sha256(f"{text}|{sp:.3f}".encode("utf-8")).digest()
        pcm = (seed * (frames * 2 // len(seed) + 1))[: frames * 2]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        return buf.getvalue()


class NullTts:
    """Placeholder when no engine is usable; synthesis raises a clear typed error."""

    name = "none"
    voice_id = "none"

    def available(self) -> bool:
        return False

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        raise TtsUnavailable("사용할 수 있는 음성 엔진이 없습니다")


FakeEngine = FakeTts
UnavailableEngine = NullTts


def make_engine(name: str) -> TtsEngine:
    if name == "windows" and sys.platform == "win32":
        try:
            mod = importlib.import_module("pa.voice.tts_windows")
            return mod.WindowsTts()
        except Exception:  # missing module (before V3) or engine init failure
            log.warning("voice: windows engine unavailable, using none")
    return NullTts()
