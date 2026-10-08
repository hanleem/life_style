"""문제해결 점검: 서버·Tailscale·Codex 로그인·설정을 쉬운 말로 알려 준다."""
import json
import os
import sys

import pytest

from tools import doctor


class Runner:
    """명령 이름으로 (종료코드, 출력)을 돌려주는 가짜."""
    def __init__(self, **table):
        self.table, self.calls = table, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        key = " ".join(cmd[:3]) if cmd[0] != "schtasks" else "schtasks"
        for k, v in self.table.items():
            if key.startswith(k.replace("_", " ")):
                return v
        return (127, "")


TS = "." + "ts" + "." + "net"
GOOD_STATUS = json.dumps({"BackendState": "Running", "Self": {"DNSName": "pc.example" + TS + ".", "UserID": 1},
                          "User": {"1": {"LoginName": "mom@example.com"}}})


def _good_runner():
    return Runner(**{"codex login status": (0, "Logged in using ChatGPT"), "tailscale status --json": (0, GOOD_STATUS),
                     "tailscale serve status": (0, "https://pc.example" + TS + " (tailnet only)\n|-- / proxy http://127.0.0.1:8010"),
                     "schtasks": (0, "가족비서")})


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "family_profile.yaml").write_text("honorific: x\n", encoding="utf-8")
    (tmp_path / ".env").write_text("\n".join([
        "PA_SESSION_SECRET=" + "s" * 40, "PA_BACKUP_KEY=" + "k" * 44, "PA_ALLOWED_HOSTS=localhost,127.0.0.1,pc.example" + TS,
        "PA_ALLOWED_TAILSCALE_LOGIN=mom@example.com", "PA_DATA_DIR=./data"]) + "\n", encoding="utf-8")
    return tmp_path


def _by_name(checks):
    return {c.name: c for c in checks}


def test_everything_ok(project):
    checks = doctor.run_checks(project, run=_good_runner(), http=lambda url: 200, free_bytes=lambda p: 50 * 2 ** 30, windows=True)
    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]
    assert doctor.summarize(checks) == "모두 잘 되고 있어요."


def test_server_down_has_an_easy_fix(project):
    def down(url):
        raise OSError("refused")
    c = _by_name(doctor.run_checks(project, run=_good_runner(), http=down, free_bytes=lambda p: 2 ** 40, windows=True))["서버"]
    assert not c.ok and "바탕화면" in c.fix and "가족비서" in c.fix


def test_server_unhealthy_status_counts_as_not_ok(project):
    c = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda url: 500, free_bytes=lambda p: 2 ** 40, windows=True))["서버"]
    assert not c.ok


def test_codex_logged_out(project):
    r = _good_runner()
    r.table["codex login status"] = (1, "Not logged in")
    c = _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))["AI(Codex) 로그인"]
    assert not c.ok and "codex login" in c.fix and "ChatGPT" in c.fix


def test_codex_missing_program(project):
    r = _good_runner()
    del r.table["codex login status"]
    c = _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))["AI(Codex) 로그인"]
    assert not c.ok and "설치하기" in c.fix


def test_tailscale_not_running_or_logged_out(project):
    for out in ((0, json.dumps({"BackendState": "NeedsLogin"})), (1, ""), (0, "not json")):
        r = _good_runner()
        r.table["tailscale status --json"] = out
        by = _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))
        assert not by["Tailscale"].ok and "Tailscale" in by["Tailscale"].fix
        assert not by["휴대폰 접속 주소"].ok          # 로그인 전에는 주소도 점검할 수 없다


def test_serve_not_pointing_at_the_app(project):
    r = _good_runner()
    r.table["tailscale serve status"] = (0, "No serve config")
    c = _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))["휴대폰 접속 주소"]
    assert not c.ok and "tailscale serve" in c.fix


def test_env_problems_are_named_without_leaking_values(project):
    (project / ".env").write_text("PA_SESSION_SECRET=short\n", encoding="utf-8")
    by = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))
    assert not by["설정 파일(.env)"].ok and "short" not in by["설정 파일(.env)"].detail + by["설정 파일(.env)"].fix
    assert not by["휴대폰 접속 설정"].ok


def test_missing_env_file(project):
    (project / ".env").unlink()
    by = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))
    assert not by["설정 파일(.env)"].ok and "설치하기" in by["설정 파일(.env)"].fix


