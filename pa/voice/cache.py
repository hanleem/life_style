"""Audio cache for synthesized briefing speech.

- Key = sha256 over length-prefixed (script_hash, engine_id, synth_speed): file names are hash hex only,
  so no user/briefing string ever reaches the file system. script_hash covers the content, engine_id is
  `f"{name}:{voice_id}"` (see tts.engine_id), synth_speed is the constant 1.0 (tts.synth_speed):
  the user's playback rate is applied client-side and never varies the key.
- Stores audio bytes only (never the script text). Each file carries a magic + sha256 of the payload so
  a damaged file reads as a miss and is regenerated. Files are always opened/read in binary mode
  (O_BINARY on Windows) so newline translation cannot corrupt audio.
- Atomic writes (temp file + os.replace), owner-only permissions best effort: on Windows chmod/mode bits
  are largely ignored and the folder inherits the ACL of the data directory.
- Retention: max_files / max_age_h, by modification time; injectable clock. `get` treats an entry older
  than max_age_h as a miss; `purge()` may also be called at startup.
- Single-flight: concurrent get_or_create() calls for one key synthesize once. Per PROCESS only (asyncio
  tasks); two processes may both synthesize, the atomic replace keeps the file consistent.
- Errors: only TtsError (synthesis) and CacheError (cache read side) leave get_or_create; messages never
  contain script text. A failure to STORE audio that was synthesized successfully is logged (no text)
  and the bytes are returned anyway.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from pa.voice.tts import TtsError, check_speed, engine_id, synth_speed  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

MAGIC = b"PAV1"
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_FILE_RE = re.compile(r"^[0-9a-f]{32}\.audio$")
_TMP_RE = re.compile(r"^\.tmp-[0-9a-f]{16}$")


class CacheError(Exception):
    """Cache storage failed (disk full, permission denied, ...). Never carries text content."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _check_key(key) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("잘못된 캐시 키입니다")
    return key


