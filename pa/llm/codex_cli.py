"""OpenAI Codex CLI(`codex exec`) 호출기. 사용자의 ChatGPT 로그인으로 동작한다(API 키 없음).

run_json(prompt, system_file, allowed_tools) -> (dict, usage) 형태를 지켜 라우터가 그대로 부른다.
보안: 읽기 전용 샌드박스 + 빈 임시 작업 폴더 + 비밀을 걸러낸 환경 변수. 웹 검색은 요청한 호출에서만 켠다.
오류 메시지에는 출력 원문을 넣지 않는다(종류·길이만).
"""
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

_LIMIT_MARKERS = ("rate limit", "rate_limit", "usage limit", "limit reached", "too many requests", "429")
_sem = asyncio.Semaphore(1)


class RateLimited(Exception):
    pass


class CodexCLIError(Exception):
    """메시지에는 출력 텍스트를 넣지 않는다(종류·길이만)."""


def resolve_bin(name: str) -> str:
    """절대 경로면 그대로, 아니면 PATH 에서 찾는다(Windows: .exe → .cmd 순)."""
    p = Path(name)
    if p.is_absolute() and p.exists():
        return str(p)
    for cand in (name + ".exe", name + ".cmd", name):
        found = shutil.which(cand)
        if found:
            return found
    return name


def build_args(bin_path: str, out_file: str, *, web: bool = False, images=()) -> list[str]:
    """프롬프트는 stdin 으로 넘긴다(위치 인자 없음 — `-i` 가 뒤따르는 값을 먹지 않게)."""
    head = ["cmd", "/c", bin_path] if bin_path.lower().endswith((".cmd", ".bat")) else [bin_path]
    args = head + ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                   "--ignore-user-config", "--json", "-o", out_file,
                   "-c", 'web_search="live"' if web else 'web_search="disabled"',
                   "-c", 'shell_environment_policy.inherit="none"']
    for img in images:
        args += ["-i", str(img)]
    return args


# 자식 프로세스에 넘기는 환경 변수 허용 목록(비밀·PA_* 는 이름이 맞아도 한 번 더 걸러낸다).
# CODEX_HOME 은 로그인 정보 위치라 필요하다.
_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "COMSPEC", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "USERNAME", "USER",
    "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "SHELL",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "CODEX_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
})
_SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


def sandbox_env(environ=None) -> dict[str, str]:
    out = {}
    for k, v in (os.environ if environ is None else environ).items():
        u = k.upper()
        if u not in _ENV_ALLOW or u.startswith("PA_") or any(m in u for m in _SECRET_MARKERS):
            continue
        out[k] = v
    return out


_SPAWN_KW = {} if sys.platform == "win32" else {"start_new_session": True}   # POSIX: 프로세스 그룹째 종료 가능


async def _terminate(proc) -> None:
    """프로세스와 자손을 강제 종료하고 회수한다. 어떤 실패도 호출자의 원래 예외를 가리지 않는다."""
    pid = getattr(proc, "pid", None)
    try:
        if sys.platform == "win32" and pid:
            tk = await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(pid),
                                                      stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(tk.wait(), 10)
        elif pid and hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except BaseException as e:   # 취소 중 재취소는 삼키지 않는다
        if isinstance(e, asyncio.CancelledError):
            raise


def _events(stdout: bytes) -> list[dict]:
    out = []
    for line in (stdout or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _is_rate_limited(err_text: str, events: list[dict]) -> bool:
    """stderr 와 오류 이벤트만 본다. 모델이 쓴 정상 답변 텍스트는 보지 않는다."""
    if any(m in err_text for m in _LIMIT_MARKERS):
        return True
    for ev in events:
        if ev.get("type") in ("error", "turn.failed"):
            blob = json.dumps(ev, ensure_ascii=False).lower()
            if any(m in blob for m in _LIMIT_MARKERS):
                return True
    return False


def _usage(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            return {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}
    return {"input_tokens": None, "output_tokens": None}


class CodexCLI:
    def __init__(self, bin: str = "codex", timeout: float = 180.0):
        self.bin, self.timeout = bin, timeout

    async def run_json(self, prompt: str, system_file: Path | None, allowed_tools: str = "",
                       images=()) -> tuple[dict, dict]:
        """allowed_tools 에 'WebSearch' 가 있을 때만 웹 검색을 켠다. images 는 파일 경로 목록."""
        system_text = system_file.read_text(encoding="utf-8") if system_file else None
        stdin_text = f"{system_text}\n\n{prompt}" if system_text else prompt
        web = "websearch" in allowed_tools.lower()
        env = sandbox_env()
        async with _sem:
            with tempfile.TemporaryDirectory(prefix="pa-codex-", ignore_cleanup_errors=True) as workdir:   # 프로젝트 파일이 보이지 않는 빈 폴더
                out_file = str(Path(workdir) / "last_message.txt")
                args = build_args(resolve_bin(self.bin), out_file, web=web, images=images)
                proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE, env=env, cwd=workdir,
                                                            **_SPAWN_KW)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), self.timeout)
                except TimeoutError:
                    raise CodexCLIError("timeout") from None
                finally:   # 타임아웃·취소·기타 예외 모두에서 프로세스(트리)를 정리한다
                    if getattr(proc, "returncode", 0) is None:
                        await _terminate(proc)
                try:
                    message = Path(out_file).read_text(encoding="utf-8")
                except OSError:
                    message = ""
        err_text = (err or b"").decode("utf-8", "replace").lower()
        events = _events(out)
        if _is_rate_limited(err_text, events):
            raise RateLimited(f"rate limited (stderr_len={len(err or b'')})")
        if proc.returncode != 0:
            raise CodexCLIError(f"exit {proc.returncode} (stdout_len={len(out or b'')}, stderr_len={len(err or b'')})")
        start, end = message.find("{"), message.rfind("}")
        if start < 0:
            raise CodexCLIError(f"no json in result (len={len(message)})")
        try:
            data = json.loads(message[start:end + 1])
        except ValueError:
            raise CodexCLIError(f"invalid json in result (len={len(message)})") from None
        if not isinstance(data, dict):
            raise CodexCLIError("result is not an object")
        return data, _usage(events)


def login_status(bin: str = "codex") -> bool:
    """`codex login status` 가 로그인 상태면 True. 문제해결 점검용(출력은 읽지 않는다)."""
    try:
        r = subprocess.run([resolve_bin(bin), "login", "status"], capture_output=True, timeout=30,
                           env=sandbox_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0
