"""설치·업데이트·문제해결 스크립트와 안내서가 약속한 모양인지 확인한다(스크립트를 실제로 실행하지는 않는다)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATS = ["설치하기.bat", "업데이트.bat", "문제해결.bat"]
PS1 = ["install.ps1", "update.ps1"]
BOM = b"\xef\xbb\xbf"


def _text(name: str) -> str:
    return (ROOT / name).read_bytes().decode("utf-8-sig")


@pytest.mark.parametrize("name", BATS + PS1 + ["시작하기.md", ".gitattributes"])
def test_files_exist(name):
    assert (ROOT / name).is_file()


@pytest.mark.parametrize("name", BATS)
def test_bat_files_are_ascii_with_crlf(name):
    raw = (ROOT / name).read_bytes()
    raw.decode("ascii")                                   # 한글은 UTF-8/콘솔 설정에 따라 깨질 수 있어 .bat 에는 넣지 않는다
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert not raw.startswith(BOM)


@pytest.mark.parametrize("name", PS1)
def test_ps1_files_have_a_bom_and_crlf(name):
    raw = (ROOT / name).read_bytes()
    assert raw.startswith(BOM), "Windows PowerShell 5.1 은 BOM 이 없으면 한글을 깨뜨린다"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell 없음")
@pytest.mark.parametrize("name", PS1)
def test_ps1_files_parse_without_errors(name):
    cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
           f"(Resolve-Path '{ROOT / name}'),[ref]$t,[ref]$e);$e.Count")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)
    assert r.stdout.decode().strip() == "0", r.stdout + r.stderr


def test_bat_files_run_the_matching_script_without_changing_the_machine_policy():
    for bat, target in (("설치하기.bat", "install.ps1"), ("업데이트.bat", "update.ps1")):
        t = _text(bat)
        assert "-ExecutionPolicy Bypass" in t and target in t and "-NoProfile" in t and "pause" in t
        assert "Set-ExecutionPolicy" not in t
    doctor = _text("문제해결.bat")
    assert "tools.doctor --interactive" in doctor and "pause" in doctor


def test_install_script_has_every_step_from_the_design():
    t = _text("install.ps1")
    for needle in ("astral-sh.uv", "OpenJS.NodeJS.LTS", "Tailscale.Tailscale", "npm i -g @openai/codex", "uv sync",
                   "tools.setup_env", "tools.setup_env --tailnet", "tailscale up", "codex login",
                   "tailscale serve --bg --https=443", "http://127.0.0.1:$Port", "Register-ScheduledTask", "-AtLogOn",
                   "가족비서 열기", "run_pa.bat", "/welcome", "Read-Host"):
        assert needle in t, needle
    assert "$Port = 8010" in t


def test_install_script_never_handles_passwords_or_secrets():
    t = _text("install.ps1") + _text("update.ps1") + _text("문제해결.bat")
    for banned in ("SecureString", "Get-Credential", "-Password", "PA_SESSION_SECRET", "PA_BACKUP_KEY", "Set-ExecutionPolicy"):
        assert banned not in t, banned
    assert re.search(r"Read-Host\s+\"[^\"]*Enter", t)       # 키 입력 대기는 Enter 뿐
    assert "winget install --id $a.Id -e --silent" in t and "--accept-package-agreements" in t


def test_user_only_steps_are_login_in_the_browser():
    t = _text("install.ps1")
    assert "tailscale up" in t and "& codex login" in t
    assert t.index("tailscale up") < t.index("tailscale serve") and t.index("& codex login") < t.index("Register-ScheduledTask")
    assert "비밀번호는" in t                                   # 비밀번호를 대신 다루지 않는다는 안내


def test_install_script_is_safe_to_rerun():
    t = _text("install.ps1")
    assert t.count("Have ") >= 3 and "이미 있어요" in t and "-Force" in t and "Server-Up" in t   # 이미 된 단계는 건너뛴다


def test_update_script_pulls_syncs_and_restarts_without_touching_data():
    t = _text("update.ps1")
    assert "git pull --ff-only" in t and "uv sync" in t and "tools.doctor --restart" in t
    assert "Remove-Item" not in t and "git reset" not in t and "git clean" not in t


def test_scheduled_task_name_matches_the_doctor():
    from tools import doctor
    assert doctor.TASK_NAME == "가족비서" and f"$TaskName = '{doctor.TASK_NAME}'" in _text("install.ps1")
    assert doctor.PORT == 8010 and "$Port = 8010" in _text("install.ps1")


def test_start_guide_covers_the_whole_flow_in_easy_words_without_images():
    t = _text("시작하기.md")
    for needle in ("설치하기", "더블클릭", "Tailscale", "ChatGPT", "QR", "홈 화면에 추가", "알림 켜기", "문제해결", "업데이트",
                   "인터뷰", "Safari", "해제"):
        assert needle in t, needle
    assert "![" not in t and "<img" not in t.lower()            # 이미지는 저장소에 올리지 않는다(도식은 글자로)
    assert "비밀번호는 이 프로그램에 입력하지 않아요" in t


def test_start_guide_menu_names_match_the_app():
    from pa.web import routes_settings  # noqa: F401 - 화면 문구는 템플릿에서 확인
    settings = (ROOT / "pa/web/templates/settings.html").read_text(encoding="utf-8")
    for label in ("휴대폰 등록·기기 관리", "홈 화면에 추가·알림 켜기", "가정통신문 사진 올리기", "인터뷰 다시 하기"):
        assert label in _text("시작하기.md") and label in settings, label


def test_gitattributes_forces_crlf_for_windows_scripts():
    t = _text(".gitattributes")
    assert "*.bat text eol=crlf" in t and "*.ps1 text eol=crlf" in t
