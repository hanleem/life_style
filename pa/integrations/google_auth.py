"""Google OAuth(캘린더 전용) 권한 단계와 Fernet 으로 암호화한 토큰 저장소."""
import asyncio
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from pa.clock import utcnow, utcnow_iso
from pa.core.errors import Forbidden

log = logging.getLogger(__name__)

SCOPES = {
    "base": ["https://www.googleapis.com/auth/calendar.readonly"],
    "calwrite": ["https://www.googleapis.com/auth/calendar.events"],
}
FORBIDDEN_SCOPES = {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.compose"}   # 메일 권한은 이 앱에서 쓰지 않는다
ALLOWED_SCOPES = frozenset(x for v in SCOPES.values() for x in v)


class TokenStore:
    def __init__(self, path: Path, key: str):
        self.path = Path(path)
        self._key = key

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _fernet(self) -> Fernet | None:
        if not self._key:
            return None
        try:
            return Fernet(self._key.encode())
        except (ValueError, TypeError):
            log.warning("google token key is invalid")
            return None

    def save(self, info: dict) -> None:
        f = self._fernet()
        if f is None:
            raise RuntimeError("google token key not configured")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(f.encrypt(json.dumps(info).encode()))

    def load(self) -> dict | None:
        f = self._fernet()
        if f is None or not self.path.exists():
            return None
        try:
            return json.loads(f.decrypt(self.path.read_bytes()).decode())
        except (InvalidToken, ValueError):
            log.warning("google token file could not be decrypted")
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _pref(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM preference WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # 증분 동의로 scope가 늘어나도 허용
REDIRECT_URI = "http://localhost:8010/auth/google/callback"
STATE_TTL = timedelta(minutes=10)


def token_path(settings) -> Path:
    return settings.google_token_path


class GoogleAuth:
    def __init__(self, settings, con):
        self.settings, self.con = settings, con
        self.store = TokenStore(token_path(settings), settings.google_token_key)

    # --- configuration ---
    def config_problem(self) -> str | None:
        if not self.store.enabled:
            return "PA_GOOGLE_TOKEN_KEY가 설정되지 않았습니다 (.env에 Fernet 키를 넣어 주세요)"
        if not Path(self.settings.google_client_secret_path).is_file():
            return "client_secret 파일을 찾을 수 없습니다 (PA_GOOGLE_CLIENT_SECRET_PATH 확인)"
        return None

    def _client_config(self) -> dict:
        data = json.loads(Path(self.settings.google_client_secret_path).read_text(encoding="utf-8"))
        return data.get("web") or data.get("installed") or {}

    def _flow(self, scopes: list[str]):
        from google_auth_oauthlib.flow import Flow
        return Flow.from_client_secrets_file(str(self.settings.google_client_secret_path), scopes,
                                             redirect_uri=REDIRECT_URI)

    # --- connection state ---
    def granted(self) -> list[str]:
        info = self.store.load()
        return list(info.get("scopes") or []) if info else []

    def connected(self) -> bool:
        info = self.store.load()
        return bool(info and (info.get("refresh_token") or info.get("token")))

    # --- flow ---
    def start(self, levels: list[str]) -> str:
        problem = self.config_problem()
        if problem:
            raise ValueError(problem)
        scopes = list(SCOPES["base"])
        for lv in levels:
            if lv not in SCOPES:
                raise ValueError("알 수 없는 연결 단계입니다")
            scopes += SCOPES[lv]
        scopes += [x for x in self.granted() if x in ALLOWED_SCOPES]
        scopes = list(dict.fromkeys(scopes))
        if FORBIDDEN_SCOPES & set(scopes):
            raise Forbidden("허용되지 않는 권한입니다")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        flow = self._flow(scopes)
        flow.code_verifier = verifier
        url, _ = flow.authorization_url(state=state, include_granted_scopes="true", prompt="consent")
        self.con.execute("DELETE FROM google_oauth_state WHERE expires_at<?", (utcnow_iso(),))
        self.con.execute("INSERT INTO google_oauth_state(state,code_verifier,scopes,expires_at) VALUES(?,?,?,?)",
                         (state, verifier, json.dumps(scopes), (utcnow() + STATE_TTL).isoformat()))
        return url

    def _exchange(self, code: str, verifier: str, scopes: list[str]) -> dict:
        flow = self._flow(scopes)
        flow.code_verifier = verifier
        flow.fetch_token(code=code)
        c = flow.credentials
        raw = getattr(c, "granted_scopes", None) or (flow.oauth2session.token or {}).get("scope") or scopes
        granted = raw.split() if isinstance(raw, str) else list(raw)
        return {"token": c.token, "refresh_token": c.refresh_token, "scopes": granted,
                "expiry": c.expiry.replace(tzinfo=timezone.utc).isoformat() if c.expiry else None}

    def finish(self, state: str, code: str) -> list[str]:
        cur = self.con.execute("UPDATE google_oauth_state SET used_at=? WHERE state=? AND used_at IS NULL "
                               "AND expires_at>?", (utcnow_iso(), state, utcnow_iso()))
        if cur.rowcount != 1:
            raise ValueError("연결 요청이 만료되었거나 이미 사용되었습니다")
        row = self.con.execute("SELECT code_verifier, scopes FROM google_oauth_state WHERE state=?",
                               (state,)).fetchone()
        info = self._exchange(code, row["code_verifier"], json.loads(row["scopes"]))
        granted = list(info.get("scopes") or [])
        if FORBIDDEN_SCOPES & set(granted):
            raise Forbidden("허용되지 않는 권한이 부여되어 저장하지 않았습니다")
        granted = [x for x in granted if x in ALLOWED_SCOPES]
        old = self.store.load() or {}
        self.store.save({"token": info.get("token"),
                         "refresh_token": info.get("refresh_token") or old.get("refresh_token"),
                         "scopes": granted, "expiry": info.get("expiry")})
        return granted

    async def access_token(self) -> str | None:
        info = self.store.load()
        if not info:
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            cfg = self._client_config()
            exp = datetime.fromisoformat(info["expiry"]) if info.get("expiry") else None
            if exp is not None:
                exp = (exp.astimezone(timezone.utc) if exp.tzinfo else exp).replace(tzinfo=None)
            creds = Credentials(token=info.get("token"), refresh_token=info.get("refresh_token"),
                                token_uri=cfg.get("token_uri", "https://oauth2.googleapis.com/token"),
                                client_id=cfg.get("client_id"), client_secret=cfg.get("client_secret"),
                                scopes=info.get("scopes"), expiry=exp)
            if creds.valid:
                return creds.token
            if not creds.refresh_token:
                return None
            await asyncio.to_thread(creds.refresh, Request())
            info.update(token=creds.token,
                        expiry=creds.expiry.replace(tzinfo=timezone.utc).isoformat() if creds.expiry else None)
            self.store.save(info)
            return creds.token
        except Exception as e:  # 토큰·응답 내용은 로그에 남기지 않는다
            log.warning("google token refresh failed (%s)", type(e).__name__)
            return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        print(Fernet.generate_key().decode())
