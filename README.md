# 가족 비서 (Family Edition)

4인 가정을 위한 개인 비서 앱입니다. 윈도우 PC에서 항상 켜 두고, 아이폰으로도 접속해 쓰는 것을 목표로 합니다.
설계는 `docs/DESIGN.md`에 있습니다.

## 지금 상태 (Stage 1)
- 식단·장보기·재고(식단 화면), 할 일, 승인 대기(캘린더 쓰기), 물품 공급, 주말 나들이 추천, 음성 브리핑, 백업이 동작합니다.
- 접속은 이 PC의 브라우저(루프백)만 소유자로 인정합니다. 휴대폰 기기 등록(QR)·웹 푸시 알림은 Stage 5에서 붙습니다.
- AI 연결(Codex)은 Stage 2에서, 첫 인터뷰와 테마는 Stage 3에서 붙습니다.
- 설치 스크립트와 `시작하기` 안내는 Stage 7에서 추가됩니다.

## 개발
    uv sync
    uv run pytest
    uv run python tools/privacy_check.py

## 실행 (개발용)
1. `.env.example`을 `.env`로 복사하고 값을 채웁니다.
2. `uv run python -m pa.main` → `http://127.0.0.1:8010`

## 자동 백업·복원 (core.db)
앱이 켜져 있으면 **매일 03:00(KST)** 에 `data/backups/` 로 암호화된 백업(`.pabk`)을 만든다(03:00 에 꺼져 있었으면 켠 직후).
일관된 스냅샷을 떠서 `PA_BACKUP_KEY` 로 암호화하고, 만든 직후 복호화·무결성까지 확인한다. 보존: 최근 7일 + 4주. `.env`·키는 백업에 들어가지 않는다.

1. 키 만들기: `uv run python -m pa.backup genkey` → 출력 한 줄을 `.env` 의 `PA_BACKUP_KEY=` 에 넣고 같은 값을 비밀번호 관리자에 보관한다.
2. 확인: `uv run python -m pa.backup keycheck`
3. (권장) 외장 드라이브 미러: `uv run python -m pa.backup init-mirror E:\backup` 후 `.env` 에 `PA_BACKUP_MIRROR_DIR=E:\backup`
4. 지금 백업: `uv run python -m pa.backup run`, 목록: `uv run python -m pa.backup list`
5. 확인: `uv run python -m pa.backup verify <이름>`
6. 복원: 서버를 끄고 `uv run python -m pa.backup restore <이름> --replace-live` (새 폴더로 연습하려면 `--to 빈폴더`)
