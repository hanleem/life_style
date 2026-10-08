"""백업 화면·알림용 고정 문구(LLM 없음). 모든 문구는 coach_copy.notify_safe 를 통과해야 한다."""
BACKUP_CHECK = "백업을 확인해 주세요"
BACKUP_OK = "마지막 백업 {when} · {size}"
BACKUP_NEVER = "아직 백업이 없습니다"
BACKUP_OFF = "백업이 꺼져 있습니다 — 백업 키(PA_BACKUP_KEY)를 설정해 주세요"
MIRROR_MISSING = "외장 백업 위치를 찾을 수 없습니다"
MIRROR_OK = "외장 백업: 연결됨"
RETENTION_NOTE = "삭제한 개인 기록은 백업 보존 기간(14일)이 지나야 백업에서도 완전히 사라집니다"
REASON_TEXT = {
    "no_key": "백업 키가 설정되지 않았습니다",
    "bad_key": "백업 키 형식이 올바르지 않습니다",
    "unsafe_location": "백업 위치가 안전하지 않습니다",
    "locked": "다른 백업이 진행 중입니다",
    "low_disk": "저장 공간이 부족합니다",
    "too_large": "데이터가 백업 상한보다 큽니다",
    "sqlite_old": "데이터베이스 엔진이 오래되어 백업할 수 없습니다",
    "busy": "데이터베이스가 사용 중입니다",
    "integrity": "데이터 파일 점검에 문제가 있습니다",
    "io": "백업 파일을 쓰지 못했습니다",
    "verify": "백업 확인에 문제가 있습니다",
    "unexpected": "알 수 없는 문제로 백업하지 못했습니다",
}