class AudioCache:
    def __init__(self, dir: Path, max_files: int = 30, max_age_h: int = 48,
                 clock: Callable[[], datetime] = _now_utc):
        if max_files < 1 or max_age_h <= 0:
            raise ValueError("보관 한도는 1 이상이어야 합니다")
        self.dir = Path(dir)
        self.max_files = max_files
        self.max_age_h = max_age_h
        self._clock = clock
        self._inflight: dict[str, asyncio.Task] = {}
        self._waiters: dict[str, int] = {}

    # ---- keys -------------------------------------------------------------
    def key(self, script_hash: str, engine_id: str, synth_speed: float = 1.0) -> str:
        sp = check_speed(synth_speed)
        parts = [str(script_hash), str(engine_id), f"{sp:.3f}"]
        material = "\x1f".join(f"{len(p)}:{p}" for p in parts)       # length-prefixed: no ambiguity
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.dir / f"{_check_key(key)}.audio"

    # ---- storage ----------------------------------------------------------
    def _ensure_dir(self) -> None:
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass   # e.g. Windows / foreign filesystem: best effort
        except OSError as e:
            raise CacheError(f"음성 캐시 폴더를 만들 수 없습니다 ({e.strerror or type(e).__name__})") from None

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            if p.is_symlink() or not p.is_file():
                return None
            if self._expired(p):
                self._unlink(p)
                return None
            raw = p.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            if isinstance(e, (NotADirectoryError, PermissionError)) and not self.dir.exists():
                return None
            raise CacheError(f"음성 캐시를 읽을 수 없습니다 ({e.strerror or type(e).__name__})") from None
        payload = raw[len(MAGIC) + 32:]
        if (len(raw) <= len(MAGIC) + 32 or not raw.startswith(MAGIC)
                or hashlib.sha256(payload).digest() != raw[len(MAGIC):len(MAGIC) + 32]):
            log.warning("voice cache: corrupted entry dropped")
            self._unlink(p)
            return None
        return payload

    def _expired(self, p: Path) -> bool:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp() - p.stat().st_mtime > self.max_age_h * 3600

    def put(self, key: str, data: bytes) -> None:
        final = self._path(key)
        if not isinstance(data, bytes) or not data:
            raise ValueError("오디오 데이터는 비어 있지 않은 bytes 여야 합니다")
        self._ensure_dir()
        blob = MAGIC + hashlib.sha256(data).digest() + data
        tmp = self.dir / f".tmp-{secrets.token_hex(8)}"
        try:
            if final.is_symlink():
                final.unlink()
            elif final.is_dir():
                raise CacheError("음성 캐시 파일 이름을 폴더가 차지하고 있습니다")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            fd = os.open(tmp, flags, 0o600)
            try:
                view = memoryview(blob)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
            os.replace(tmp, final)
        except CacheError:
            self._unlink(tmp)
            raise
        except OSError as e:
            self._unlink(tmp)
            raise CacheError(f"음성 캐시에 쓸 수 없습니다 ({e.strerror or type(e).__name__})") from None
        try:
            self.purge(keep=final.name)
        except OSError:
            pass   # retention is best effort; the write itself succeeded

    def invalidate(self, key: str) -> bool:
        p = self._path(key)
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            raise CacheError(f"음성 캐시를 지울 수 없습니다 ({e.strerror or type(e).__name__})") from None

    def clear(self) -> int:
        n = 0
        for p in self._entries():
            if self._unlink(p):
                n += 1
        return n

    # ---- retention --------------------------------------------------------
    @staticmethod
    def _unlink(p: Path) -> bool:
        try:
            p.unlink()
            return True
        except OSError:
            return False

    def _entries(self):
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        out = []
        for n in names:
            if _FILE_RE.fullmatch(n):
                p = self.dir / n
                if not p.is_symlink() and p.is_file():
                    out.append(p)
        return out

    def purge(self, now: datetime | None = None, *, keep: str | None = None) -> int:
        """Delete entries older than max_age_h, then the oldest beyond max_files. Returns the number removed.
        `now` defaults to the injected clock. Foreign files and symlinks are never touched."""
        now = now or self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        limit = self.max_age_h * 3600
        t_now = now.timestamp()
        removed = 0
        # stale temp files left by a crash
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for n in names:
            if _TMP_RE.fullmatch(n):
                p = self.dir / n
                try:
                    if t_now - p.stat().st_mtime > limit and self._unlink(p):
                        removed += 1
                except OSError:
                    pass
        live = []
        for p in self._entries():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if p.name != keep and t_now - m > limit:
                if self._unlink(p):
                    removed += 1
            else:
                live.append((m, p))
        if len(live) > self.max_files:
            others = sorted((x for x in live if x[1].name != keep), key=lambda x: (x[0], x[1].name))
            for _, p in others[: len(live) - self.max_files]:
                if self._unlink(p):
                    removed += 1
        return removed

    # ---- single-flight ----------------------------------------------------
    async def get_or_create(self, key: str, factory: Callable[[], Awaitable[bytes]]) -> bytes:
        """Cached audio for `key`, else produce it once (shared by concurrent callers) and cache it.

        Raises only TtsError (synthesis failed, incl. any other exception from the factory, reported by
        type name only) or CacheError (cache could not be READ). If synthesis succeeded but the result
        could not be stored, the bytes are returned and a warning without text is logged.
        If every waiter is cancelled, the synthesis is cancelled too."""
        data = self.get(_check_key(key))
        if data is not None:
            return data
        task = self._inflight.get(key)
        if task is None or task.done():          # a finished task may be a stale failure: start fresh
            task = asyncio.ensure_future(self._produce(key, factory))
            self._inflight[key] = task

            def _done(t: asyncio.Task, key=key):
                if self._inflight.get(key) is t:
                    del self._inflight[key]
                if not t.cancelled():
                    t.exception()      # mark retrieved even if every waiter was cancelled

            task.add_done_callback(_done)
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done() and self._waiters.get(key, 0) <= 1:
                task.cancel()
                if self._inflight.get(key) is task:
                    del self._inflight[key]
            raise
        finally:
            n = self._waiters.get(key, 1) - 1
            if n <= 0:
                self._waiters.pop(key, None)
            else:
                self._waiters[key] = n

    async def _produce(self, key: str, factory: Callable[[], Awaitable[bytes]]) -> bytes:
        data = self.get(key)
        if data is not None:
            return data
        try:
            data = await factory()
        except TtsError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:      # incl. ValueError / CacheError raised by an engine factory
            raise TtsError(f"음성 합성에 실패했습니다 ({type(e).__name__})") from None
        if not isinstance(data, bytes) or not data:
            raise TtsError("음성 엔진이 오디오를 돌려주지 않았습니다")
        try:
            self.put(key, data)
        except CacheError:
            log.warning("voice cache: store failed, serving uncached audio")
        return data
