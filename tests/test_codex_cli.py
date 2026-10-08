import asyncio
import json

import pytest

from pa.llm import codex_cli
from pa.llm.codex_cli import CodexCLI, CodexCLIError, RateLimited, build_args, sandbox_env

SECRET = "SECRET-MODEL-OUTPUT"


class _Proc:
    def __init__(self, out: bytes, err: bytes, rc: int):
        self.out, self.err, self.returncode = out, err, rc
        self.stdin_seen = b""

    async def communicate(self, data=None):
        self.stdin_seen = data or b""
        return self.out, self.err

    def kill(self):
        pass

    async def wait(self):
        pass


def _jsonl(*events) -> bytes:
    return "\n".join(json.dumps(e) for e in events).encode()


def _done(input_tokens=11, output_tokens=7):
    return {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


def _patch(monkeypatch, message, out=None, err=b"", rc=0):
    """message 를 -o 파일에 써 주는 가짜 codex 프로세스."""
    calls = {}

    async def fake(*args, **kw):
        calls["args"], calls["kw"] = list(args), kw
        if message is not None and "-o" in args:
            open(args[list(args).index("-o") + 1], "w", encoding="utf-8").write(message)
        proc = _Proc(_jsonl(_done()) if out is None else out, err, rc)
        calls["proc"] = proc
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


async def test_run_json_parses_last_message_and_usage(monkeypatch):
    calls = _patch(monkeypatch, 'ok: {"reply": "안녕"} 끝')
    data, usage = await CodexCLI("codex").run_json("질문", None)
    assert data == {"reply": "안녕"} and usage == {"input_tokens": 11, "output_tokens": 7}
    assert calls["proc"].stdin_seen.decode() == "질문"


async def test_system_file_is_prepended_to_stdin(monkeypatch, tmp_path):
    f = tmp_path / "sys.md"
    f.write_text("너는 비서다", encoding="utf-8")
    calls = _patch(monkeypatch, '{"a": 1}')
    await CodexCLI("codex").run_json("질문", f)
    text = calls["proc"].stdin_seen.decode()
    assert text.startswith("너는 비서다") and text.endswith("질문")


async def test_runs_in_empty_temp_dir_with_scrubbed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    calls = _patch(monkeypatch, '{"a": 1}')
    await CodexCLI("codex").run_json("p", None)
    env = calls["kw"]["env"]
    assert "PA_SESSION_SECRET" not in env and "OPENAI_API_KEY" not in env
    assert calls["kw"]["cwd"] != str(tmp_path)


def test_args_are_read_only_and_ephemeral():
    a = build_args("codex", "/tmp/out.txt")
    assert a[:2] == ["codex", "exec"]
    assert a[a.index("--sandbox") + 1] == "read-only"
    for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--json"):
        assert flag in a
    assert a[a.index("-o") + 1] == "/tmp/out.txt"
    assert 'web_search="disabled"' in a and 'web_search="live"' not in a
    assert "-i" not in a and "--search" not in a


def test_web_search_only_when_allowed():
    a = build_args("codex", "o.txt", web=True)
    assert 'web_search="live"' in a and 'web_search="disabled"' not in a


def test_images_are_attached_before_other_flags():
    a = build_args("codex", "o.txt", images=["a.jpg", "b.jpg"])
    assert a.count("-i") == 2
    assert a[a.index("-i") + 1] == "a.jpg"


def test_cmd_wrapper_on_windows_shim():
    a = build_args(r"C:\npm\codex.cmd", "o.txt")
    assert a[:4] == ["cmd", "/c", r"C:\npm\codex.cmd", "exec"]


def test_sandbox_env_drops_secrets_but_keeps_codex_home():
    env = sandbox_env({"PATH": "p", "CODEX_HOME": "c", "OPENAI_API_KEY": "k", "PA_X": "1", "FOO": "2"})
    assert env == {"PATH": "p", "CODEX_HOME": "c"}


@pytest.mark.parametrize("marker", ["You've hit your usage limit", "rate limit exceeded", "429 Too Many Requests"])
async def test_rate_limit_in_stderr(monkeypatch, marker):
    _patch(monkeypatch, None, out=b"", err=(marker + " " + SECRET).encode(), rc=1)
    with pytest.raises(RateLimited) as ei:
        await CodexCLI("codex").run_json("p", None)
    assert SECRET not in str(ei.value)


async def test_rate_limit_in_error_event(monkeypatch):
    out = _jsonl({"type": "error", "message": "usage limit reached"}, {"type": "turn.failed", "error": {"message": "usage limit"}})
    _patch(monkeypatch, None, out=out, rc=1)
    with pytest.raises(RateLimited):
        await CodexCLI("codex").run_json("p", None)


async def test_model_text_mentioning_rate_limit_is_normal(monkeypatch):
    _patch(monkeypatch, '{"body": "the rate limit and usage limit docs"}')
    data, _ = await CodexCLI("codex").run_json("p", None)
    assert data["body"].startswith("the rate limit")


@pytest.mark.parametrize("message,rc", [(None, 1), (SECRET, 0), ("{" + SECRET + "}", 0), ("", 0)])
async def test_failures_do_not_leak_output(monkeypatch, message, rc):
    _patch(monkeypatch, message, out=SECRET.encode(), err=SECRET.encode(), rc=rc)
    with pytest.raises(CodexCLIError) as ei:
        await CodexCLI("codex").run_json("p", None)
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)


async def test_timeout_raises_cli_error(monkeypatch):
    class Slow(_Proc):
        async def communicate(self, data=None):
            await asyncio.sleep(5)

    async def fake(*a, **k):
        return Slow(b"", b"", None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def noop(_):
        return None
    monkeypatch.setattr(codex_cli, "_terminate", noop)
    with pytest.raises(CodexCLIError, match="timeout"):
        await CodexCLI("codex", timeout=0.05).run_json("p", None)


def test_resolve_bin_prefers_absolute_existing(tmp_path):
    f = tmp_path / "codex.exe"
    f.write_text("x")
    assert codex_cli.resolve_bin(str(f)) == str(f)
