# 가족 비서 (Family Edition) 설계서

- 작성일: 2026-10-08 · 대상: 구현 담당 Sonnet 에이전트
- 대상 폴더: `C:\Users\user\Downloads\life` · 원격: 공개 저장소(주소는 감독자가 push 직전에 정한다)
- 사용자: 주부, 4인 가구(어른 2 + 아이 10살·8살). **윈도우 데스크톱(상시 켜 둠) + 아이폰.** AI는 **OpenAI Codex CLI**(본인 ChatGPT 계정 로그인).
- 원본: PA_BOT(이 저장소). 원본에서 복사한 뒤 줄이는 방식(fork & strip). 원본 저장소는 건드리지 않는다.

## 0. 절대 규칙
1. 원본 소유자 개인 정보가 하나도 들어가면 안 된다: `data/`, `.env`, `client_secret.json`, 프로필, 원본의 조직·메일·채팅 설정 파일, 연구과제용 스카우트 설정, 원본의 인계·계획·설계 문서 폴더, 프롬프트와 테스트 픽스처 안의 실명·소속·호칭 표현, 이미지(스크린샷). 저장소는 **공개**다. **커밋 전과 첫 push 전에** `uv run python tools/privacy_check.py`를 돌려서 결과가 0건이어야 한다. 검사 규칙(금지 표현, 이메일, 호스트 이름, 토큰 모양 문자열, 이미지)은 그 스크립트에 있다.
2. 외부로 나가는 부작용(캘린더 쓰기, 쿠팡 장바구니)은 기존 ApprovalGate를 그대로 거친다.
3. 각 단계는 TDD로 진행하고, 단계 끝에 `uv run pytest`가 전부 통과해야 커밋한다.

## 1. 남김 / 뺌
| 남김(가족용으로 손봄) | 뺌(코드·테스트·템플릿·라우트·스케줄 전부) |
|---|---|
| meals / nutrition / pantry / supplies / supply / shop_cli / taste | 원본의 사내 메신저·연구 보조 연동, 과제 공고 수집, 채팅 수집·분석, 비서 대화방, 전달, 구성원 관련 기능, 주간 사이클 |
| todos / task_ops / calendar(gcal, 선택) / holidays / quiet / notify | gmail, mail_* 전부, drafts, triage, delegated, commitments, meeting |
| routine_plan / routine_jobs (아이 등하교·학원 중심으로 단순화) | `pa/private` 전체(일기·상담·음주·감량·위기), counselor 에이전트, crisis 테스트 |
| scout: outing만(아이 동반 주말 나들이) | scout: grants·jobs·content 중 연구 관련 |
| voice(선택), backup(core.db만) | Ollama(`pa/llm/local.py`) — 대체 동작은 §3 |
| secretary 에이전트(대화 탭) | lessons(원본 소유자 승인 학습), pc_login 중 외부 메신저 연동 부분 |

## 2. 가족 프로필과 첫 인터뷰
- `data/family_profile.yaml`이 없으면 모든 페이지가 `/welcome`으로 넘어간다.
- `/welcome`은 HTMX 카드 인터뷰다. 한 화면에 질문 하나, 탭으로 선택, 뒤로 가기 가능, 전체 5분 이내.
  1. 호칭(예: "○○님"), 말투(`friendly` 친근한 말투 / `polite` 존댓말)
  2. 가족: 구성원 목록 `{role: adult|child, name?, age, sex}`, 알레르기, 싫어하는 음식
  3. 요리: 실력(초/중/고), 평일 요리 시간(분), 주간 식비 예산(원), 장보는 곳(쿠팡/마트/컬리/기타)
  4. 관심사(여러 개 선택): `kids_edu`, `housekeeping`, `health`, `money`, `outing`, `hobby`
  5. 테마: 미리보기 카드 4개 `pastel` / `wood` / `mono` / `fresh`, 글씨 크기 `normal|large`
