# 구현 인계

## 현재 상태
- 1~7단계 구현과 커밋이 끝났다(`git log --oneline` 참고). 각 단계 끝에 `uv run pytest -q` 전부 통과, `uv run python tools/privacy_check.py` 0건을 확인했다.
- **아직 하지 않은 것: 원격 추가와 push.** 저장소는 공개이므로, 감독자가 개인 정보 검사 + 사람이 직접 점검한 뒤 **사용자 확인을 받고** 한다. 이미지·스크린샷은 올리지 않는다(아이콘 제외).

## 감독자 점검 목록(push 전)
1. `uv run python tools/privacy_check.py` 가 0건인지, `git status` 에 `data/`·`.env`·`client_secret*.json`이 없는지.
2. **커밋 이력 정리가 필요하다.** 작업 폴더는 privacy_check 0건이지만, 첫 커밋(`Stage 1`)의 이력에는 금지 표현이 남아 있다(`docs/DESIGN.md`·`docs/HANDOFF.md` 의 원본 설명, 몇몇 테스트의 지역명, 초기 privacy_check.py). 공개 저장소는 이력까지 보이므로 push 전에 이력을 새로 만들거나(예: 현재 트리를 새 첫 커밋으로 squash) 해당 커밋을 고쳐야 한다. 모든 커밋에 검사기 규칙을 적용하는 방법은 `git ls-tree -r <커밋>` + `git show <커밋>:<파일>` 로 `tools/privacy_check.py` 의 규칙을 돌리면 된다. 임시 파일은 `.gitignore` 의 `tools_tmp/`.
3. 실제 PC 에서 `설치하기.bat` 를 한 번 끝까지 돌려 본다(winget·Tailscale·ChatGPT 로그인은 사람이 직접). 이 환경에서는 스크립트를 실행하지 않고 문법·약속만 테스트했다.
4. 아이폰에서 QR 등록 → 홈 화면 추가 → 알림 켜기 → 식단 알림이 오는지 확인한다(웹 푸시는 실기기 확인이 필요하다).
5. 가정통신문 사진 한 장으로 후보가 나오는지 확인한다(AI 호출은 실제 Codex 로그인이 필요하다).

## 진행 규칙(유지)
1. 단계마다 TDD, `uv run pytest -q` 전부 통과 후 커밋. 커밋 메시지 끝에 `Co-Authored-By` 줄.
2. 원본 앱 폴더는 읽기만 한다.
3. 비밀번호·토큰은 스크립트에 넣지 않는다. Tailscale·Codex(ChatGPT) 로그인은 사용자가 브라우저에서 직접 한다.
4. 사용자(동생)는 왕초보다. 화면 문구와 안내서는 쉬운 한국어로, 설치는 더블클릭 한 번으로 끝나게 만든다.
