import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request

from pa.clock import utcnow, utcnow_iso
from pa.core.errors import Forbidden

router = APIRouter()
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# 접속 허용 상대 주소. 운영에서는 루프백뿐(설정/환경변수로 바꿀 수 없다). 테스트만 monkeypatch.
ALLOWED_PEERS = LOOPBACK
PROXY_HEADERS = ("tailscale-user-login", "tailscale-user-name", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                 "x-real-ip", "forwarded")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
GOOGLE_CALLBACK = "/auth/google/callback"


def LOOPBACK_DIRECT(settings):
    """TailscaleGuard 가 헤더 검사를 생략하는 조건: 프록시를 거치지 않고 직접 루프백(이 PC 의 브라우저)으로 들어온 요청.
    is_loopback_scope() 가 클라이언트 주소·프록시 헤더 부재·Host 이름을 모두 확인한다.
    TODO(Stage 5): 휴대폰은 QR 기기 등록(서명 쿠키)으로 인증한다. 지금은 이 PC(루프백)만 소유자다."""
    return True


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def is_loopback_scope(scope) -> bool:
    """직접 접속한 루프백만 허용: tailscale serve 등 프록시 경유(헤더 존재)나 다른 Host는 거부."""
    client = scope.get("client")
    if not client or client[0] not in LOOPBACK:
        return False
    h = _headers(scope)
    if any(p in h for p in PROXY_HEADERS):
        return False
    return host_name(h.get("host", "")) in LOCAL_HOSTS | {"[::1]"}


def is_loopback(request: Request) -> bool:
    return is_loopback_scope(request.scope)


def host_name(host: str) -> str:
    """Host 헤더에서 포트를 떼고 소문자로."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


SEC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "content-security-policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
}


async def _reject(scope, receive, send, status: int = 403) -> None:
    """http 는 JSON 403, websocket 은 handshake 거부(close 1008)."""
    if scope["type"] == "websocket":
        await receive()   # websocket.connect
        await send({"type": "websocket.close", "code": 1008})
        return
    body = b'{"detail":"forbidden"}'
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")] + [(k.encode(), v.encode()) for k, v in SEC_HEADERS.items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class ClientGuard:
    """가장 바깥. 판정의 기준은 scope["server"](연결을 받은 로컬 소켓 주소)다.
    프록시 헤더 미들웨어(uvicorn ProxyHeadersMiddleware 등)는 scope["client"] 만 X-Forwarded-For 로 덮어쓰고
    scope["server"] 는 건드리지 않으므로, 헤더로 위조할 수 없다. 규칙:
      - server 정보가 없거나 루프백이 아니면 거부(모든 인터페이스 바인딩 후 LAN 주소로 들어온 연결은 server 가 LAN IP 다).
      - server 가 루프백이면 허용: 루프백 주소로 받은 연결은 이 PC 의 로컬 프로세스(tailscale serve 포함)만 만들 수 있다.
        이때 client 가 100.x(tailscale 이 붙인 X-Forwarded-For 로 재작성됨)여도 소유자의 정상 경로이므로 거부하지 않는다.
      - client 정보 자체가 없으면 거부(방어 심층).
    main() 은 프록시 헤더를 아예 끄지만(proxy_headers=False), 팩토리로 직접 띄워도 소유자가 잠기지 않도록 이렇게 둔다.
    끄는 스위치는 없다. 테스트만 conftest 에서 ALLOWED_PEERS 를 monkeypatch 한다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client, server = scope.get("client"), scope.get("server")
            if not client or not server or server[0] not in ALLOWED_PEERS:
                return await _reject(scope, receive, send)
        await self.app(scope, receive, send)


