"""공개 저장소에 개인 정보가 섞이지 않았는지 검사한다. 0건이면 종료코드 0, 있으면 1.

검사 대상: git 이 추적하거나 추적 후보인(.gitignore 제외) 모든 파일.
- 금지 표현: 원본 소유자·원본 조직·연동 서비스·실명·집 주소 지역을 가리키는 낱말
- 이메일 주소(example 도메인과 noreply@anthropic.com 은 허용)
- tailnet 호스트 이름(tailscale 의 기본 도메인)
- 긴 토큰처럼 보이는 문자열(32자 이상, 영문자와 숫자가 섞인 것)
- 이미지·PDF(pa/web/static/icons/ 아래 아이콘만 허용)
- 저장소에 있으면 안 되는 경로·파일 이름(data/, .env, 원본 설정 파일 등)

출력에는 파일 경로, 줄 번호, 분류만 담는다(줄 내용은 찍지 않는다).
금지 표현은 이 파일 자체가 걸리지 않도록 조각으로 나눠 만든다.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _j(*parts: str) -> str:
    return "".join(parts)


# --- 금지 표현(조각으로 조립) -------------------------------------------------------------------
_WORDS = [
    _j("교", "수"), _j("연구", "실"), _j("Lab", "Note"), _j("lab", "note"), _j("nano", "lab"), _j("NANO", "LAB"),
    _j("학", "생"), _j("나", "노"), _j("nano", "energy"), _j("han", "leem"), _j("dl", "lee"),
    # 원본 조직 구성원 이름·집 주소 지역
    _j("신", "승주"), _j("박", "승진"), _j("임", "상범"), _j("김", "도은"), _j("채", "범수"), _j("한", "소은"),
    _j("김", "민주"), _j("이", "한림"), _j("죽", "전"), _j("수지", "구"), _j("용인", "시"),
]
_INITIALS = [_j("S", "SJ"), _j("C", "BS"), _j("M", "JK"), _j("H", "SE"), _j("K", "DE"), _j("P", "SJ"), _j("L", "SB")]   # 구성원 이니셜(낱말 단위로만 본다)
BANNED = re.compile("|".join(map(re.escape, _WORDS)) + r"|\b(?:" + "|".join(_INITIALS) + r")\b")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
EMAIL_OK = re.compile(r"@(?:[A-Za-z0-9-]+\.)*example\.(?:com|org|net)$|^noreply@anthropic\.com$|@group\.v\.calendar\.google\.com$",
                      re.I)
TAILNET = re.compile(re.escape(_j(".", "ts", ".", "net")) + r"\b")
_TOKEN_BODY = r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-]))"
# 영문 대·소문자와 숫자가 모두 섞인 긴 문자열(base64 계열 토큰·키), 또는 숫자가 섞인 16진수 32자 이상(해시·키)
TOKEN = re.compile(_TOKEN_BODY + r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"
                   r"|" + _TOKEN_BODY + r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{32,}")
TOKEN_SKIP_FILES = {"uv.lock"}
TOKEN_ALLOW_LINE = re.compile(r"data:audio/wav;base64,UklGRiQAAABXQVZFZm10")   # 무음 wav(공개 데이터)

# 이 파일들은 금지 표현을 설명하는 문서이므로 표현 검사에서 뺀다(현재는 없음)
PHRASE_SKIP_FILES: set[str] = set()

FORBIDDEN_PREFIXES = (
    "data/", "secrets/", "docs/handoff", "docs/superpowers/plans", "docs/superpowers/specs",
    "docs/OWNER_ONLY.md", "docs/PC_HANDOFF_", "pa/private", "tools_tmp/", "design/mockups/", ".claude/agents/",
)
FORBIDDEN_NAMES = {
    ".env", "client_secret.json", _j("student", "_rooms.yaml"), _j("mail", "_senders.yaml"), _j("nano", "lab_kinds.yaml"),
    _j("weekly", "_cycle.yaml"), "private.db", "core.db",
}
BLOCKED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".pdf"}
ICON_DIR = "pa/web/static/icons/"


def _files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                         capture_output=True, check=True).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def check(root: Path = ROOT, files: list[str] | None = None) -> list[str]:
    """문제 목록을 돌려준다. 각 항목은 '경로[:줄]: 분류' 형태이고 내용은 담지 않는다."""
    problems: list[str] = []
    for rel in (files if files is not None else _files(root)):
        p = root / rel
        if not p.is_file():
            continue
        name = Path(rel).name
        if name in FORBIDDEN_NAMES or rel.startswith(FORBIDDEN_PREFIXES):
            problems.append(f"{rel}: 저장소에 있으면 안 되는 파일")
            continue
        if Path(rel).suffix.lower() in BLOCKED_SUFFIXES and not rel.startswith(ICON_DIR):
            problems.append(f"{rel}: 이미지·PDF 는 올리지 않는다(아이콘 제외)")
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rel not in PHRASE_SKIP_FILES and BANNED.search(line):
                problems.append(f"{rel}:{n}: 금지 표현")
            for m in EMAIL.finditer(line):
                if not EMAIL_OK.search(m.group(0)):
                    problems.append(f"{rel}:{n}: 이메일 주소")
            if TAILNET.search(line):
                problems.append(f"{rel}:{n}: tailnet 호스트 이름")
            if name not in TOKEN_SKIP_FILES and TOKEN.search(line) and not TOKEN_ALLOW_LINE.search(line):
                problems.append(f"{rel}:{n}: 토큰처럼 보이는 긴 문자열")
    return problems


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔·파이프에서도 한글이 깨지지 않게
    problems = check()
    for pr in problems:
        print(pr)
    print(f"privacy_check: {len(problems)}건")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
