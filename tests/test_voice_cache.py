import asyncio
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pa.voice import cache as cache_mod
from pa.voice.cache import AudioCache, CacheError
from pa.voice.tts import FakeTts, TtsError, TtsUnavailable

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _age(path: Path, hours: float, now=NOW):
    t = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (t, t))


def _mk(tmp_path, **kw):
    return AudioCache(tmp_path / "voice_cache", clock=lambda: NOW, **kw)


def _files(c):
    return sorted(p.name for p in c.dir.iterdir())


def test_put_get_roundtrip(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h1", "windows", 1.0)
    assert c.get(k) is None
    c.put(k, b"RIFFdata")
    assert c.get(k) == b"RIFFdata"


def test_key_changes_with_every_component():
    c = AudioCache(Path("/nonexistent-never-created"))
    base = c.key("h1", "windows", 1.0)
    assert base == c.key("h1", "windows", 1)          # canonical speed
    for other in (c.key("h2", "windows", 1.0), c.key("h1", "none", 1.0), c.key("h1", "windows", 1.2)):
        assert other != base
    assert c.key("h1", "windows") == base                   # synth_speed defaults to 1.0
    # separator ambiguity must not collide
    assert c.key("a|b", "c", 1.0) != c.key("a", "b|c", 1.0)
    assert c.key("ab", "c", 1.0) != c.key("a", "bc", 1.0)
    assert len(base) == 32 and all(ch in "0123456789abcdef" for ch in base)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "1.0", True, None])
def test_key_rejects_bad_speed(bad):
    with pytest.raises(ValueError):
        AudioCache(Path("/x")).key("b", "e", bad)


def test_key_and_filenames_contain_no_user_strings(tmp_path):
    c = _mk(tmp_path)
    evil = "../../etc/passwd 한국어 \x00"
    k = c.key(evil, evil, 1.0)
    c.put(k, b"x")
    for name in _files(c):
        assert evil not in name and "/" not in name and ".." not in name
    assert Path(os.path.realpath(c.dir / _files(c)[0])).parent == Path(os.path.realpath(c.dir))


@pytest.mark.parametrize("badkey", ["../x", "a/b", "", "A" * 32, "g" * 32, "0" * 31, "0" * 33, "x.audio", None, 5])
def test_bad_keys_rejected_everywhere(tmp_path, badkey):
    c = _mk(tmp_path)
    with pytest.raises(ValueError):
        c.get(badkey)
    with pytest.raises(ValueError):
        c.put(badkey, b"x")
    with pytest.raises(ValueError):
        c.invalidate(badkey)


