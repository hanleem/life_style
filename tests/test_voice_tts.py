import asyncio
import io
import re
import sys
import wave
from pathlib import Path

import pytest

from pa.voice import tts
from pa.voice.tts import (FakeEngine, FakeTts, NullTts, TtsEngine, TtsError, TtsUnavailable, UnavailableEngine,
                          make_engine)


async def test_fake_returns_wav_header():
    e = FakeTts()
    data = await e.synthesize("안녕하세요", speed=1.0)
    assert data[:4] == b"RIFF"
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    assert e.calls == [("안녕하세요", 1.0)]


async def test_fake_is_deterministic_and_speed_sensitive():
    e = FakeEngine()
    a = await e.synthesize("같은 문장", speed=1.0)
    assert a == await e.synthesize("같은 문장", speed=1.0)
    assert a != await e.synthesize("다른 문장", speed=1.0)
    assert a != await e.synthesize("같은 문장", speed=1.5)


async def test_fake_failure_raises_unavailable():
    with pytest.raises(TtsUnavailable):
        await FakeTts(fail=True).synthesize("x", speed=1.0)


async def test_null_engine_unavailable():
    e = NullTts()
    assert not e.available()
    with pytest.raises(TtsUnavailable) as ei:
        await e.synthesize("x", speed=1.0)
    assert isinstance(ei.value, TtsError) and str(ei.value)
    assert UnavailableEngine is NullTts


@pytest.mark.parametrize("bad", [0, -1.0, 5.0, float("nan"), "1.0", True])
async def test_fake_rejects_bad_speed(bad):
    with pytest.raises(ValueError):
        await FakeTts().synthesize("x", speed=bad)


async def test_empty_text_rejected():
    with pytest.raises(ValueError):
        await FakeTts().synthesize("  ", speed=1.0)


def test_protocol_conformance():
    assert isinstance(FakeTts(), TtsEngine) and isinstance(NullTts(), TtsEngine)
    assert FakeTts().available()


def test_make_engine_none_and_unknown_platform_fallback(monkeypatch):
    assert isinstance(make_engine("none"), NullTts)
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(make_engine("windows"), NullTts)
    assert isinstance(make_engine("anything-else"), NullTts)


def test_tts_module_has_no_network_imports():
    import ast
    src = Path(tts.__file__).read_text(encoding="utf-8")
    banned = {"httpx", "requests", "urllib", "socket", "edge_tts", "gtts", "google", "azure", "boto3", "openai",
              "aiohttp", "http", "ssl", "winrt", "comtypes", "pyttsx3", "subprocess"}
    for node in ast.walk(ast.parse(src)):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [node.module or ""]
        for m in mods:
            assert m.split(".")[0] not in banned, m
    assert not re.search(r"https?://", src)


async def test_synthesize_default_speed_works():
    e = FakeTts()
    a = await e.synthesize("기본 속도")
    assert a == await e.synthesize("기본 속도", speed=1.0)
    assert e.calls == [("기본 속도", 1.0), ("기본 속도", 1.0)]


def test_engine_id_and_synth_speed_helpers():
    assert tts.engine_id(FakeTts()) == "fake:fake-ko"
    assert tts.engine_id(NullTts()) == "none:none"
    assert tts.synth_speed() == 1.0
    assert FakeTts.voice_id == "fake-ko" and NullTts.voice_id == "none"
