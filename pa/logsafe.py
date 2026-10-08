"""예외 메시지에는 사용자 데이터(메일 제목, 본문, 경로 등)가 실릴 수 있다. 로그에는 고정 단계 이름과 예외 클래스 이름만 남긴다."""
import logging


def log_failure(log: logging.Logger, step: str, exc: BaseException, level: int = logging.ERROR) -> None:
    log.log(level, "%s failed (%s)", step, type(exc).__name__)
