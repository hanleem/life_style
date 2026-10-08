"""Isolation checks for the V1 voice modules (settings, engine seam, cache)."""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "pa"
V1_MODULES = ["voice/prefs.py", "voice/tts.py", "voice/cache.py", "voice/source.py"]
# 건강·수면 같은 개인 기록 표현이 음성 모듈에 들어오지 않는지 본다
_PRIVATE_DATA = re.compile(
    r"life_log|sleep_session|routine_checkin|private_plan|alcohol|PrivateStore|open_store"
    r"|\bsleep_[a-z]|\bmood\b|mood_|\bcondition_|\blife_[a-z]")
NETWORK = {"httpx", "requests", "urllib", "socket", "edge_tts", "gtts", "google", "azure", "boto3", "openai",
           "aiohttp", "http", "ssl"}


def _imports(src):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            yield base
            for a in node.names:
                yield f"{base}.{a.name}"


@pytest.mark.parametrize("rel", V1_MODULES)
def test_no_private_counselor_or_network_imports(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    for m in _imports(src):
        bare = m.lstrip(".")
        assert not (bare == "pa.private" or bare.startswith("pa.private.") or bare == "private"
                    or bare.startswith("private.") or bare == "pa.private"), (rel, m)
        assert "counselor" not in bare, (rel, m)
        assert bare.split(".")[0] not in NETWORK, (rel, m)
    assert "pa.private" not in src
    assert not _PRIVATE_DATA.search(src), rel


@pytest.mark.parametrize("rel", V1_MODULES)
def test_no_logging_of_text_or_secret_fields(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert not re.search(r"(log|logger)\.\w+\([^)]*\btext\b", src)


def test_prefs_sql_touches_only_preference():
    src = (ROOT / "voice/prefs.py").read_text(encoding="utf-8")
    tables = set(re.findall(r"(?:FROM|INTO|(?<!DO )UPDATE|JOIN)\s+([a-z_]+)", src))
    assert tables == {"preference"}


