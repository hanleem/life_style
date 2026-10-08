"""설치 도우미: .env 를 만들고 채운다(비밀값 자동 발급, Codex 경로 탐지, Tailscale 주소 반영).

    uv run python -m tools.setup_env              # .env 만들기·빈 값 채우기(이미 있는 값은 건드리지 않는다)
    uv run python -m tools.setup_env --tailnet    # tailscale status 를 읽어 휴대폰 접속 설정을 넣는다

화면에는 키 이름만 보여 주고 값(비밀)은 절대 출력하지 않는다. 사용자 비밀번호는 다루지 않는다(로그인은 브라우저에서 사용자가 직접).
"""
import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOSTS = ["localhost", "127.0.0.1"]


def _line_re(key: str):
    return re.compile(rf"^{re.escape(key)}=(.*)$", re.M)


def get_value(text: str, key: str) -> str | None:
    m = _line_re(key).search(text)
    return m.group(1).strip().rstrip("\r") if m else None


def set_value(text: str, key: str, value: str) -> str:
    """KEY=값 줄을 제자리에서 바꾸고(주석·순서·줄바꿈 방식 유지), 없으면 끝에 붙인다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    rx = _line_re(key)
    if rx.search(text):
        return rx.sub(lambda m: f"{key}={value}" + ("\r" if m.group(1).endswith("\r") else ""), text, count=1)
    return text + ("" if text == "" or text.endswith("\n") else nl) + f"{key}={value}{nl}"


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def ensure_env(project: Path, *, which=shutil.which) -> dict:
    """.env 가 없으면 .env.example 로 만들고, 비어 있는 비밀값과 Codex 경로를 채운다. 이미 있는 값은 건드리지 않는다."""
    env_path, example = project / ".env", project / ".env.example"
    created = not env_path.exists()
    text = env_path.read_text(encoding="utf-8") if not created else (example.read_text(encoding="utf-8") if example.exists() else "")
    before, filled = text, []

    def fill(key: str, make, *, ok=lambda v: bool(v)):
        nonlocal text
        if not ok(get_value(text, key)):
            text = set_value(text, key, make())
            filled.append(key)

    fill("PA_SESSION_SECRET", lambda: secrets.token_urlsafe(48), ok=lambda v: bool(v) and len(v) >= 32)
    fill("PA_BACKUP_KEY", _fernet_key)
    fill("PA_GOOGLE_TOKEN_KEY", _fernet_key)
    fill("PA_DATA_DIR", lambda: "./data")
    if get_value(text, "PA_DEV_SKIP_TAILSCALE") != "false":
        text = set_value(text, "PA_DEV_SKIP_TAILSCALE", "false")
    found = which("codex")
    cur = get_value(text, "PA_CODEX_BIN")
    if found and (not cur or cur == "codex" or not Path(cur).exists()) and cur != found:
        text = set_value(text, "PA_CODEX_BIN", found)
        filled.append("PA_CODEX_BIN")
    elif not cur:
        text = set_value(text, "PA_CODEX_BIN", "codex")
    if text != before or created:
        env_path.write_text(text, encoding="utf-8", newline="")
    return {"created": created, "filled": filled, "codex_found": bool(found)}


def tailnet_info(status) -> tuple[str, str] | None:
    """`tailscale status --json` 에서 (이 PC 의 tailnet 주소, 로그인 이름). 로그인 전이면 None."""
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return None
    me = status.get("Self") or {}
    dns = str(me.get("DNSName") or "").rstrip(".")
    login = str(((status.get("User") or {}).get(str(me.get("UserID"))) or {}).get("LoginName") or "")
    return (dns, login) if dns and login else None


def _merge(existing: str | None, *new: str) -> str:
    items = [x.strip() for x in (existing or "").split(",") if x.strip()]
    for n in new:
        if n not in items:
            items.append(n)
    return ",".join(items)


def apply_tailnet(project: Path, status) -> dict:
    """휴대폰 접속에 필요한 설정(허용 호스트·공개 주소·허용 로그인)을 넣는다. 이미 있는 다른 값은 지키고 더한다."""
    info = tailnet_info(status)
    if info is None:
        return {"ok": False, "host": None}
    dns, login = info
    env_path = project / ".env"
    text = env_path.read_text(encoding="utf-8")
    text = set_value(text, "PA_ALLOWED_HOSTS", _merge(get_value(text, "PA_ALLOWED_HOSTS") or ",".join(LOCAL_HOSTS), *LOCAL_HOSTS, dns))
    text = set_value(text, "PA_PUBLIC_URL", f"https://{dns}")
    text = set_value(text, "PA_ALLOWED_TAILSCALE_LOGIN", _merge(get_value(text, "PA_ALLOWED_TAILSCALE_LOGIN"), login))
    env_path.write_text(text, encoding="utf-8", newline="")
    return {"ok": True, "host": dns}


def _tailscale_status() -> dict | None:
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=30)
        return json.loads(r.stdout.decode("utf-8", "replace")) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="가족 비서 설치 도우미")
    ap.add_argument("--tailnet", action="store_true", help="tailscale 정보를 읽어 휴대폰 접속 설정을 넣는다")
    args = ap.parse_args(argv)
    if args.tailnet:
        r = apply_tailnet(ROOT, _tailscale_status())
        print("휴대폰 접속 설정을 넣었어요." if r["ok"] else "Tailscale 로그인이 아직 안 된 것 같아요. 로그인 뒤에 다시 해 주세요.")
        return 0 if r["ok"] else 1
    s = ensure_env(ROOT)
    print("설정 파일(.env)을 새로 만들었어요." if s["created"] else "설정 파일(.env)이 이미 있어요.")
    for k in s["filled"]:
        print(f"  채움: {k}")
    print("AI(Codex) 프로그램을 찾았어요." if s["codex_found"] else "AI(Codex) 프로그램을 아직 못 찾았어요(설치가 끝나면 다시 시도해 주세요).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
