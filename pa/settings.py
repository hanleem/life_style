from pathlib import Path
from typing import Annotated

import secrets

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PA_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    session_secret: str = "dev-only-change-me"
    allowed_tailscale_login: str = ""
    public_url: str = ""   # 폰에서 PA 에 들어가는 주소(tailscale serve 가 알려 주는 https 주소). 알림 링크에 쓴다
    dev_skip_tailscale: bool = False
    vapid_contact: str = "mailto:admin@example.com"   # 웹 푸시 서비스에 알리는 연락처(mailto: 또는 https 주소)
    require_profile: bool = True   # 첫 인터뷰(family_profile.yaml) 전에는 모든 화면을 /welcome 으로 보낸다
    # 허용 Host 헤더. 환경변수 PA_ALLOWED_HOSTS(쉼표 구분)로 tailnet 호스트명을 추가한다.
    allowed_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1", "localhost"]
    codex_bin: str = "codex"
    budget_ai_per_5h: int = 25
    budget_reserve_for_user: int = 8
    budget_ai_web_per_day: int = 6
    scout_config_path: Path = Path("config/scout.yaml")
    approval_ttl_minutes: int = 30
    google_client_secret_path: Path = Path("client_secret.json")
    google_token_key: str = ""
    backup_key: str = ""
    backup_dir: Path | None = None
    backup_mirror_dir: Path | None = None
    backup_include_google_token: bool = False
    backup_max_mb: int = 1024

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, v):
        if isinstance(v, str):
            v = v.split(",")
        return [h.strip().lower() for h in v if str(h).strip()]

    @model_validator(mode="after")
    def _no_default_secret(self):
        """개발 모드에서도 기본/짧은 세션 비밀값으로 쿠키 위조가 가능하면 안 된다: 프로세스마다 무작위 값으로 대체."""
        if self.session_secret == "dev-only-change-me" or len(self.session_secret) < 32:
            if self.dev_skip_tailscale:
                self.session_secret = secrets.token_urlsafe(48)
        return self

    @property
    def host_allowlist(self) -> set[str]:
        """루프백 이름은 항상 포함(운영 기본값을 env로 약화시키지 못하고, 구글 콜백이 깨지지 않게)."""
        return {"127.0.0.1", "localhost"} | set(self.allowed_hosts)

    @property
    def core_db_path(self) -> Path:
        return self.data_dir / "core.db"

    @property
    def google_token_path(self) -> Path:
        return self.data_dir / "google_token.enc"

    @property
    def backup_root(self) -> Path:
        return self.backup_dir or (self.data_dir / "backups")

    @property
    def backup_work_dir(self) -> Path:
        return self.data_dir / ".backup-work"
