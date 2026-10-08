import json
from cryptography.fernet import Fernet
from pa.integrations.google_auth import SCOPES, FORBIDDEN_SCOPES, TokenStore


def test_roundtrip_encrypted(tmp_path):
    k = Fernet.generate_key().decode()
    s = TokenStore(tmp_path / "t.enc", k)
    s.save({"token": "abc", "refresh_token": "r", "scopes": SCOPES["base"]})
    raw = (tmp_path / "t.enc").read_bytes()
    assert b"abc" not in raw and b"refresh" not in raw
    assert s.load()["refresh_token"] == "r"


def test_wrong_key_returns_none(tmp_path):
    TokenStore(tmp_path / "t.enc", Fernet.generate_key().decode()).save({"token": "x"})
    assert TokenStore(tmp_path / "t.enc", Fernet.generate_key().decode()).load() is None


def test_disabled_without_key(tmp_path):
    s = TokenStore(tmp_path / "t.enc", "")
    assert s.enabled is False and s.load() is None


def test_no_forbidden_scope_requested():
    all_scopes = {x for v in SCOPES.values() for x in v}
    assert not (all_scopes & FORBIDDEN_SCOPES)
    assert "https://www.googleapis.com/auth/gmail.send" not in all_scopes
    assert "https://www.googleapis.com/auth/gmail.modify" not in all_scopes


def test_clear_and_settings(tmp_path):
    from pa.settings import Settings
    s = TokenStore(tmp_path / "t.enc", Fernet.generate_key().decode())
    s.save({"token": "x"})
    s.clear()
    assert s.load() is None
    st = Settings(data_dir=tmp_path)
    assert st.google_token_path == tmp_path / "google_token.enc"
    assert st.google_token_key == ""
