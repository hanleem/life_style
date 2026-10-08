"""백업 암호 컨테이너 .pabk (버전 1).

헤더: MAGIC(6) + 키 지문 8바이트 + file_id 16바이트. 본문: [u32 길이][Fernet 토큰] 반복.
토큰 평문 = file_id(16) + idx(u32) + last(u8) + data(<= CHUNK).
"""
import hashlib
import io
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MAGIC = b"PABK1\n"
CHUNK = 1 << 20
MAX_BLOB = 1 << 20
_HDR = len(MAGIC) + 8 + 16


class BackupKeyError(Exception):
    pass


class BundleCorrupt(Exception):
    pass


@dataclass
class EncInfo:
    bytes_in: int
    bytes_out: int
    sha256: str


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(b"pa-backup-fp\x00" + key.encode()).hexdigest()[:16]


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except Exception:
        raise BackupKeyError("PA_BACKUP_KEY 형식이 올바르지 않습니다") from None


def _header(key: str, file_id: bytes) -> bytes:
    return MAGIC + bytes.fromhex(key_fingerprint(key))[:8] + file_id


def _record(f: Fernet, file_id: bytes, idx: int, last: bool, data: bytes) -> bytes:
    tok = f.encrypt(file_id + struct.pack(">IB", idx, 1 if last else 0) + data)
    return struct.pack(">I", len(tok)) + tok


def _chunks(fobj):
    """청크를 하나씩 읽되 마지막 청크를 알기 위해 한 청크 앞서 읽는다(메모리 상 최대 2 청크)."""
    cur = fobj.read(CHUNK)
    while True:
        nxt = fobj.read(CHUNK) if len(cur) == CHUNK else b""
        yield cur, not nxt
        if not nxt:
            return
        cur = nxt


def _open_excl(dst: Path):
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")


def encrypt_file(src: Path, dst: Path, key: str) -> EncInfo:
    f = _fernet(key)
    file_id = os.urandom(16)
    h = hashlib.sha256()
    n_in = n_out = 0
    out = _open_excl(Path(dst))
    ok = False
    try:
        with out, open(src, "rb") as fin:
            hd = _header(key, file_id)
            out.write(hd)
            h.update(hd)
            n_out += len(hd)
            for idx, (data, last) in enumerate(_chunks(fin)):
                rec = _record(f, file_id, idx, last, data)
                out.write(rec)
                h.update(rec)
                n_out += len(rec)
                n_in += len(data)
            out.flush()
            os.fsync(out.fileno())
        ok = True
    finally:
        if not ok:
            try:
                Path(dst).unlink()
            except OSError:
                pass
    return EncInfo(n_in, n_out, h.hexdigest())


def _decrypt_stream(fin, key: str):
    f = _fernet(key)
    hd = fin.read(_HDR)
    if len(hd) < _HDR or hd[:len(MAGIC)] != MAGIC:
        raise BundleCorrupt("format")
    fp = hd[len(MAGIC):len(MAGIC) + 8].hex()
    mine = key_fingerprint(key)
    if fp != mine[:16]:
        raise BackupKeyError(f"백업 키가 다릅니다 (파일 {fp[:8]} / 현재 {mine[:8]})")
    file_id = hd[len(MAGIC) + 8:]
    idx = 0
    while True:
        lb = fin.read(4)
        if len(lb) < 4:
            raise BundleCorrupt("truncated")
        (ln,) = struct.unpack(">I", lb)
        if ln > CHUNK * 2 + 4096:
            raise BundleCorrupt("length")
        tok = fin.read(ln)
        if len(tok) < ln:
            raise BundleCorrupt("truncated")
        try:
            plain = f.decrypt(tok)
        except InvalidToken:
            if idx == 0:
                raise BackupKeyError("백업 키로 열 수 없습니다") from None
            raise BundleCorrupt("token") from None
        if len(plain) < 21 or plain[:16] != file_id:
            raise BundleCorrupt("file_id")
        i, last = struct.unpack(">IB", plain[16:21])
        if i != idx:
            raise BundleCorrupt("order")
        yield plain[21:]
        idx += 1
        if last:
            if fin.read(1):
                raise BundleCorrupt("trailing")
            return


def decrypt_file(src: Path, dst: Path, key: str) -> int:
    n = 0
    out = None
    ok = False
    try:
        with open(src, "rb") as fin:
            gen = _decrypt_stream(fin, key)
            first = next(gen)   # 키·형식 오류는 dst 를 만들기 전에 드러난다
            out = _open_excl(Path(dst))
            out.write(first)
            n += len(first)
            for data in gen:
                out.write(data)
                n += len(data)
            out.flush()
            os.fsync(out.fileno())
        ok = True
    finally:
        if out is not None:
            out.close()
            if not ok:
                try:
                    Path(dst).unlink()
                except OSError:
                    pass
    return n


def encrypt_bytes(data: bytes, key: str) -> bytes:
    if len(data) > MAX_BLOB:
        raise ValueError("too large")
    f = _fernet(key)
    file_id = os.urandom(16)
    return _header(key, file_id) + _record(f, file_id, 0, True, data)


def decrypt_bytes(blob: bytes, key: str) -> bytes:
    return b"".join(_decrypt_stream(io.BytesIO(blob), key))
