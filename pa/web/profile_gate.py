"""가족 프로필(첫 인터뷰)이 없으면 화면을 /welcome 으로 보내는 순수 ASGI 미들웨어.

TailscaleGuard 안쪽에 둔다: 접속 자격이 없는 요청은 여기까지 오지 못한다.
"""
from pa.core import profile

OPEN_PREFIXES = ("/welcome", "/static/", "/health", "/manifest.webmanifest", "/sw.js", "/auth/")
_NEED_PROFILE = b'{"detail":"\\uba3c\\uc800 \\uccab \\uc124\\uc815\\uc744 \\ud574 \\uc8fc\\uc138\\uc694"}'


class ProfileGate:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or not self.settings.require_profile
                or scope.get("path", "").startswith(OPEN_PREFIXES)
                or profile.exists(self.settings.data_dir)):
            return await self.app(scope, receive, send)
        if scope.get("method", "GET").upper() in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/welcome"), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await send({"type": "http.response.start", "status": 409,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_NEED_PROFILE)).encode())]})
        await send({"type": "http.response.body", "body": _NEED_PROFILE})
