import os
import struct

import pytest

from pa.backup import crypt
from pa.backup.crypt import BackupKeyError, BundleCorrupt, decrypt_file, encrypt_file, key_fingerprint
from pa.settings import Settings

KEY = crypt.generate_key()
KEY2 = crypt.generate_key()


def _mk(tmp_path, data=b"hello" * 100, name="p"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _records(blob):
    pos = crypt._HDR
    out = []
    while pos < len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        out.append(blob[pos:pos + 4 + ln])
        pos += 4 + ln
    return out


def test_roundtrip_multi_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(crypt, "CHUNK", 1024)
    data = bytes(range(256)) * 14  # 3.5 KiB
    src = _mk(tmp_path, data)
    info = encrypt_file(src, tmp_path / "e", KEY)
    blob = (tmp_path / "e").read_bytes()
    assert len(_records(blob)) == 4
    assert info.bytes_in == len(data) and info.bytes_out == len(blob)
    assert decrypt_file(tmp_path / "e", tmp_path / "d", KEY) == len(data)
    assert (tmp_path / "d").read_bytes() == data


def test_roundtrip_empty_file(tmp_path):
    src = _mk(tmp_path, b"")
    encrypt_file(src, tmp_path / "e", KEY)
    assert len(_records((tmp_path / "e").read_bytes())) == 1
    decrypt_file(tmp_path / "e", tmp_path / "d", KEY)
    assert (tmp_path / "d").read_bytes() == b""


def test_ciphertext_has_no_plaintext_marker(tmp_path):
    src = _mk(tmp_path, b"abc MARKER-XYZ def")
    encrypt_file(src, tmp_path / "e", KEY)
    assert b"MARKER-XYZ" not in (tmp_path / "e").read_bytes()


def test_wrong_key_raises_key_error_not_corrupt(tmp_path):
    encrypt_file(_mk(tmp_path), tmp_path / "e", KEY)
    with pytest.raises(BackupKeyError):
        decrypt_file(tmp_path / "e", tmp_path / "d", KEY2)
    assert not (tmp_path / "d").exists()


def test_fingerprint_mismatch_detected_before_decrypt(tmp_path):
    encrypt_file(_mk(tmp_path), tmp_path / "e", KEY)
    b = bytearray((tmp_path / "e").read_bytes())
    b[len(crypt.MAGIC)] ^= 0xFF
    (tmp_path / "e").write_bytes(bytes(b))
    with pytest.raises(BackupKeyError):
        decrypt_file(tmp_path / "e", tmp_path / "d", KEY)


def test_bad_magic_is_corrupt(tmp_path):
    (tmp_path / "e").write_bytes(b"NOTPABK" + b"x" * 40)
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "e", tmp_path / "d", KEY)


def _multi(tmp_path, monkeypatch):
    monkeypatch.setattr(crypt, "CHUNK", 64)
    encrypt_file(_mk(tmp_path, b"x" * 300), tmp_path / "e", KEY)
    return (tmp_path / "e").read_bytes()


def test_truncated_raises_corrupt(tmp_path, monkeypatch):
    blob = _multi(tmp_path, monkeypatch)
    recs = _records(blob)
    (tmp_path / "t").write_bytes(blob[:crypt._HDR] + b"".join(recs[:-1]))
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "t", tmp_path / "d", KEY)
    assert not (tmp_path / "d").exists()


def test_reordered_chunks_raise_corrupt(tmp_path, monkeypatch):
    blob = _multi(tmp_path, monkeypatch)
    recs = _records(blob)
    recs[1], recs[2] = recs[2], recs[1]
    (tmp_path / "t").write_bytes(blob[:crypt._HDR] + b"".join(recs))
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "t", tmp_path / "d", KEY)
    assert not (tmp_path / "d").exists()


def test_chunk_from_other_file_raises_corrupt(tmp_path, monkeypatch):
    blob = _multi(tmp_path, monkeypatch)
    encrypt_file(_mk(tmp_path, b"x" * 300, "p2"), tmp_path / "e2", KEY)
    other = _records((tmp_path / "e2").read_bytes())
    recs = _records(blob)
    recs[2] = other[2]
    (tmp_path / "t").write_bytes(blob[:crypt._HDR] + b"".join(recs))
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "t", tmp_path / "d", KEY)


def test_trailing_bytes_raise_corrupt(tmp_path):
    encrypt_file(_mk(tmp_path), tmp_path / "e", KEY)
    with open(tmp_path / "e", "ab") as f:
        f.write(b"\x00")
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "e", tmp_path / "d", KEY)


def test_bitflip_in_later_chunk_is_corrupt_not_key(tmp_path, monkeypatch):
    blob = _multi(tmp_path, monkeypatch)
    recs = _records(blob)
    b = bytearray(recs[2])
    b[-3] ^= 0x01
    recs[2] = bytes(b)
    (tmp_path / "t").write_bytes(blob[:crypt._HDR] + b"".join(recs))
    with pytest.raises(BundleCorrupt):
        decrypt_file(tmp_path / "t", tmp_path / "d", KEY)
    assert not (tmp_path / "d").exists()


def test_bitflip_in_first_chunk_raises(tmp_path):
    encrypt_file(_mk(tmp_path), tmp_path / "e", KEY)
    b = bytearray((tmp_path / "e").read_bytes())
    b[crypt._HDR + 4 + 60] ^= 0x01
    (tmp_path / "e").write_bytes(bytes(b))
    with pytest.raises((BundleCorrupt, BackupKeyError)):
        decrypt_file(tmp_path / "e", tmp_path / "d", KEY)


def test_dst_exists_refused(tmp_path):
    src = _mk(tmp_path)
    (tmp_path / "e").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        encrypt_file(src, tmp_path / "e", KEY)
    assert (tmp_path / "e").read_bytes() == b"old"
    encrypt_file(src, tmp_path / "e2", KEY)
    (tmp_path / "d").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        decrypt_file(tmp_path / "e2", tmp_path / "d", KEY)
    assert (tmp_path / "d").read_bytes() == b"keep"


def test_fingerprint_stable_and_not_key():
    assert key_fingerprint(KEY) == key_fingerprint(KEY)
    assert len(key_fingerprint(KEY)) == 16
    assert key_fingerprint(KEY) not in KEY and key_fingerprint(KEY) != key_fingerprint(KEY2)


def test_invalid_key_message_has_no_key(tmp_path):
    with pytest.raises(BackupKeyError) as e:
        encrypt_file(_mk(tmp_path), tmp_path / "e", "not-a-key")
    assert "not-a-key" not in str(e.value)
    assert not (tmp_path / "e").exists()


def test_bytes_roundtrip_and_limit():
    assert crypt.decrypt_bytes(crypt.encrypt_bytes(b"{}", KEY), KEY) == b"{}"
    with pytest.raises(ValueError):
        crypt.encrypt_bytes(b"x" * ((1 << 20) + 1), KEY)
    with pytest.raises(BackupKeyError):
        crypt.decrypt_bytes(crypt.encrypt_bytes(b"{}", KEY), KEY2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_ciphertext_is_0600(tmp_path):
    encrypt_file(_mk(tmp_path), tmp_path / "e", KEY)
    assert (tmp_path / "e").stat().st_mode & 0o777 == 0o600


def test_settings_backup_defaults(tmp_path):
    s = Settings(data_dir=tmp_path, _env_file=None)
    assert s.backup_root == tmp_path / "backups"
    assert s.backup_work_dir == tmp_path / ".backup-work"
    assert s.backup_include_google_token is False
    assert s.backup_key == "" and s.backup_max_mb == 1024
