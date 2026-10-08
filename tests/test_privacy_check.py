"""tools/privacy_check.py: 공개 저장소에 개인 정보가 섞이지 않게 막는 검사기 자체의 테스트."""
import subprocess
import sys
from pathlib import Path

import pytest

from tools import privacy_check as pc

ROOT = Path(__file__).resolve().parent.parent
j = pc._j   # 금지 표현을 이 테스트 파일에 그대로 쓰지 않으려고 조각으로 만든다


def _run(tmp_path, files: dict[str, str | bytes]) -> list[str]:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return pc.check(tmp_path, list(files))


def test_clean_files_pass(tmp_path):
    ok = {
        "a.py": "x = 1\n",
        "mail.md": "문의: noreply@anthropic.com, family@example.com, a@mail.example.com\n",
        "pa/web/static/icons/icon.png": b"\x89PNG",
        "pa/web/templates/p.html": "<p>가족 비서</p>\n",
        "calendar.py": 'CAL = "ko.south_korea#holiday@group.v.calendar.google.com"\n',
    }
    assert _run(tmp_path, ok) == []


@pytest.mark.parametrize("word", [j("교", "수"), j("연구", "실"), j("Lab", "Note"), j("nano", "lab"), j("학", "생"),
                                  j("han", "leem"), j("김", "민주"), j("죽", "전"), j("S", "SJ"), j("M", "JK")])
def test_banned_expressions_are_found(tmp_path, word):
    got = _run(tmp_path, {"t.txt": f"앞줄\n오늘 {word} 일정\n"})
    assert got == ["t.txt:2: 금지 표현"]


def test_initials_only_match_whole_words(tmp_path):
    glued = j("X", "S", "SJX 와 x", "C", "BSx 는 낱말이 아니다\n")
    assert _run(tmp_path, {"t.txt": glued}) == []


def test_emails_other_than_example_are_found(tmp_path):
    got = _run(tmp_path, {"t.txt": "a@" "gmail.com\nok@example.com\nb@" "corp.co.kr\n"})
    assert got == ["t.txt:1: 이메일 주소", "t.txt:3: 이메일 주소"]


def test_tailnet_host_names_are_found(tmp_path):
    host = j("pc.tail1234.", "ts", ".net")
    assert _run(tmp_path, {"t.txt": f"HOST={host}\n"}) == ["t.txt:1: tailnet 호스트 이름"]


def test_long_token_like_strings_are_found(tmp_path):
    tok = "aB3dE5fG7hI9jK1l" "M3nO5pQ7rS9tU1vW3xY5z"   # 이어 붙이면 영문 대소문자 + 숫자 32자 이상
    hexa = "0123456789abcdef" * 4
    got = _run(tmp_path, {"t.txt": f"KEY={tok}\nSHA={hexa}\nname=test_a_very_long_snake_case_function_name_1\n",
                          "uv.lock": f"hash = {tok}\n"})
    assert got == ["t.txt:1: 토큰처럼 보이는 긴 문자열", "t.txt:2: 토큰처럼 보이는 긴 문자열"]


def test_images_and_pdfs_only_allowed_as_icons(tmp_path):
    got = _run(tmp_path, {"shot.png": b"x", "docs/a.PDF": b"x", "b.jpeg": b"x", "c.webp": b"x",
                          "pa/web/static/icons/ok.png": b"x"})
    assert sorted(got) == sorted(["shot.png: 이미지·PDF 는 올리지 않는다(아이콘 제외)",
                                  "docs/a.PDF: 이미지·PDF 는 올리지 않는다(아이콘 제외)",
                                  "b.jpeg: 이미지·PDF 는 올리지 않는다(아이콘 제외)",
                                  "c.webp: 이미지·PDF 는 올리지 않는다(아이콘 제외)"])


def test_forbidden_paths_and_names(tmp_path):
    got = _run(tmp_path, {"data/core.db": b"x", ".env": "A=1\n", "design/mockups/a.html": "<p/>",
                          "config/" + j("weekly", "_cycle.yaml"): "a: 1\n", "docs/OWNER_ONLY.md": "x"})
    assert len(got) == 5 and all("저장소에 있으면 안 되는 파일" in g for g in got)


def test_output_never_contains_line_content(tmp_path):
    secret = j("김", "민주") + " 비밀 메모"
    got = _run(tmp_path, {"t.txt": secret + "\n"})
    assert got and secret not in "".join(got)


def test_cli_exit_code(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "tools/privacy_check.py")], capture_output=True, text=True,
                       encoding="utf-8", cwd=ROOT)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip().endswith("privacy_check: 0건")


def test_real_repository_is_clean():
    """공개 저장소 게이트: 추적 대상 파일 전체에서 0건이어야 한다."""
    assert pc.check() == []
