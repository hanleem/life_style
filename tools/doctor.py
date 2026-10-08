"""문제해결 점검: 서버·Tailscale·Codex 로그인·설정을 쉬운 말로 알려 주고, 원하면 서버를 다시 시작한다.

    uv run python -m tools.doctor            # 점검 결과 보기
    uv run python -m tools.doctor --restart  # 서버 다시 시작(run_pa.bat 이 10초 안에 다시 켠다)
    uv run python -m tools.doctor --interactive  # 결과를 보고 나서 다시 시작할지 묻는다(문제해결.bat)

비밀값(.env 의 키)은 어디에도 출력하지 않는다. 비밀번호는 다루지 않는다.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tools.setup_env import get_value, tailnet_info

ROOT = Path(__file__).resolve().parents[1]
PORT = 8010
HEALTH = f"http://127.0.0.1:{PORT}/health"
TASK_NAME = "가족비서"
MIN_FREE = 1 * 2 ** 30


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    level: str = "error"      # error | warn


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    """명령을 실행해 (종료코드, 출력). 프로그램이 없거나 오래 걸려도 예외를 던지지 않는다(없으면 127)."""
    exe = shutil.which(cmd[0])   # Windows 는 확장자 없는 이름(codex)으로 codex.cmd 를 바로 실행하지 못한다 → 실제 경로로
    if exe:
        cmd = [exe] + cmd[1:]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _http(url: str) -> int:
    with urllib.request.urlopen(url, timeout=5) as r:   # noqa: S310 - 이 PC 의 루프백 주소만 쓴다
        return r.status


def _env_text(project: Path) -> str | None:
    p = project / ".env"
    return p.read_text(encoding="utf-8") if p.exists() else None


def run_checks(project: Path = ROOT, *, run=_run, http=_http, free_bytes=lambda p: shutil.disk_usage(p).free,
               windows: bool = sys.platform == "win32") -> list[Check]:
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, **k))
    env = _env_text(project)

    try:
        up = http(HEALTH) == 200
    except Exception:  # noqa: BLE001
        up = False
    add("서버", up, "켜져 있어요" if up else "꺼져 있어요",
        "" if up else "바탕화면의 '가족비서 열기'를 눌러 보세요. 그래도 안 되면 '문제해결'에서 다시 시작하거나, 컴퓨터를 한 번 다시 켜 보세요.")

    if env is None:
        add("설정 파일(.env)", False, "없어요", "'설치하기'를 다시 실행해 주세요.")
    else:
        secret = get_value(env, "PA_SESSION_SECRET") or ""
        good = len(secret) >= 32
        add("설정 파일(.env)", good, "잘 되어 있어요" if good else "필수 값이 비어 있거나 너무 짧아요", "" if good else "'설치하기'를 다시 실행하면 자동으로 채워 줘요.")

    rc, _ = run(["codex", "login", "status"])
    add("AI(Codex) 로그인", rc == 0, "로그인되어 있어요" if rc == 0 else ("로그인이 필요해요" if rc != 127 else "프로그램이 없어요"),
        "" if rc == 0 else ("검은 창에 'codex login' 을 입력하고, 열리는 브라우저에서 ChatGPT 로그인을 해 주세요." if rc != 127
                            else "'설치하기'를 다시 실행해 주세요."))

    rc, text = run(["tailscale", "status", "--json"])
    try:
        info = tailnet_info(json.loads(text)) if rc == 0 else None
    except ValueError:
        info = None
    add("Tailscale", info is not None, "연결되어 있어요" if info else "로그인이 안 되어 있어요",
        "" if info else "화면 오른쪽 아래 Tailscale 아이콘을 눌러 로그인해 주세요(휴대폰과 같은 계정으로).")

    served = False
    if info:
        rc, text = run(["tailscale", "serve", "status"])
        served = rc == 0 and f"127.0.0.1:{PORT}" in text
    add("휴대폰 접속 주소", served, "열려 있어요" if served else "아직 안 열렸어요",
        "" if served else f"검은 창에 'tailscale serve --bg --https=443 http://127.0.0.1:{PORT}' 를 입력해 주세요.")

    if env is not None:
        hosts = (get_value(env, "PA_ALLOWED_HOSTS") or "").split(",")
        ok = bool(get_value(env, "PA_ALLOWED_TAILSCALE_LOGIN")) and any(h.strip() not in ("", "localhost", "127.0.0.1") for h in hosts)
        add("휴대폰 접속 설정", ok, "되어 있어요" if ok else "아직 안 되어 있어요", "" if ok else "Tailscale 로그인 뒤 '설치하기'를 다시 실행하면 자동으로 넣어 줘요.")

    data = project / (get_value(env or "", "PA_DATA_DIR") or "data").removeprefix("./")
    done = (data / "family_profile.yaml").exists()
    add("첫 인터뷰", done, "마쳤어요" if done else "아직 안 했어요", "" if done else "브라우저에서 http://localhost:8010 을 열고 질문에 답해 주세요.")

    if windows:
        rc, _ = run(["schtasks", "/query", "/tn", TASK_NAME])
        add("자동 실행", rc == 0, "컴퓨터를 켜면 자동으로 켜져요" if rc == 0 else "등록되어 있지 않아요",
            "" if rc == 0 else "'설치하기'를 다시 실행해 주세요.")

    try:
        free = free_bytes(project)
    except OSError:
        free = MIN_FREE
    add("저장 공간", free >= MIN_FREE, "충분해요" if free >= MIN_FREE else "거의 가득 찼어요", "" if free >= MIN_FREE else "컴퓨터의 저장 공간을 조금 비워 주세요.")

    if env is not None:
        has = bool(get_value(env, "PA_BACKUP_KEY"))
        add("자동 백업", has, "켜져 있어요" if has else "꺼져 있어요", "" if has else "'설치하기'를 다시 실행하면 백업 키를 만들어 줘요.", level="warn")
    return out


def summarize(checks: list[Check]) -> str:
    bad = [c for c in checks if not c.ok]
    return "모두 잘 되고 있어요." if not bad else f"확인이 필요한 곳이 {len(bad)}군데 있어요."


def report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{'✓' if c.ok else ('△' if c.level == 'warn' else '✗')} {c.name}: {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"    해결: {c.fix}")
    return "\n".join(lines + ["", summarize(checks)])


def find_listener_pid(port: int, run=_run) -> int | None:
    rc, text = run(["netstat", "-ano", "-p", "tcp"])
    if rc != 0:
        return None
    for line in text.splitlines():
        m = re.match(rf"\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def restart_server(run=_run) -> str:
    """서버를 끈다. run_pa.bat 의 반복 실행이 10초 안에 새로 켠다(업데이트 직후에도 같은 방법)."""
    pid = find_listener_pid(PORT, run)
    if pid is None:
        return "not_running"
    run(["taskkill", "/F", "/PID", str(pid)])
    return "restarted"


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="가족 비서 문제해결")
    ap.add_argument("--restart", action="store_true", help="서버를 다시 시작한다")
    ap.add_argument("--interactive", action="store_true", help="결과를 보여 준 뒤 서버를 다시 시작할지 묻는다")
    args = ap.parse_args(argv)
    if args.restart:
        r = restart_server()
        print("서버를 껐어요. 10초쯤 뒤 자동으로 다시 켜져요." if r == "restarted"
              else "서버가 켜져 있지 않아요. 바탕화면의 '가족비서 열기'를 눌러 주세요.")
        return 0
    checks = run_checks()
    print(report(checks))
    code = 0 if all(c.ok or c.level == "warn" for c in checks) else 1
    if args.interactive:
        try:
            answer = input("\n서버를 다시 시작할까요? (Y/N): ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("y", "yes", "ㅛ", "예", "네"):
            return_code = main(["--restart"])
            return code or return_code
    return code


if __name__ == "__main__":
    sys.exit(main())
