"""기기 등록(QR) — 비밀번호 없이 휴대폰을 쓰게 한다.

흐름: PC 화면 /devices 가 일회용 토큰(5분)을 만들어 QR 로 보여 주고, 폰이 QR 을 찍어 이름을 정하면 기기가 등록되어
서명된 쿠키(180일)를 받는다. 설정에서 기기를 해제하면 그 기기의 쿠키는 바로 쓸 수 없고 푸시 구독도 지워진다.
"""
import io
import secrets
from datetime import datetime, timedelta

import segno

from pa.clock import utcnow, utcnow_iso
from pa.ids import new_id

JOIN_TTL = timedelta(minutes=5)
TOUCH_EVERY = timedelta(minutes=10)
COOKIE_DAYS = 180
NAME_MAX = 30
DEFAULT_NAME = "내 기기"


def create_join_token(con) -> str:
    value = secrets.token_urlsafe(24)
    con.execute("INSERT INTO auth_nonce(value,purpose,expires_at) VALUES(?,?,?)",
                (value, "join", (utcnow() + JOIN_TTL).isoformat()))
    return value


def join_token_valid(con, value: str) -> bool:
    """쓰지 않고 확인만 한다(링크 미리보기·새로고침으로 토큰이 닳지 않게)."""
    return con.execute("SELECT 1 FROM auth_nonce WHERE value=? AND purpose='join' AND used_at IS NULL AND expires_at>?",
                       (value, utcnow_iso())).fetchone() is not None


def consume_join_token(con, value: str) -> bool:
    """한 번만 쓸 수 있다. 만료됐거나 다른 용도의 값이면 False."""
    cur = con.execute("UPDATE auth_nonce SET used_at=? WHERE value=? AND purpose='join' AND used_at IS NULL AND expires_at>?",
                      (utcnow_iso(), value, utcnow_iso()))
    return cur.rowcount == 1


def _clean_name(name: str) -> str:
    name = "".join(ch for ch in str(name or "") if ch not in "<>&\"'").strip()[:NAME_MAX]
    return name or DEFAULT_NAME


def add_device(con, name: str) -> str:
    did = new_id()
    now = utcnow_iso()
    con.execute("INSERT INTO device(id,name,created_at,last_seen_at) VALUES(?,?,?,?)", (did, _clean_name(name), now, now))
    return did


def list_devices(con) -> list[dict]:
    rows = con.execute("SELECT id,name,created_at,last_seen_at FROM device WHERE revoked_at IS NULL ORDER BY created_at, id")
    return [dict(r) for r in rows]


def is_active(con, device_id) -> bool:
    if not device_id:
        return False
    return con.execute("SELECT 1 FROM device WHERE id=? AND revoked_at IS NULL", (str(device_id),)).fetchone() is not None


def revoke(con, device_id: str) -> bool:
    cur = con.execute("UPDATE device SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (utcnow_iso(), device_id))
    if cur.rowcount != 1:
        return False
    con.execute("DELETE FROM push_subscription WHERE device_id=?", (device_id,))
    return True


def touch(con, device_id: str, now: datetime | None = None) -> None:
    """마지막으로 쓴 시각을 갱신한다(자주 쓰기를 막으려고 10분에 한 번만)."""
    now = now or utcnow()
    row = con.execute("SELECT last_seen_at FROM device WHERE id=?", (device_id,)).fetchone()
    if not row:
        return
    try:
        last = datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None
    except ValueError:
        last = None
    if last is None or now - last >= TOUCH_EVERY:
        con.execute("UPDATE device SET last_seen_at=? WHERE id=?", (now.isoformat(), device_id))


def join_url(settings, token: str) -> str | None:
    """폰이 QR 로 열 주소. 설정한 공개 주소(public_url)를 쓰고, 없으면 허용 호스트 중 tailnet 이름을 쓴다. 모르면 None."""
    base = (settings.public_url or "").strip().rstrip("/")
    if not base:
        host = next((h for h in settings.allowed_hosts if h not in ("localhost", "127.0.0.1", "[::1]")), None)
        base = f"https://{host}" if host else ""
    return f"{base}/join/{token}" if base else None


def qr_svg(url: str) -> str:
    """흰 바탕에 검은 점(다크 모드에서도 카메라가 읽도록 색을 고정한다). 외부 이미지·스크립트 없이 SVG 문자열만."""
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff", xmldecl=False,
                                    svgns=True, nl=False)
    return buf.getvalue().decode("utf-8")