- 저장하면 앱이 바로 바뀐다.
  - 테마: `tools/gen_tokens.py`를 확장해서 테마 4개 × 라이트/다크 CSS를 만들고 `<html data-theme>`로 적용한다.
  - 글씨 크기: `--font-body`를 18px로 키운다.
  - 말투: 프롬프트에 `{tone}`을 넣어 반영한다.
  - 홈 화면: 고른 관심사의 카드가 먼저 나온다. 안 고른 기능은 숨기고 설정에서 켤 수 있다.
- 설정에 "인터뷰 다시 하기"를 둔다.

## 3. AI: Codex CLI 연결
- `pa/llm/codex_cli.py`를 만들고 `run_json(prompt, system_file, allowed_tools="") -> (dict, usage)`를 `claude_cli`와 같은 형태로 맞춘다.
  - 실행: `codex exec --sandbox read-only --skip-git-repo-check --output-last-message <tmp>`(+ `--json`이 지원되면 사용). 시스템 프롬프트는 stdin 앞에 붙인다. 작업 폴더는 빈 임시 폴더로 한다(프로젝트 파일이 보이지 않게).
  - 웹 검색은 나들이 추천(`scout_search`)에서만 켠다(`--search`, 버전에 따라 `-c tools.web_search=true`). 설치된 버전의 `codex exec --help`를 먼저 확인하고 플래그를 맞춘다.
  - 속도 제한/한도 초과 문구는 `RateLimited`로 바꾼다. 오류 메시지에는 출력 원문을 넣지 않는다(종류·길이만).
- `TaskRouter`의 tier는 `ai`(=codex)와 `rule`만 남긴다. AI가 실패하면: 식단은 `config/fallback_meals.yaml`(기본 가정식 4주분)에서 고르고, 분류류는 규칙으로 처리한다.
- `.env`의 `PA_CODEX_BIN=codex`. 이 값은 설치 스크립트가 경로를 찾아서 넣는다.

## 4. 4인 가족·성장기 식단
- 끼니: 아침·저녁 + 아이 간식(주 5일). 주말 점심은 선택.
- 양 계산: 어른 1.0인분, 아이 `age<=8: 0.6`, `9~11: 0.75`, `12+: 0.9`. 장보기는 이 합계로 계산한다.
- 하루 목표 kcal(한국인 영양섭취기준): 6~8세 남 1,700 / 여 1,500, 9~11세 남 1,900 / 여 1,700, 성인 여 1,900 / 남 2,500(인터뷰에서 바꿀 수 있음). 표는 `pa/core/nutrition.py`에 둔다.
- 성장 체크(주간 규칙 검사, 코드로 판정): 칼슘원(우유·요거트·치즈·멸치·두부) 주 7회 이상, 철분원(소고기·달걀·시금치) 주 4회, 생선 주 2회, 채소 반찬 끼니당 2가지, 단 음료·튀김 주 2회 이하. 화면에 "이번 주 성장 영양 체크" 배지를 보여준다. 기준에 못 미치면 프롬프트를 고쳐 한 번 다시 생성한다.
- 스타일: 가정식 한식이 기본(국/찌개 + 메인 + 반찬). 아이 맵기 0~1단계, 어른용 매운 버전은 따로 적는다. 평일 요리는 인터뷰에서 고른 시간 이내, 주말에 밑반찬 만들기 1회.
- 원본 식단 원칙(주 단위 구매, 남김없이 쓰기, 가격 효율, 재고 반영, 쿠팡 장바구니 담기는 승인 후 실행, 결제는 사용자가 직접)을 4인분으로 그대로 옮긴다. 장보는 곳이 쿠팡이 아니면 장바구니 담기 기능을 숨기고 구매 목록만 보여준다.
- `config/meals.yaml`의 개인 설정(서양식, 600kcal, 다이어트 등)은 지우고, 인터뷰에서 받은 값으로 채운다.

