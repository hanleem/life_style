"""웹 푸시(VAPID). 알림함에 새 알림이 쌓이는 같은 지점(pa.core.notify)에서 구독한 기기로도 보낸다.

- VAPID 키는 첫 실행 때 data/ 에 자동으로 만든다(개인 키 파일은 저장소에 올라가지 않는다).
- 푸시에는 고정 문구 제목과 이동할 화면 주소만 담는다(품목 이름 같은 자유 문장은 보내지 않는다).
- 구독 주소(endpoint)는 알려진 푸시 서비스의 https 만 받는다(서버가 이 주소로 요청을 보내므로).
- 전송 실패는 알림 자체를 막지 않는다. 만료(404/410)된 구독은 지우고, 연속 실패가 쌓이면 지운다. 로그에는 예외 종류만 남긴다.
"""
import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pa.clock import utcnow_iso
from pa.ids import new_id

log = logging.getLogger("pa.push")
KEY_FILE = "vapid_private.pem"
MAX_FAILURES = 5
TTL_SECONDS = 6 * 3600
SEND_TIMEOUT = 10
_B64URL = re.compile(r"^[A-Za-z0-9_-]{1,200}={0,2}$")
# 알려진 푸시 서비스(Apple, Google/FCM, Mozilla, Windows). 이 도메인(또는 하위 도메인)만 받는다.
PUSH_HOST_SUFFIXES = ("push.apple.com", "googleapis.com", "push.services.mozilla.com", "notify.windows.com")


def _default_sender(sub: dict, payload: str, key_path: str, contact: str) -> None:
    from pywebpush import webpush
    webpush(subscription_info=sub, data=payload, vapid_private_key=key_path, vapid_claims={"sub": contact},
            ttl=TTL_SECONDS, timeout=SEND_TIMEOUT)


@dataclass
class Config:
    data_dir: Path
    contact: str = "mailto:admin@example.com"
    sender: object = _default_sender

    @property
    def key_path(self) -> str:
        return str(Path(self.data_dir) / KEY_FILE)


_cfg: Config | None = None


def configure(cfg: Config | None) -> None:
    """앱이 시작할 때 한 번 정한다(None 이면 푸시를 보내지 않는다)."""
    global _cfg
    _cfg = cfg


def ensure_keys(data_dir: Path) -> str:
    """VAPID 키 파일이 없으면 만들고, 브라우저에 넘길 공개키(base64url, 65바이트 비압축 점)를 돌려준다."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / KEY_FILE
    v = Vapid()
    if path.exists():
        v = Vapid.from_file(str(path))
    else:
        v.generate_keys()
        v.save_key(str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _check_endpoint(endpoint) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 500:
        raise ValueError("푸시 주소가 올바르지 않아요")
    u = urlsplit(endpoint)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.username or u.password:
        raise ValueError("푸시 주소가 올바르지 않아요")
    if not any(host == s or host.endswith("." + s) for s in PUSH_HOST_SUFFIXES):
        raise ValueError("알 수 없는 푸시 서비스예요")
    return endpoint


def subscribe(con, device_id: str, sub) -> str:
    """브라우저의 PushSubscription(JSON)을 저장한다. 같은 주소는 키만 바꿔 다시 쓴다."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        raise ValueError("구독 정보가 올바르지 않아요")
    endpoint = _check_endpoint(sub.get("endpoint"))
    p256dh, auth = sub["keys"].get("p256dh"), sub["keys"].get("auth")
    for k in (p256dh, auth):
        if not isinstance(k, str) or not _B64URL.match(k):
            raise ValueError("구독 키가 올바르지 않아요")
    con.execute("DELETE FROM push_subscription WHERE endpoint=?", (endpoint,))
    sid = new_id()
    con.execute("INSERT INTO push_subscription(id,device_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?,?)",
                (sid, device_id, endpoint, p256dh, auth, utcnow_iso()))
    return sid


def unsubscribe(con, device_id: str, endpoint: str) -> bool:
    return con.execute("DELETE FROM push_subscription WHERE device_id=? AND endpoint=?", (device_id, endpoint)).rowcount == 1


def _status(exc: Exception):
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code


async def deliver(con, title: str, href: str) -> int:
    """구독한 모든 기기로 보낸다. 성공한 개수를 돌려준다. 어떤 실패도 밖으로 던지지 않는다."""
    cfg = _cfg
    if cfg is None:
        return 0
    try:
        rows = con.execute("SELECT id, endpoint, p256dh, auth, failed_count FROM push_subscription").fetchall()
    except Exception as e:  # noqa: BLE001 - 알림이 푸시 때문에 실패하면 안 된다
        log.warning("push list failed (%s)", type(e).__name__)
        return 0
    payload = json.dumps({"title": title, "href": href}, ensure_ascii=False)
    ok = 0
    for r in rows:
        sub = {"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}}
        try:
            await asyncio.to_thread(cfg.sender, sub, payload, cfg.key_path, cfg.contact)
        except Exception as e:  # noqa: BLE001
            code = _status(e)
            log.warning("push failed (%s%s)", type(e).__name__, f", status={code}" if code else "")
            if code in (404, 410) or r["failed_count"] + 1 >= MAX_FAILURES:
                con.execute("DELETE FROM push_subscription WHERE id=?", (r["id"],))
            else:
                con.execute("UPDATE push_subscription SET failed_count=failed_count+1 WHERE id=?", (r["id"],))
            continue
        con.execute("UPDATE push_subscription SET failed_count=0 WHERE id=?", (r["id"],))
        ok += 1
    return ok
