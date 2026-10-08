# 가족 비서 (Family Edition)

4인 가정(어른 2 + 아이 둘)을 위한 개인 비서 앱입니다. 윈도우 PC에서 항상 켜 두고, 아이폰에서는 홈 화면 앱(PWA)으로 쓰고, AI는 본인 ChatGPT 계정의 **Codex CLI**로 동작합니다.
설계는 `docs/DESIGN.md`에 있습니다.

## 쓰는 사람(왕초보)은 여기부터
**[시작하기.md](시작하기.md)** 를 보세요. 폴더의 `설치하기` 를 더블클릭하면 설치가 끝나고, `업데이트` / `문제해결` 도 더블클릭 한 번입니다.

## 기능
- **첫 인터뷰**: 호칭·말투, 가족(나이·성별), 알레르기, 요리 실력·시간, 식비, 장보는 곳, 관심사, 테마(4종×라이트/다크)·글씨 크기. 저장하면 앱이 바로 바뀝니다.
- **4인 가족 식단**: 아침·저녁·평일 아이 간식·(선택) 주말 점심·일요일 밑반찬. 가족 인분(어른 1.0, 아이 0.6/0.75/0.9)으로 양을 계산하고, 성장 영양 체크(칼슘·철분·생선·채소 반찬·단 음료/튀김)를 **코드로 판정**해 부족하면 한 번 다시 만듭니다. 알레르기 재료는 코드로 한 번 더 거릅니다. AI를 못 쓰면 기본 가정식 4주분(`config/fallback_meals.yaml`)으로 채웁니다.
- **할 일·승인 대기·캘린더(선택)**: 캘린더 쓰기와 쿠팡 장바구니 담기는 항상 승인을 거칩니다. 결제는 사용자가 직접 합니다.
- **가정통신문 → 일정**: 사진을 올리면 Codex가 일정·준비물을 읽어 *후보*로 보여 줍니다(할 일은 확정 전 후보, 일정은 승인 대기 초안). 사진은 읽은 직후 지웁니다.
- **아이 루틴**: 등교·하교·학원 요일과 준비물, 전날 저녁 "내일 준비물" 알림.
- **휴대폰 연결**: PC 화면의 QR(일회용 5분)을 찍어 기기를 등록하고(비밀번호 없음, 서명 쿠키 180일), 언제든 해제할 수 있습니다. 알림은 웹 푸시(VAPID)이며 고정 문구만 보냅니다.
- 주말 나들이 추천, 음성 브리핑, 암호화 백업(core.db).

## 접속 구조
서버는 `127.0.0.1:8010` 루프백에만 열고, 휴대폰은 `tailscale serve --bg --https=443 http://127.0.0.1:8010` 로 tailnet 안에서만 접속합니다.
이 PC의 브라우저(루프백)는 소유자로 자동 인정하고, 휴대폰은 QR 로 등록한 기기만 소유자입니다. Tailscale 로그인 이름 확인과 Host 허용 목록, 보안 헤더도 함께 적용됩니다.

## 개발
    uv sync
    uv run pytest
    uv run python tools/privacy_check.py     # 공개 저장소 게이트: 0건이어야 한다

개발 서버(개인 PC 확인용): `.env.example` 을 `.env` 로 복사해 값을 채우거나 `uv run python -m tools.setup_env` 로 만든 뒤
`uv run python -m pa.main` → `http://127.0.0.1:8010`

### 도구
| 명령 | 하는 일 |
|---|---|
| `uv run python -m tools.setup_env [--tailnet]` | `.env` 만들기·비밀 키 자동 발급·Codex 경로 탐지·Tailscale 주소 반영(값은 출력하지 않음) |
| `uv run python -m tools.doctor [--interactive\|--restart]` | 서버·Tailscale·Codex 로그인·설정 점검, 서버 다시 시작 |
| `uv run python tools/gen_tokens.py --css pa/web/static/tokens.css` | 디자인 토큰(테마)에서 CSS 생성 |
| `uv run python tools/gen_icons.py` | PWA 아이콘 생성 |

## 자동 백업·복원 (core.db)
앱이 켜져 있으면 **매일 03:00(KST)** 에 `data/backups/` 로 암호화된 백업(`.pabk`)을 만든다(03:00 에 꺼져 있었으면 켠 직후).
일관된 스냅샷을 떠서 `PA_BACKUP_KEY` 로 암호화하고, 만든 직후 복호화·무결성까지 확인한다. 보존: 최근 7일 + 4주. `.env`·키는 백업에 들어가지 않는다.

1. 키: 설치 스크립트가 자동으로 만들어 `.env` 의 `PA_BACKUP_KEY=` 에 넣는다. 같은 값을 비밀번호 관리자에도 보관한다(잃으면 백업을 열 수 없다). 수동 발급: `uv run python -m pa.backup genkey`
2. 확인: `uv run python -m pa.backup keycheck`
3. (권장) 외장 드라이브 미러: `uv run python -m pa.backup init-mirror E:\backup` 후 `.env` 에 `PA_BACKUP_MIRROR_DIR=E:\backup`
4. 지금 백업: `uv run python -m pa.backup run`, 목록: `uv run python -m pa.backup list`
5. 확인: `uv run python -m pa.backup verify <이름>`
6. 복원: 서버를 끄고 `uv run python -m pa.backup restore <이름> --replace-live` (새 폴더로 연습하려면 `--to 빈폴더`)