def test_interview_not_done_is_a_gentle_notice(project):
    (project / "data" / "family_profile.yaml").unlink()
    c = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))["첫 인터뷰"]
    assert not c.ok and "localhost:8010" in c.fix


def test_scheduled_task_missing_and_skipped_off_windows(project):
    r = _good_runner()
    r.table["schtasks"] = (1, "")
    by = _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))
    assert not by["자동 실행"].ok and "설치하기" in by["자동 실행"].fix
    assert "자동 실행" not in _by_name(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=False))


def test_low_disk_space_is_flagged(project):
    c = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda u: 200, free_bytes=lambda p: 100 * 2 ** 20, windows=True))["저장 공간"]
    assert not c.ok and "공간" in c.fix


def test_backup_key_missing_is_only_a_warning_level_problem(project):
    (project / ".env").write_text((project / ".env").read_text(encoding="utf-8").replace("PA_BACKUP_KEY=" + "k" * 44, "PA_BACKUP_KEY="), encoding="utf-8")
    c = _by_name(doctor.run_checks(project, run=_good_runner(), http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))["자동 백업"]
    assert not c.ok and c.level == "warn"


def test_summary_counts_problems_in_easy_words(project):
    r = _good_runner()
    r.table["codex login status"] = (1, "")
    checks = doctor.run_checks(project, run=r, http=lambda u: 500, free_bytes=lambda p: 2 ** 40, windows=True)
    assert doctor.summarize(checks) == "확인이 필요한 곳이 2군데 있어요."


def test_report_text_has_marks_and_fixes_and_no_secrets(project):
    r = _good_runner()
    r.table["codex login status"] = (1, "")
    text = doctor.report(doctor.run_checks(project, run=r, http=lambda u: 200, free_bytes=lambda p: 2 ** 40, windows=True))
    assert "✓" in text and "✗" in text and "해결:" in text
    assert "s" * 40 not in text and "k" * 44 not in text


def test_find_listener_pid_parses_netstat():
    out = ("  TCP    0.0.0.0:135     0.0.0.0:0      LISTENING       1000\n"
           "  TCP    127.0.0.1:8010  0.0.0.0:0      LISTENING       4242\n"
           "  TCP    127.0.0.1:80100 0.0.0.0:0      LISTENING       5555\n"
           "  TCP    127.0.0.1:8010  127.0.0.1:5000 ESTABLISHED     4242\n")
    assert doctor.find_listener_pid(8010, lambda cmd: (0, out)) == 4242
    assert doctor.find_listener_pid(9999, lambda cmd: (0, out)) is None
    assert doctor.find_listener_pid(8010, lambda cmd: (1, "")) is None


def test_restart_kills_only_the_listener_and_reports(project):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[0] == "netstat":
            return 0, "  TCP    127.0.0.1:8010  0.0.0.0:0      LISTENING       4242\n"
        return 0, ""
    assert doctor.restart_server(run) == "restarted"
    assert ["taskkill", "/F", "/PID", "4242"] in calls and all(c[0] in ("netstat", "taskkill") for c in calls)
    assert doctor.restart_server(lambda cmd: (0, "")) == "not_running"


def test_run_helper_never_raises_for_missing_programs():
    assert doctor._run(["definitely-not-a-real-program-xyz", "--version"]) == (127, "")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 의 .cmd 실행 방식 확인")
def test_run_finds_cmd_shims_by_their_plain_name(tmp_path, monkeypatch):
    (tmp_path / "fakeprog.cmd").write_text("@echo hello-from-shim\r\n", encoding="utf-8", newline="")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    rc, out = doctor._run(["fakeprog"])
    assert rc == 0 and "hello-from-shim" in out


def _patch_checks(monkeypatch, ok=True):
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [doctor.Check("서버", ok, "x", "" if ok else "고쳐 주세요")])


def test_interactive_mode_asks_and_restarts_on_yes(monkeypatch, capsys):
    _patch_checks(monkeypatch, ok=False)
    called = []
    monkeypatch.setattr(doctor, "restart_server", lambda *a, **k: called.append(1) or "restarted")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert doctor.main(["--interactive"]) == 1
    out = capsys.readouterr().out
    assert called == [1] and "서버를 다시 시작할까요" not in out and "다시 켜져요" in out


def test_interactive_mode_does_nothing_on_no_or_eof(monkeypatch):
    _patch_checks(monkeypatch)
    monkeypatch.setattr(doctor, "restart_server", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not restart")))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert doctor.main(["--interactive"]) == 0

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert doctor.main(["--interactive"]) == 0