class HostAllowlist:
    """DNS rebinding 방어: 허용 목록에 없는 Host 는 다른 모든 검사보다 먼저 거부."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            if host_name(_headers(scope).get("host", "")) not in self.settings.host_allowlist:
                return await _reject(scope, receive, send)
        await self.app(scope, receive, send)


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_BODY = b'{"detail":"cross-origin request blocked"}'


def _origin_matches(origin: str, host: str, allowed: set[str]) -> bool:
    """Origin(스킴://호스트[:포트])이 요청 Host 와 같은 호스트[:포트]인지. 허용 호스트 목록에도 있어야 한다."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(origin)
        o_host, o_port = (u.hostname or ""), u.port
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not o_host or u.path not in ("", "/") or u.username or u.password:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        h_name, _, rest = h.partition("]")
        h_name += "]"
        h_port = rest[1:] if rest.startswith(":") else ""
    else:
        h_name, _, h_port = h.partition(":")
    o_name = f"[{o_host}]" if ":" in o_host else o_host
    if o_name != h_name or h_name not in allowed:
        return False
    default = 443 if u.scheme == "https" else 80
    return (o_port or default) == (int(h_port) if h_port.isdigit() else default)


class CsrfGuard:
    """안전하지 않은 메서드(POST/PUT/PATCH/DELETE)의 교차 출처 요청을 거부한다(순수 ASGI, 모든 라우트에 적용).
    Sec-Fetch-Site 가 있으면 same-origin/none 일 때만 통과하고(Origin 은 보지 않는다: 같은 출처 POST 가 Origin: null 로 올 수 있다),
    없을 때만 Origin 이 요청 Host 와 같은 출처여야 한다(`null` 포함 불일치는 403). 둘 다 없으면(브라우저 아님) 통과 - 다른 방어선이 계속 적용된다."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    def _blocked(self, scope) -> bool:
        h = _headers(scope)
        site = h.get("sec-fetch-site")
        if site is not None:   # 브라우저가 붙이는 헤더(페이지가 위조할 수 없음)가 있으면 이것이 결정한다. 일부 브라우저는 같은 출처 POST 에도 Origin: null 을 보낸다
            return site.strip().lower() not in ("same-origin", "none")
        return "origin" in h and not _origin_matches(h["origin"], h.get("host", ""), self.settings.host_allowlist)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method", "GET").upper() in UNSAFE_METHODS and self._blocked(scope):
            headers = [(b"content-type", b"application/json"), (b"content-length", str(len(CSRF_BODY)).encode()),
                       (b"cache-control", b"no-store")] + [(k.encode(), v.encode()) for k, v in SEC_HEADERS.items()]
            await send({"type": "http.response.start", "status": 403, "headers": headers})
            await send({"type": "http.response.body", "body": CSRF_BODY})
            return
        await self.app(scope, receive, send)


# 접근 로그에서 가릴 경로 접두어의 유일한 목록(pa.main 이 그대로 쓴다). 지금은 비어 있다.
PRIVATE_PREFIXES: tuple[str, ...] = ()


class SecurityHeaders:
    """보안 헤더 + (/health 제외) 모든 응답 no-store. 500 에도 헤더를 붙인다.
    템플릿에 인라인 스크립트가 있어 script-src 는 제한하지 않는다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        started = False

        def add(headers: list) -> list:
            names = {k.lower() for k, _ in headers}
            out = list(headers)
            for k, v in SEC_HEADERS.items():
                if k.encode() not in names:
                    out.append((k.encode(), v.encode()))
            if path != "/health":
                out = [(k, v) for k, v in out if k.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
            return out

        async def wrapped(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": add(list(message.get("headers", [])))}
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            if not started:
                body = b"Internal Server Error"
                await wrapped({"type": "http.response.start", "status": 500,
                               "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                           (b"content-length", str(len(body)).encode())]})
                await wrapped({"type": "http.response.body", "body": body})
            raise


class TailscaleGuard:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            s, path = self.settings, scope.get("path", "")
            direct = scope["type"] == "http" and LOOPBACK_DIRECT(s) and is_loopback_scope(scope)
            if not direct:
                if not s.dev_skip_tailscale and not (scope["type"] == "http" and path == "/health"):
                    login = _headers(scope).get("tailscale-user-login", "")
                    if not s.allowed_tailscale_login or login != s.allowed_tailscale_login:
                        return await _reject(scope, receive, send)
        await self.app(scope, receive, send)


def fetch_site_ok(request: Request) -> bool:
    """브라우저가 Sec-Fetch-Site 를 보내면 same-origin/none 만 인정(같은 PC 의 다른 포트 = same-site 는 거부)."""
    v = request.headers.get("sec-fetch-site")
    return v is None or v.strip().lower() in ("same-origin", "none")


def same_origin(request: Request) -> bool:
    """Origin 이 정확히 http://<요청 Host>(Host 는 localhost/127.0.0.1), 또는 Origin 없음 + Sec-Fetch-Site: same-origin."""
    host = request.headers.get("host", "").strip().lower()
    if host_name(host) not in LOCAL_HOSTS:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site", "").strip().lower() == "same-origin"
    return origin.strip().lower() == "http://" + host


def require_owner(request: Request) -> str:
    """소유자 확인. 지금은 이 PC 의 브라우저(직접 루프백)만 소유자로 인정한다.
    TODO(Stage 5): QR 로 등록한 기기의 서명 쿠키(session["owner"])도 인정한다."""
    if request.session.get("owner"):
        return f"device:{request.session.get('device', '?')}"
    if is_loopback(request):
        return "pc:loopback"
    raise HTTPException(401, "로그인이 필요합니다")


@router.post("/api/nonce")
async def new_nonce(request: Request):
    require_owner(request)
    value = secrets.token_urlsafe(24)
    request.app.state.con.execute("INSERT INTO auth_nonce(value,purpose,expires_at) VALUES(?,?,?)",
                                  (value, "approve", (utcnow() + timedelta(minutes=2)).isoformat()))
    return {"nonce": value}


def consume_nonce(con, value: str) -> None:
    cur = con.execute("UPDATE auth_nonce SET used_at=? WHERE value=? AND used_at IS NULL AND expires_at>?",
                      (utcnow_iso(), value, utcnow_iso()))
    if cur.rowcount != 1:
        raise Forbidden("지문 확인이 만료되었습니다. 다시 눌러 주세요")
