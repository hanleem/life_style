"""설치 도우미: .env 만들기·채우기, Tailscale 정보 해석. 비밀값은 어디에도 출력하지 않는다."""
import json
from pathlib import Path

import pytest

from tools import setup_env

TS = "." + "ts" + "." + "net"                       # 저장소에 실제 tailnet 주소처럼 보이는 글자를 두지 않는다(privacy_check)
DNS = "pc.tailnet-example" + TS
EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _status(login="mom@example.com", dns=DNS + ".", state="Running", uid=111):
    return {"BackendState": state, "Self": {"DNSName": dns, "UserID": uid}, "User": {str(uid): {"LoginName": login}}}


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _env(project) -> dict:
    out = {}
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_set_value_replaces_in_place_and_keeps_comments_and_order():
    text = "# 설명\nA=1\n# B=주석 처리된 줄\nB=2\n"
    out = setup_env.set_value(text, "B", "9")
    assert out == "# 설명\nA=1\n# B=주석 처리된 줄\nB=9\n"
    assert setup_env.set_value(text, "C", "3").endswith("B=2\nC=3\n")
    assert setup_env.get_value(text, "A") == "1" and setup_env.get_value(text, "Z") is None


def test_set_value_handles_missing_trailing_newline_and_crlf():
    assert setup_env.set_value("A=1", "B", "2") == "A=1\nB=2\n"
    assert setup_env.set_value("A=1\r\nB=2\r\n", "A", "5") == "A=5\r\nB=2\r\n"


def test_ensure_env_creates_from_the_example_and_fills_secrets(project):
    summary = setup_env.ensure_env(project, which=lambda n: None)
    env = _env(project)
    assert summary["created"] is True
    assert len(env["PA_SESSION_SECRET"]) >= 32 and len(env["PA_BACKUP_KEY"]) >= 32 and env["PA_GOOGLE_TOKEN_KEY"]
    assert env["PA_DEV_SKIP_TAILSCALE"] == "false" and env["PA_DATA_DIR"] == "./data"
    assert {"PA_SESSION_SECRET", "PA_BACKUP_KEY", "PA_GOOGLE_TOKEN_KEY"} <= set(summary["filled"])


def test_backup_key_is_a_valid_fernet_key(project):
    from cryptography.fernet import Fernet
    setup_env.ensure_env(project, which=lambda n: None)
    Fernet(_env(project)["PA_BACKUP_KEY"].encode())            # 올바른 키가 아니면 여기서 오류


def test_existing_values_are_never_overwritten(project):
    (project / ".env").write_text("PA_SESSION_SECRET=" + "k" * 40 + "\nPA_BACKUP_KEY=keep-me\nPA_APPROVAL_TTL_MINUTES=45\n", encoding="utf-8")
    summary = setup_env.ensure_env(project, which=lambda n: None)
    env = _env(project)
    assert summary["created"] is False
    assert env["PA_SESSION_SECRET"] == "k" * 40 and env["PA_BACKUP_KEY"] == "keep-me" and env["PA_APPROVAL_TTL_MINUTES"] == "45"
    assert "PA_BACKUP_KEY" not in summary["filled"]


def test_too_short_session_secret_is_replaced(project):
    (project / ".env").write_text("PA_SESSION_SECRET=short\n", encoding="utf-8")
    setup_env.ensure_env(project, which=lambda n: None)
    assert len(_env(project)["PA_SESSION_SECRET"]) >= 32


def test_running_twice_changes_nothing_the_second_time(project):
    setup_env.ensure_env(project, which=lambda n: None)
    first = (project / ".env").read_text(encoding="utf-8")
    again = setup_env.ensure_env(project, which=lambda n: None)
    assert (project / ".env").read_text(encoding="utf-8") == first and again["filled"] == []


def test_codex_path_is_detected_and_stored(project, tmp_path):
    fake = tmp_path / "codex.cmd"
    fake.write_text("@echo off")
    summary = setup_env.ensure_env(project, which=lambda n: str(fake) if n == "codex" else None)
    assert _env(project)["PA_CODEX_BIN"] == str(fake) and summary["codex_found"] is True


def test_missing_codex_is_reported_not_invented(project):
    summary = setup_env.ensure_env(project, which=lambda n: None)
    assert summary["codex_found"] is False and _env(project)["PA_CODEX_BIN"] == "codex"


def test_summary_never_contains_secret_values(project):
    summary = setup_env.ensure_env(project, which=lambda n: None)
    blob = json.dumps(summary, ensure_ascii=False)
    for v in _env(project).values():
        if len(v) >= 20:
            assert v not in blob


def test_tailnet_info_reads_dns_name_and_login():
    assert setup_env.tailnet_info(_status()) == (DNS, "mom@example.com")


@pytest.mark.parametrize("bad", [None, {}, _status(state="NeedsLogin"), _status(dns=""), {"BackendState": "Running", "Self": {}},
                                 _status(login="")])
def test_tailnet_info_is_none_until_logged_in(bad):
    assert setup_env.tailnet_info(bad) is None


def test_apply_tailnet_sets_hosts_public_url_and_login(project):
    setup_env.ensure_env(project, which=lambda n: None)
    summary = setup_env.apply_tailnet(project, _status())
    env = _env(project)
    assert env["PA_ALLOWED_HOSTS"] == f"localhost,127.0.0.1,{DNS}"
    assert env["PA_PUBLIC_URL"] == f"https://{DNS}" and env["PA_ALLOWED_TAILSCALE_LOGIN"] == "mom@example.com"
    assert summary["ok"] is True and summary["host"] == DNS


def test_apply_tailnet_keeps_other_family_logins_and_hosts(project):
    setup_env.ensure_env(project, which=lambda n: None)
    (project / ".env").write_text(setup_env.set_value(setup_env.set_value((project / ".env").read_text(encoding="utf-8"),
                                  "PA_ALLOWED_TAILSCALE_LOGIN", "dad@example.com"), "PA_ALLOWED_HOSTS", "localhost,other.example.test"), encoding="utf-8")
    setup_env.apply_tailnet(project, _status())
    env = _env(project)
    assert env["PA_ALLOWED_TAILSCALE_LOGIN"].split(",") == ["dad@example.com", "mom@example.com"]
    assert env["PA_ALLOWED_HOSTS"].split(",") == ["localhost", "other.example.test", "127.0.0.1", DNS]
    setup_env.apply_tailnet(project, _status())                  # 두 번 해도 중복되지 않는다
    assert _env(project)["PA_ALLOWED_TAILSCALE_LOGIN"].count("mom@example.com") == 1


def test_apply_tailnet_without_login_changes_nothing(project):
    setup_env.ensure_env(project, which=lambda n: None)
    before = (project / ".env").read_text(encoding="utf-8")
    summary = setup_env.apply_tailnet(project, _status(state="NeedsLogin"))
    assert summary["ok"] is False and (project / ".env").read_text(encoding="utf-8") == before


def test_cli_prints_no_secrets(project, capsys, monkeypatch):
    monkeypatch.setattr(setup_env, "ROOT", project)
    assert setup_env.main([]) == 0
    out = capsys.readouterr().out
    for v in _env(project).values():
        if len(v) >= 20:
            assert v not in out
    assert "PA_SESSION_SECRET" in out                            # 키 이름은 보여 줘도 값은 보여 주지 않는다