## 5. 접속·로그인·알림 (원본의 메신저 연동 대체)
- 서버는 `127.0.0.1:8010` 루프백에 바인딩하고, `tailscale serve --bg --https=443 http://127.0.0.1:8010`으로 연다. 기존 TailscaleGuard, Host 허용 목록, 보안 헤더는 그대로 둔다.
- 로그인: PC 브라우저(루프백)는 소유자로 자동 인정한다. 휴대폰은 PC 화면 `/devices`의 **QR 코드(일회용 토큰, 5분)** 를 찍어서 기기를 등록하고, 이후 서명된 쿠키(180일)를 쓴다. 비밀번호는 없다. 설정에서 기기를 해제할 수 있다.
- PWA: `manifest.webmanifest`, 아이콘, service worker를 넣는다. 아이폰 안내 "공유 → 홈 화면에 추가" 페이지를 만든다.
- 알림: Web Push(VAPID, `pywebpush`). VAPID 키는 첫 실행 때 자동 생성한다. iOS는 홈 화면에 추가한 PWA에서만 동작하므로, 안내 페이지에서 "알림 켜기" 버튼으로 권한을 요청한다. 기존 `notify.py`의 외부 메신저 발송은 1단계에서 앱 안 알림함으로 바뀌었고, 이 단계에서 웹 푸시를 함께 보낸다.

## 6. 새 기능: 가정통신문 → 일정
- 사진 업로드(아이폰 카메라) → Codex에 이미지 입력(`codex exec -i <file>`) → `{title, date, items_to_prepare[], child}` 추출 → 할 일·일정 후보로 보여준다. 캘린더 쓰기는 승인 후 실행한다. 사진은 처리가 끝나면 지운다.
- 아이 루틴: 등교·하교·학원 요일·시간을 설정하고, 하루 전 저녁에 준비물 알림을 보낸다.

## 7. 원클릭 설치 (왕초보)
- `설치하기.bat`(더블클릭 → PowerShell `install.ps1 -ExecutionPolicy Bypass`):
  1. winget으로 `astral-sh.uv`, `OpenJS.NodeJS.LTS`, `Tailscale.Tailscale` 설치(이미 있으면 건너뜀). PATH를 새로 고친다.
  2. `npm i -g @openai/codex` → `uv sync`
  3. `.env` 생성: 세션 비밀키·백업 키·VAPID 키 자동 발급, `PA_CODEX_BIN` 자동 탐지
  4. **사용자 직접 단계(안내 화면 + 키 입력 대기)**: `tailscale up`(브라우저 로그인), `codex login`(브라우저 ChatGPT 로그인). 비밀번호는 스크립트가 다루지 않는다.
  5. `tailscale serve` 설정, tailnet 호스트명을 `PA_ALLOWED_HOSTS`에 자동으로 넣기
  6. 로그온 시 자동 실행 작업 등록(기존 `run_pa.bat` 감시 방식), 바탕화면 바로가기 "가족비서 열기"
  7. 브라우저로 `http://localhost:8010/welcome` 열기 → 인터뷰 → 마지막에 휴대폰 등록 QR
- `업데이트.bat`(git pull + uv sync + 재시작), `문제해결.bat`(서버·Tailscale·Codex 로그인 상태 점검 → 쉬운 말로 결과 보여주기 + 재시작).
- `시작하기.md`: 그림 위주 안내. 아이폰 Tailscale 앱 설치·로그인 → Safari로 주소 열기 → 홈 화면 추가 → 알림 켜기.

## 8. 구현 단계
1. 복사 + 기능 제거 + 외부 메신저 연결 끊기 + 개인 정보 검사 0건 + 테스트 통과
2. Codex 연결 + 대체 동작
3. 인터뷰 + 테마/홈 화면 바꾸기
4. 4인 가족·성장기 식단 + 성장 체크
5. QR 기기 등록 + PWA + 웹 푸시
6. 가정통신문 + 아이 루틴
7. 설치/업데이트/문제해결 스크립트 + 시작하기.md
각 단계가 끝나면 커밋한다. push는 감독자(Opus)가 개인 정보 검사를 확인한 뒤에 한다.