def test_put_rejects_non_bytes_and_empty(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    for bad in ("text", None, b"", bytearray(b"x")):
        with pytest.raises((ValueError, TypeError)):
            c.put(k, bad)


def test_stores_audio_only_never_text(tmp_path):
    c = _mk(tmp_path)
    k = c.key("SCRIPTHASH", "e", 1.0)
    c.put(k, b"\x01\x02audio")
    blob = b"".join(p.read_bytes() for p in c.dir.iterdir())
    assert b"SCRIPTHASH" not in blob
    assert all(p.suffix == ".audio" for p in c.dir.iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_owner_only_permissions(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    c.put(k, b"x")
    assert stat.S_IMODE(c.dir.stat().st_mode) == 0o700
    f = next(c.dir.iterdir())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_purge_by_age_boundary(tmp_path):
    c = _mk(tmp_path)
    ks = [c.key(f"b{i}", "e", 1.0) for i in range(3)]
    for k in ks:
        c.put(k, b"x")
    _age(c.dir / f"{ks[0]}.audio", 49)
    _age(c.dir / f"{ks[1]}.audio", 48)       # exactly at limit: kept
    _age(c.dir / f"{ks[2]}.audio", 47.99)
    assert c.purge() == 1
    assert c.get(ks[0]) is None and c.get(ks[1]) and c.get(ks[2])
    assert c.purge(NOW + timedelta(seconds=1)) == 1   # injected "now": ks[1] now older than 48h
    assert c.get(ks[1]) is None


def test_purge_by_count(tmp_path):
    c = _mk(tmp_path)
    ks = [c.key(f"b{i}", "e", 1.0) for i in range(31)]
    for i, k in enumerate(ks):
        c.put(k, b"x")
        _age(c.dir / f"{k}.audio", 31 - i)    # ks[0] oldest (31h), ks[30] newest (1h)
    assert len(_files(c)) <= 31
    assert c.purge() in (0, 1)
    assert len(_files(c)) == 30
    assert c.get(ks[0]) is None and c.get(ks[30]) is not None


def test_put_triggers_purge_and_keeps_new_file(tmp_path):
    c = _mk(tmp_path, max_files=2)
    ks = [c.key(f"b{i}", "e", 1.0) for i in range(4)]
    for i, k in enumerate(ks):
        c.put(k, b"x")
        _age(c.dir / f"{k}.audio", 10 - i)
    assert len(_files(c)) == 2
    assert c.get(ks[3]) is not None            # just-written (same mtime ties) is never purged


def test_put_with_all_same_mtime_keeps_new(tmp_path):
    c = _mk(tmp_path, max_files=1)
    k1, k2 = c.key("a", "e", 1.0), c.key("b", "e", 1.0)
    c.put(k1, b"x"); c.put(k2, b"y")
    assert c.get(k2) == b"y" and len(_files(c)) == 1


def test_purge_ignores_foreign_files_and_symlinks(tmp_path):
    c = _mk(tmp_path)
    c.dir.mkdir(parents=True)
    (c.dir / "notes.txt").write_text("keep")
    outside = tmp_path / "outside.audio"
    outside.write_bytes(b"x")
    link = c.dir / ("0" * 32 + ".audio")
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("no symlinks")
    _age(outside, 100)
    c.purge()
    assert (c.dir / "notes.txt").read_text() == "keep" and outside.exists()
    k = "0" * 32
    assert c.get(k) is None                    # symlinks are never followed


def test_purge_missing_dir_is_zero(tmp_path):
    assert _mk(tmp_path).purge() == 0


def test_purge_removes_stale_tmp_files(tmp_path):
    c = _mk(tmp_path)
    c.dir.mkdir(parents=True)
    t = c.dir / ".tmp-deadbeefdeadbeef"
    t.write_bytes(b"partial")
    _age(t, 49)
    assert c.purge() == 1 and not t.exists()


def test_atomic_write_no_partial_file(tmp_path, monkeypatch):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    def boom(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(CacheError):
        c.put(k, b"x" * 100)
    monkeypatch.undo()
    assert _files(c) == []                     # no final file, tmp cleaned up
    assert c.get(k) is None


def test_disk_full_on_write_is_typed_error(tmp_path, monkeypatch):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    real_write = os.write
    def full(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "write", full)
    with pytest.raises(CacheError):
        c.put(k, b"x")
    monkeypatch.setattr(os, "write", real_write)
    assert _files(c) == []


def test_permission_error_on_mkdir_is_typed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = AudioCache(blocker / "sub", clock=lambda: NOW)       # parent is a file -> cannot create
    with pytest.raises(CacheError):
        c.put(c.key("b", "e", 1.0), b"x")
    assert c.get(c.key("b", "e", 1.0)) is None          # reads of a missing cache are just misses


def test_corrupted_file_is_a_miss_and_removed(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    c.put(k, b"RIFF-good-data")
    p = c.dir / f"{k}.audio"
    raw = bytearray(p.read_bytes())
    raw[-1] ^= 0xFF
    p.write_bytes(bytes(raw))
    assert c.get(k) is None and not p.exists()
    for junk in (b"", b"abc", b"PAV1" + b"\0" * 10, os.urandom(200)):
        p.write_bytes(junk)
        assert c.get(k) is None and not p.exists()


async def test_get_or_create_regenerates_after_corruption(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    eng = FakeTts()
    async def make():
        return await eng.synthesize("문장", speed=1.0)
    a = await c.get_or_create(k, make)
    (c.dir / f"{k}.audio").write_bytes(b"garbage")
    b = await c.get_or_create(k, make)
    assert a == b and len(eng.calls) == 2
    assert await c.get_or_create(k, make) == a and len(eng.calls) == 2   # now cached


async def test_single_flight_same_key_synthesizes_once(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    n = 0
    gate = asyncio.Event()
    async def make():
        nonlocal n
        n += 1
        await gate.wait()
        return b"RIFFaudio"
    tasks = [asyncio.create_task(c.get_or_create(k, make)) for _ in range(5)]
    await asyncio.sleep(0.05)
    assert n == 1
    gate.set()
    assert set(await asyncio.gather(*tasks)) == {b"RIFFaudio"}
    assert n == 1
    assert c._inflight == {}


async def test_different_keys_run_in_parallel(tmp_path):
    c = _mk(tmp_path)
    started = 0
    gate = asyncio.Event()
    async def make():
        nonlocal started
        started += 1
        await gate.wait()
        return b"x"
    t = [asyncio.create_task(c.get_or_create(c.key(f"b{i}", "e", 1.0), make)) for i in range(3)]
    await asyncio.sleep(0.05)
    assert started == 3
    gate.set()
    await asyncio.gather(*t)


async def test_single_flight_failure_propagates_to_all_then_retries(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    calls = 0
    gate = asyncio.Event()
    async def bad():
        nonlocal calls
        calls += 1
        await gate.wait()
        raise TtsUnavailable("no engine")
    ts = [asyncio.create_task(c.get_or_create(k, bad)) for _ in range(3)]
    await asyncio.sleep(0.02)
    gate.set()
    res = await asyncio.gather(*ts, return_exceptions=True)
    assert calls == 1 and all(isinstance(r, TtsUnavailable) for r in res)
    assert c._inflight == {} and c.get(k) is None
    async def good():
        return b"ok"
    assert await c.get_or_create(k, good) == b"ok"          # failure is not cached


async def test_waiter_cancellation_does_not_break_others(tmp_path):
    c = _mk(tmp_path)
    k = c.key("b", "e", 1.0)
    gate = asyncio.Event()
    async def make():
        await gate.wait()
        return b"v"
    t1 = asyncio.create_task(c.get_or_create(k, make))
    t2 = asyncio.create_task(c.get_or_create(k, make))
    await asyncio.sleep(0.02)
    t1.cancel()
    gate.set()
    assert await t2 == b"v"
    with pytest.raises(asyncio.CancelledError):
        await t1


async def test_unexpected_engine_exception_becomes_typed_error(tmp_path):
    c = _mk(tmp_path)
    async def make():
        raise RuntimeError("engine blew up with secret text")
    with pytest.raises(TtsError) as ei:
        await c.get_or_create(c.key("b", "e", 1.0), make)
    assert "secret text" not in str(ei.value)


async def test_store_failure_after_synthesis_returns_bytes_and_warns(tmp_path, monkeypatch, caplog):
    c = _mk(tmp_path)
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    async def make():
        return b"SECRETAUDIO"
    with caplog.at_level("WARNING"):
        assert await c.get_or_create(c.key("h", "e", 1.0), make) == b"SECRETAUDIO"
    assert any("store" in r.getMessage() for r in caplog.records)
    assert _files(c) == []


async def test_read_side_failure_raises_cache_error(tmp_path, monkeypatch):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.put(k, b"x")
    real = Path.read_bytes
    def bad(self):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(Path, "read_bytes", bad)
    async def make():
        return b"y"
    with pytest.raises(CacheError):
        await c.get_or_create(k, make)
    monkeypatch.setattr(Path, "read_bytes", real)


async def test_factory_value_error_is_sanitized(tmp_path):
    c = _mk(tmp_path)
    async def make():
        raise ValueError("bad text: 비밀 문장")
    with pytest.raises(TtsError) as ei:
        await c.get_or_create(c.key("h", "e", 1.0), make)
    assert "비밀" not in str(ei.value) and "ValueError" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


async def test_factory_returning_non_bytes_is_typed(tmp_path):
    c = _mk(tmp_path)
    async def make():
        return "text"
    with pytest.raises(TtsError):
        await c.get_or_create(c.key("b", "e", 1.0), make)


def test_invalidation_by_key_change_and_explicit(tmp_path):
    c = _mk(tmp_path)
    k1 = c.key("h1", "windows", 1.0)
    c.put(k1, b"a")
    # script changed / engine or voice changed / synth speed changed -> different key -> miss
    for k in (c.key("h2", "windows", 1.0), c.key("h1", "windows", 1.2), c.key("h1", "none", 1.0)):
        assert c.get(k) is None
    assert c.invalidate(k1) is True and c.invalidate(k1) is False
    assert c.get(k1) is None
    k2 = c.key("b9", "e", 1.0)
    c.put(k1, b"a"); c.put(k2, b"b")
    assert c.clear() == 2 and _files(c) == []


def test_defaults_are_d14():
    c = AudioCache(Path("/x"))
    assert c.max_files == 30 and c.max_age_h == 48


def test_bad_limits_rejected():
    for kw in ({"max_files": 0}, {"max_age_h": 0}, {"max_files": -1}):
        with pytest.raises(ValueError):
            AudioCache(Path("/x"), **kw)


def test_cache_module_has_no_network_or_private_imports():
    import ast
    src = Path(cache_mod.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for m in mods:
            assert m.split(".")[0] not in {"httpx", "requests", "urllib", "socket", "boto3", "openai"}
            assert not m.startswith("pa.private") and "counselor" not in m


# ---- fix round 1 ----------------------------------------------------------
@pytest.mark.parametrize("payload", [b"a\nb", b"a\r\nb", b"x\x1ay", b"\x00\x00z\x00", b"\n\r\n\x1a\x00" * 50])
def test_binary_roundtrip_with_newlines_ctrl_z_nul(tmp_path, payload):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.put(k, payload)
    assert c.get(k) == payload


@pytest.mark.skipif(os.name == "nt", reason="Windows 의 실제 O_BINARY 를 가짜 플래그와 구분할 수 없다(리눅스에서 검증)")
def test_put_opens_files_in_binary_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    seen = []
    real = os.open
    def spy(path, flags, *a, **kw):
        seen.append(flags)
        return real(path, flags & ~0x8000, *a, **kw)      # keep the fake flag away from the real OS
    monkeypatch.setattr(os, "open", spy)
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.put(k, b"a\r\nb")
    assert seen and all(f & 0x8000 for f in seen)
    assert c.get(k) == b"a\r\nb"


def test_put_without_o_binary_attribute_still_works(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_BINARY", raising=False)
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.put(k, b"x")
    assert c.get(k) == b"x"


def test_synth_speed_is_constant_and_key_ignores_user_rate():
    from pa.voice.tts import synth_speed
    assert synth_speed() == 1.0
    c = AudioCache(Path("/nonexistent-never-created"))
    assert c.key("h", "e") == c.key("h", "e", synth_speed())


def test_engine_id_composition():
    from pa.voice.cache import engine_id
    assert engine_id(FakeTts()) == "fake:fake-ko"


def test_get_treats_expired_entry_as_miss_with_injected_clock(tmp_path):
    now = [NOW]
    c = AudioCache(tmp_path / "vc", clock=lambda: now[0])
    k = c.key("h", "e", 1.0)
    c.put(k, b"x")
    t = NOW.timestamp()
    os.utime(c.dir / f"{k}.audio", (t, t))
    assert c.get(k) == b"x"
    now[0] = NOW + timedelta(hours=48)                  # exactly at the limit: still served
    assert c.get(k) == b"x"
    now[0] = NOW + timedelta(hours=48, seconds=1)
    assert c.get(k) is None                              # idle system: never serve expired audio
    assert not (c.dir / f"{k}.audio").exists()


def test_purge_callable_at_startup_on_existing_dir(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.put(k, b"x")
    _age(c.dir / f"{k}.audio", 100)
    fresh = AudioCache(c.dir, clock=lambda: NOW)         # a "restarted" process
    assert fresh.purge() == 1 and _files(fresh) == []


def test_put_replaces_symlink_on_final_name(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    c.dir.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"orig")
    try:
        (c.dir / f"{k}.audio").symlink_to(outside)
    except OSError:
        pytest.skip("no symlinks")
    c.put(k, b"new")
    assert not (c.dir / f"{k}.audio").is_symlink()
    assert c.get(k) == b"new" and outside.read_bytes() == b"orig"


def test_put_fails_clearly_when_directory_occupies_final_name(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    (c.dir / f"{k}.audio").mkdir(parents=True)
    with pytest.raises(CacheError):
        c.put(k, b"x")
    assert c.get(k) is None
    assert [n for n in os.listdir(c.dir) if n.startswith(".tmp-")] == []


async def test_all_waiters_cancelled_cancels_synthesis_and_cleans_inflight(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def make():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return b"x"
    t1 = asyncio.create_task(c.get_or_create(k, make))
    t2 = asyncio.create_task(c.get_or_create(k, make))
    await started.wait()
    t1.cancel()
    await asyncio.sleep(0.02)
    assert not cancelled.is_set() and k in c._inflight       # one waiter left: keep going
    t2.cancel()
    await asyncio.sleep(0.05)
    assert cancelled.is_set() and c._inflight == {} and c._waiters == {}
    async def good():
        return b"ok"
    assert await c.get_or_create(k, good) == b"ok"


async def test_new_caller_does_not_get_stale_failure_from_finished_task(tmp_path):
    c = _mk(tmp_path)
    k = c.key("h", "e", 1.0)
    async def bad():
        raise TtsUnavailable("no engine")
    stale = asyncio.ensure_future(c._produce(k, bad))
    c._inflight[k] = stale                                    # finished but not yet removed by its callback
    await asyncio.wait([stale])
    assert stale.done() and k in c._inflight
    async def good():
        return b"fresh"
    assert await c.get_or_create(k, good) == b"fresh"
    stale.exception()
