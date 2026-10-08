"""소유자 전용 방어선 검증: 라우트 감사, Host 허용목록, docs 비활성, 헤더, 시작 거부."""
import ast
import inspect
import re
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient

from pa import main
from pa.settings import Settings
from pa.web import auth
from pa.web.app import create_app

LOGIN = "prof@example.com"
TS = {"Tailscale-User-Login": LOGIN}
# 가드 통과 후에도 공개인 경로(소유자 세션 불필요) - 이 상수만 늘릴 수 있다.
PUBLIC_AFTER_GUARD = {"/health", "/auth/google/callback"}
SKIP_METHODS = {"HEAD", "OPTIONS"}


def make_app(con, **kw):
    s = Settings(session_secret="t" * 32, allowed_tailscale_login=LOGIN, **kw)
    return create_app(s, con, router=None)


# --- 라우트 감사 (fastapi 신버전의 _IncludedRouter 를 풀어서 모든 엔드포인트를 본다) ---

KNOWN_CLASSES = {"APIRoute", "Route", "APIWebSocketRoute", "WebSocketRoute", "StaticMount"}
DOC_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 본문을 만들 수 없을 때 422 를 허용하는 (method, path) - 비어 있어야 정상.
UNBUILDABLE_BODY_OK: set = set()
PUBLIC_MOUNTS: set = {"/static"}   # 가드 통과 후 공개인 정적 Mount (test_static_mount_is_safe 가 안전성 검증)
STATIC_CLASS = "StaticMount"


def is_public(path: str, mounts=frozenset()) -> bool:
    """정확히 공개 경로이거나, 등록된 Mount 경로(정확히 일치 또는 '<mount>/' 하위)."""
    if path in PUBLIC_AFTER_GUARD:
        return True
    return any(path == m or path.startswith(m.rstrip("/") + "/") for m in mounts)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


class Entry:
    def __init__(self, cls, methods, path, endpoint=None, dependant=None, body_params=()):
        self.cls, self.methods, self.path = cls, methods, path
        self.endpoint, self.dependant, self.body_params = endpoint, dependant, body_params


def _flatten(routes, prefix=""):
    """(Entry...) 반환. 모르는 라우트 클래스는 cls 이름 그대로 담겨 KNOWN_CLASSES 검사에서 실패한다."""
    for r in routes:
        name = type(r).__name__
        if name == "_IncludedRouter":
            yield from _flatten(r.effective_route_contexts(), prefix)
            continue
        if name == "_EffectiveRouteContext":
            orig = r.starlette_route or r.original_route
            cls = type(orig).__name__
            dep = getattr(r, "dependant", None)
            yield Entry(cls, set(getattr(r, "methods", None) or {"WEBSOCKET" if "WebSocket" in cls else "GET"}),
                        prefix + r.path, r.endpoint, dep, getattr(dep, "body_params", ()))
            continue
        if name == "Mount" and type(getattr(r, "app", None)).__name__ == "StaticFiles":
            yield Entry(STATIC_CLASS, {"GET"}, prefix + r.path)
            continue
        if name in ("Mount", "Host"):
            sub = getattr(r, "routes", None)
            yield Entry(name, {"GET"}, prefix + getattr(r, "path", getattr(r, "host", "?")))
            if sub:
                yield from _flatten(sub, prefix + getattr(r, "path", ""))
            continue
        dep = getattr(r, "dependant", None)
        methods = set(getattr(r, "methods", None) or {"WEBSOCKET" if "WebSocket" in name else "GET"})
        yield Entry(name, methods, prefix + r.path, getattr(r, "endpoint", None), dep, getattr(dep, "body_params", ()))


def endpoints(app):
    return [e for e in _flatten(app.routes) if e.path not in DOC_PATHS]


def unknown_routes(app, extra_known=frozenset()):
    return sorted((e.cls, e.path) for e in endpoints(app) if e.cls not in KNOWN_CLASSES | set(extra_known))


def mount_paths(app):
    return {e.path for e in endpoints(app) if e.cls == STATIC_CLASS and e.path in PUBLIC_MOUNTS}


def _is_owner_ref(node) -> bool:
    return (isinstance(node, ast.Name) and node.id == "require_owner") or \
        (isinstance(node, ast.Attribute) and node.attr == "require_owner")


def has_owner_check(ep, dependant=None) -> bool:
    """정적 검사(AST): 핸들러가 정확히 `require_owner` 를 호출하거나 Depends(require_owner) 를 쓴다.
    주석/문자열/`require_owner_soft` 같은 비슷한 이름은 인정하지 않는다."""
    if ep is not None:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(ep)))
        except (OSError, TypeError, SyntaxError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _is_owner_ref(node.func):
                    return True
                fn = node.func
                if (isinstance(fn, ast.Name) and fn.id == "Depends") or \
                        (isinstance(fn, ast.Attribute) and fn.attr == "Depends"):
                    if any(_is_owner_ref(a) for a in node.args) or any(_is_owner_ref(k.value) for k in node.keywords):
                        return True
    stack = list(getattr(dependant, "dependencies", []) or [])
    while stack:
        d = stack.pop()
        if d.call is auth.require_owner:
            return True
        stack.extend(d.dependencies)
    return False


def unprotected(app):
    mounts = mount_paths(app)
    return sorted({(m, e.path) for e in endpoints(app) if e.cls in KNOWN_CLASSES for m in e.methods
                   if not is_public(e.path, mounts) and not has_owner_check(e.endpoint, e.dependant)})


def _sample(schema, defs, depth=0):
    if "$ref" in schema:
        return _sample(defs[schema["$ref"].rsplit("/", 1)[-1]], defs, depth)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            opts = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _sample(opts[0], defs, depth)
    if "allOf" in schema:
        return _sample(schema["allOf"][0], defs, depth)
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props, req = schema.get("properties", {}), set(schema.get("required", []))
        return {k: _sample(v, defs, depth + 1) for k, v in props.items() if k in req}
    return {"string": "x", "integer": 1, "number": 1, "boolean": True, "array": []}.get(t, "x")


def build_body(entry):
    """라우트 요청 모델에서 유효한 JSON 본문을 만든다. 만들 수 없으면 None."""
    params = list(entry.body_params)
    if not params:
        return {}
    try:
        from pydantic import TypeAdapter
        out = {}
        for p in params:
            ad = TypeAdapter(p.field_info.annotation or p.field_info.__class__)
            sch = ad.json_schema(ref_template="#/$defs/{model}")
            out[p.name] = _sample(sch, sch.get("$defs", {}))
        if len(params) == 1 and not getattr(params[0].field_info, "embed", False):
            return out[params[0].name]
        return out
    except Exception:
        return None


def probe(client, e, m, headers):
    """세션 없는 요청 전송. 유효 본문을 붙인다. (status, body_was_built)"""
    path = concrete(e.path)
    if m in BODY_METHODS:
        body = build_body(e)
        if body is None:
            return client.request(m, path, headers=headers), False
        return client.request(m, path, headers=headers, json=body) if e.body_params else \
            client.request(m, path, headers=headers), True
    return client.request(m, path, headers=headers), True


def dynamic_failures(app, headers=TS, client_kw=None, skip=frozenset()):
    """client_kw: TestClient 인자(예: 루프백 클라이언트). skip: 검사에서 뺄 (메서드, 경로)."""
    c = TestClient(app, follow_redirects=False, **(client_kw or {}))
    mounts = mount_paths(app)
    bad = []
    for e in endpoints(app):
        if e.cls not in {"APIRoute", "Route"} or is_public(e.path, mounts):
            continue
        for m in sorted(e.methods - SKIP_METHODS):
            if (m, e.path) in skip:
                continue
            r, built = probe(c, e, m, headers)
            if r.status_code in (401, 403):
                continue
            if r.status_code == 422 and not built and (m, e.path) in UNBUILDABLE_BODY_OK:
                continue
            bad.append((m, e.path, r.status_code, built))
    return bad


def test_audit_sees_real_routes(con):
    """감사가 헛돌지 않는지(과거: _IncludedRouter 때문에 /health 외에 아무것도 못 봤다)."""
    paths = {e.path for e in endpoints(make_app(con))}
    assert len(paths) > 40
    assert {"/health", "/", "/api/nonce", "/auth/google/callback"} <= paths


def test_every_route_class_known(con):
    assert unknown_routes(make_app(con)) == []


HARDENING_BASELINE_PATHS = 60   # 열거된 경로 수의 하한(이후 추가분은 더해진다)
STATIC_ALLOWED_FILES = {"app.css", "tokens.css"}
STATIC_ALLOWED_EXT = {".css"}


def test_audit_enumerates_routes(con):
    paths = {e.path for e in endpoints(make_app(con))}
    print("enumerated paths:", len(paths))
    assert "/static" in paths
    assert len(paths) >= HARDENING_BASELINE_PATHS


def _static_mount(app):
    ms = [r for r in app.router.routes if type(r).__name__ == "Mount" and r.path == "/static"]
    assert len(ms) == 1
    return ms[0]


def test_static_mount_is_safe(con):
    from starlette.staticfiles import StaticFiles
    app = make_app(con)
    m = _static_mount(app)
    # (a) 경로
    assert m.path == "/static" and isinstance(m.app, StaticFiles)
    # (b) 디렉터리와 내용
    repo = Path(main.__file__).resolve().parent.parent
    expected = (repo / "pa" / "web" / "static").resolve()
    d = Path(m.app.directory).resolve()
    assert d == expected
    files = sorted(d.rglob("*"))
    assert files, "static dir empty"
    for f in files:
        assert f.is_file() and f.parent == d, f"unexpected entry {f}"
        assert f.name in STATIC_ALLOWED_FILES and f.suffix in STATIC_ALLOWED_EXT, f"non-css file in static: {f.name}"
        # (c) 심볼릭 링크 없음
        assert not f.is_symlink()
    assert not d.is_symlink() and not Path(m.app.directory).is_symlink()
    # (c) html=False, follow_symlink 비활성
    assert m.app.all_directories and m.app.html is False
    assert getattr(m.app, "follow_symlink", False) is False
    # (f) 공개는 정확히 '/static/' 접두만
    assert is_public("/static/app.css", {"/static"}) and not is_public("/staticx", {"/static"})
    assert mount_paths(app) == {"/static"}
    assert not is_public("/static-admin", mount_paths(app)) and not is_public("/", mount_paths(app))
    # (d)(e) 경로 탈출 시도
    secret = "SECRETVALUE"
    env = repo / ".env"
    probes = ["/static/../.env", "/static/%2e%2e/.env", "/static/..%2f.env", "/static//etc/passwd",
              "/static/app.css/", "/static/app.css/../../../.env", "/static/%2e%2e%2f%2e%2e%2f.env",
              "/static/..%5c.env", "/static/app.css%00", "/static/", "/static/.env", "/static/../../etc/passwd"]
    for headers in (TS, {}):
        c = TestClient(app, follow_redirects=False)
        for u in probes:
            r = c.get(u, headers=headers)
            if r.status_code == 200:   # 끝 슬래시 변형은 같은 css 를 그대로 줄 수 있다(비밀 아님) - 내용이 css 파일과 동일할 때만 허용
                assert headers and u == "/static/app.css/" and r.content == (d / "app.css").read_bytes(), (u, headers)
                continue
            assert r.status_code in (400, 403, 404, 307, 308), (u, headers, r.status_code)
            assert "root:" not in r.text and secret not in r.text and "PA_" not in r.text, (u, headers)
            if not headers:
                assert r.status_code == 403, (u, r.status_code)
    # (e) 헤더 없이 /static
    c = TestClient(app, follow_redirects=False)
    assert c.get("/static").status_code == 403
    assert c.get("/static/app.css").status_code == 403
    # 2) 동적 감사: 헤더 있으면 200, 쓰기 메서드 405
    r = c.get("/static/app.css", headers=TS)
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    for meth in ("POST", "PUT", "PATCH", "DELETE"):
        assert c.request(meth, "/static/app.css", headers=TS).status_code == 405, meth


def test_every_route_requires_owner_statically(con):
    assert unprotected(make_app(con)) == []


def test_no_owner_session_never_passes_dynamic(con):
    assert dynamic_failures(make_app(con)) == []


# --- 감사 자가 테스트: 속이는 사례를 모두 잡아야 한다 ---

def test_detector_fires_on_new_unprotected_route(con):
    app = make_app(con)

    @app.get("/zz-new-leak")
    async def leak():
        return {"secret": 1}

    assert ("GET", "/zz-new-leak") in unprotected(app)
    assert any(f[1] == "/zz-new-leak" for f in dynamic_failures(app))


def test_detector_ignores_comment_only_mention(con):
    app = make_app(con)

    @app.get("/zz-comment")
    async def leak():
        # require_owner(request) 를 나중에 붙일 것
        s = "require_owner"
        return {"s": s}

    assert ("GET", "/zz-comment") in unprotected(app)


def test_detector_rejects_similar_name(con):
    app = make_app(con)

    def require_owner_soft(request: Request):
        return True

    @app.get("/zz-soft")
    async def leak(request: Request):
        require_owner_soft(request)
        return {}

    @app.get("/zz-soft-dep")
    async def leak2(_=Depends(require_owner_soft)):
        return {}

    got = unprotected(app)
    assert ("GET", "/zz-soft") in got and ("GET", "/zz-soft-dep") in got


def test_detector_accepts_real_forms(con):
    app = make_app(con)

    @app.get("/zz-ok-call")
    async def ok1(request: Request):
        auth.require_owner(request)
        return {}

    @app.get("/zz-ok-dep")
    async def ok2(_=Depends(auth.require_owner)):
        return {}

    got = unprotected(app)
    assert ("GET", "/zz-ok-call") not in got and ("GET", "/zz-ok-dep") not in got


def test_public_prefix_needs_slash_boundary():
    mounts = {"/static"}
    assert is_public("/static", mounts) and is_public("/static/app.css", mounts)
    assert not is_public("/staticdump", mounts) and not is_public("/static-admin", mounts)
    assert not is_public("/staticdump")   # 마운트 없으면 공개 아님


def test_post_with_required_body_cannot_hide_missing_auth(con):
    from pydantic import BaseModel

    class Payload(BaseModel):
        name: str
        count: int

    app = make_app(con)

    @app.post("/zz-post")
    async def leak(p: Payload):   # require_owner 없음
        return {"ok": p.name}

    e = next(e for e in endpoints(app) if e.path == "/zz-post")
    assert build_body(e) == {"name": "x", "count": 1}
    assert ("POST", "/zz-post") in unprotected(app)
    assert any(f[1] == "/zz-post" and f[2] == 200 for f in dynamic_failures(app))   # 422 가 아니라 200 으로 드러남


def test_unbuildable_body_422_counts_as_failure(con, monkeypatch):
    app = make_app(con)

    @app.post("/zz-nobody")
    async def leak(request: Request):
        raise HTTPException(422, "bad body")   # 인증 전 검증 오류가 인증 누락을 가리는 상황

    monkeypatch.setattr(sys.modules[__name__], "build_body", lambda e: None)
    assert any(f[1] == "/zz-nobody" for f in dynamic_failures(app))


def test_unknown_route_classes_fail(con):
    from starlette.routing import Host, Mount, WebSocketRoute
    app = make_app(con)
    app.router.routes.append(Mount("/zz-mount", app=lambda s, r, se: None))
    app.router.routes.append(Host("zz.example", app=lambda s, r, se: None))
    got = unknown_routes(app)
    assert ("Mount", "/zz-mount") in got
    assert any(c == "Host" for c, _ in got)


def test_websocket_route_enumerated_and_audited(con):
    from fastapi import WebSocket
    app = make_app(con)

    @app.websocket("/zz-ws")
    async def ws(w: WebSocket):
        await w.accept()
        await w.send_text("secret")

    es = [e for e in endpoints(app) if e.path == "/zz-ws"]
    assert es and es[0].cls in KNOWN_CLASSES
    assert ("WEBSOCKET", "/zz-ws") in unprotected(app)


def test_unknown_class_detector_flags_foreign_route(con):
    from starlette.routing import BaseRoute

    class Weird(BaseRoute):
        path = "/zz-weird"
        def matches(self, scope): return None
        async def handle(self, scope, receive, send): ...
        def url_path_for(self, name, **p): ...

    app = make_app(con)
    app.router.routes.append(Weird())
    assert ("Weird", "/zz-weird") in unknown_routes(app)


def test_without_tailscale_header_everything_403_except_health(con):
    app = make_app(con)
    c = TestClient(app, follow_redirects=False)
    for e in endpoints(app):
        if e.cls not in {"APIRoute", "Route"}:
            continue
        for m in sorted(e.methods - SKIP_METHODS):
            r = c.request(m, concrete(e.path), json={} if m in BODY_METHODS else None)
            if e.path == "/health":
                assert r.status_code == 200
            else:
                assert r.status_code == 403, (m, e.path, r.status_code)


def test_wrong_tailscale_login_403(con):
    c = TestClient(make_app(con))
    assert c.get("/", headers={"Tailscale-User-Login": "evil@example.com"}).status_code == 403


# --- Host 허용목록 ---

def test_host_allowlist_rejects_rebinding_with_forged_header(con):
    c = TestClient(make_app(con))
    r = c.get("/", headers={**TS, "Host": "evil.example"})
    assert r.status_code == 403 and r.json() == {"detail": "forbidden"}
    assert c.get("/health", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/health", headers={"Host": "evil.example:8010"}).status_code == 403


def test_host_allowlist_allows_loopback_names_with_port(con):
    c = TestClient(make_app(con))
    assert c.get("/health", headers={"Host": "127.0.0.1:8010"}).status_code == 200
    assert c.get("/health", headers={"Host": "localhost:8010"}).status_code == 200


def test_production_defaults_do_not_include_testserver(monkeypatch):
    monkeypatch.delenv("PA_ALLOWED_HOSTS")
    assert Settings().host_allowlist == {"127.0.0.1", "localhost"}


def test_allowed_hosts_env_adds_tailnet_name_but_keeps_loopback(monkeypatch):
    monkeypatch.setenv("PA_ALLOWED_HOSTS", "pc.tail1234.tailnet.example, other.tailnet.example")
    assert Settings().host_allowlist == {"127.0.0.1", "localhost", "pc.tail1234.tailnet.example", "other.tailnet.example"}


def test_tailnet_host_allowed_when_configured(con, monkeypatch):
    monkeypatch.setenv("PA_ALLOWED_HOSTS", "pc.tail1234.tailnet.example")
    c = TestClient(make_app(con))
    assert c.get("/health", headers={"Host": "pc.tail1234.tailnet.example"}).status_code == 200
    assert c.get("/health", headers={"Host": "evil.example"}).status_code == 403


# --- docs 비활성 ---

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_disabled_in_production(con, path):
    c = TestClient(make_app(con))
    assert c.get(path, headers=TS).status_code == 404
    assert c.get(path).status_code == 403


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_enabled_only_in_dev(con, path):
    c = TestClient(make_app(con, dev_skip_tailscale=True))
    assert c.get(path).status_code == 200


# --- 시작 거부/경고 ---

def test_dev_mode_refuses_non_loopback_bind():
    s = Settings(dev_skip_tailscale=True)
    with pytest.raises(SystemExit):
        main.check_exposure(s, "0.0.0.0")
    main.check_exposure(s, "127.0.0.1")
    main.check_exposure(Settings(dev_skip_tailscale=False), "0.0.0.0")   # 운영 가드는 별도(grep 테스트)


def test_build_warns_dev_mode(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "true")
    with caplog.at_level("WARNING", logger="pa"):
        main.build()
    assert [r.getMessage() for r in caplog.records].count("DEV MODE: Tailscale guard disabled") == 1


def test_build_warns_empty_login_in_production(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "false")
    monkeypatch.setenv("PA_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("PA_ALLOWED_TAILSCALE_LOGIN", "")
    with caplog.at_level("INFO", logger="pa"):
        main.build()
    assert any("이 PC(루프백)에서만" in r.getMessage() for r in caplog.records)


def test_main_binds_loopback_only():
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "0.0.0.0" not in src
    assert main.BIND_HOST == "127.0.0.1"
    run_calls = re.findall(r"uvicorn\.run\((.*)\)", src)
    assert run_calls and all("host=BIND_HOST" in c for c in run_calls)
    assert 'host="' not in "".join(run_calls)


# --- 세션 쿠키 / 헤더 ---

def _owner_cookie(secret: str) -> str:
    data = base64.b64encode(_json.dumps({"owner": True}).encode())
    return TimestampSigner(secret).sign(data).decode()


def _login(app, base="https://testserver"):
    """소유자 세션(서명 쿠키)을 가진 클라이언트. 로그인 라우트는 Stage 5(QR 기기 등록)에서 생기므로 쿠키를 직접 만든다."""
    c = TestClient(app, base_url=base, follow_redirects=False)
    c.cookies.set("pa_session", _owner_cookie("t" * 32))
    return c, None


def _app_with_session_route(con, **kw):
    """세션 쿠키를 심는 임시 라우트가 붙은 앱(쿠키 속성 시험용)."""
    app = make_app(con, **kw)

    @app.get("/zz-session")
    async def zz(request: Request):
        request.session["owner"] = True
        return {"ok": 1}

    return app


def test_session_cookie_flags_production(con):
    c = TestClient(_app_with_session_route(con), base_url="https://testserver", follow_redirects=False)
    r = c.get("/zz-session", headers=TS)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "pa_session=" in cookie


def test_session_cookie_secure_off_only_in_dev(con):
    app = _app_with_session_route(con, dev_skip_tailscale=True)
    c = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = c.get("/zz-session")
    assert "secure" not in r.headers["set-cookie"].lower()
    assert "httponly" in r.headers["set-cookie"].lower()


def test_security_headers_present_on_all_responses(con):
    c = TestClient(make_app(con))
    for r in (c.get("/health"), c.get("/"), c.get("/", headers=TS)):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "base-uri 'self'" in csp
        assert "script-src" not in csp and "default-src" not in csp   # 인라인 스크립트 유지


def test_no_store_on_private_pages_and_json(con):
    app = make_app(con)
    c, _ = _login(app)
    for path in ("/", "/today", "/routine", "/api/routine/x"):
        r = c.get(path, headers=TS)
        assert r.headers.get("cache-control") == "no-store", (path, r.status_code)
    assert "cache-control" not in c.get("/health").headers


# --- 라운드1: 접속 상대 주소(loopback) 강제, pure ASGI, 비밀값 ---

import asyncio
import base64
import json as _json

from itsdangerous import TimestampSigner


def forged_cookie(secret: str = "dev-only-change-me") -> str:
    data = base64.b64encode(_json.dumps({"owner": True}).encode())
    return TimestampSigner(secret).sign(data).decode()


def asgi(app, path="/", *, client=("192.168.1.5", 50000), server=("192.168.1.9", 8010), headers=None,
         scope_type="http"):
    """TestClient 를 거치지 않고 ASGI 에 직접 scope 를 넣는다(실제 소켓 주소 위조 없이 상대 주소 시뮬레이션)."""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": scope_type, "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
             "client": client, "server": server}
    if scope_type == "websocket":
        scope.update(type="websocket", scheme="ws", subprotocols=[])
    sent, queue = [], ([{"type": "websocket.connect"}] if scope_type == "websocket" else [{"type": "http.request"}])

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    asyncio.run(app(scope, receive, send))
    return sent


def status_of(sent):
    first = sent[0]
    return first["status"] if first["type"] == "http.response.start" else ("ws", first["type"], first.get("code"))


ATTACK = {"Host": "127.0.0.1:8010", "Tailscale-User-Login": LOGIN, "Cookie": f"pa_session={forged_cookie()}"}


def dev_app(con, **kw):
    s = Settings(dev_skip_tailscale=True, allowed_tailscale_login=LOGIN, **kw)   # 기본 비밀값
    return create_app(s, con, router=None)


@pytest.mark.parametrize("path", ["/", "/api/drafts", "/health", "/auth/google/callback"])
@pytest.mark.parametrize("peer", ["192.168.1.5", "100.64.0.7", "10.0.0.2", "8.8.8.8", "fe80::1", "0.0.0.0"])
@pytest.mark.parametrize("dev", [True, False])
def test_non_loopback_server_gets_403_even_with_forged_everything(con, path, peer, dev):
    """LAN 에서 직접 들어온 연결: server(로컬 소켓 주소)가 LAN IP. client 를 loopback 으로 위조해도 거부."""
    app = dev_app(con) if dev else make_app(con)
    for client in ((peer, 4000), ("127.0.0.1", 4000)):
        sent = asgi(app, path, client=client, server=(peer, 8010),
                    headers={**ATTACK, "X-Forwarded-For": "127.0.0.1"})
        assert status_of(sent) == 403
        assert b"forbidden" in sent[1]["body"]


def test_missing_server_info_rejected(con):
    app = make_app(con)
    for server in (None, ()):
        sent = asgi(app, "/health", client=("127.0.0.1", 4000), server=server, headers={"Host": "127.0.0.1"})
        assert status_of(sent) == 403


def test_missing_client_info_rejected(con):
    app = make_app(con)
    for client in (None, ()):
        scope_sent = asgi(app, "/health", client=client, server=("127.0.0.1", 8010), headers={"Host": "127.0.0.1"})
        assert status_of(scope_sent) == 403


def test_non_loopback_server_address_rejected_even_if_client_is_loopback(con):
    """uvicorn --host 0.0.0.0 로 LAN 주소로 들어온 연결: server 가 LAN IP."""
    app = make_app(con)
    sent = asgi(app, "/health", client=("127.0.0.1", 4000), server=("192.168.1.9", 8010), headers={"Host": "127.0.0.1"})
    assert status_of(sent) == 403


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_peers_still_served(con, peer):
    app = make_app(con)
    sent = asgi(app, "/health", client=(peer, 4000), server=(peer, 8010), headers={"Host": "127.0.0.1:8010"})
    assert status_of(sent) == 200


def test_peer_allowlist_is_loopback_only_in_production(monkeypatch):
    monkeypatch.undo()   # conftest 의 테스트용 확장 제거
    assert auth.ALLOWED_PEERS == {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
    assert "testclient" not in auth.ALLOWED_PEERS


def test_testclient_rejected_without_test_extension(con, monkeypatch):
    app = make_app(con)
    monkeypatch.setattr(auth, "ALLOWED_PEERS", auth.LOOPBACK)
    assert TestClient(app).get("/health").status_code == 403   # ('testclient', 50000) 는 비루프백


def test_no_settings_switch_disables_client_guard():
    names = " ".join(Settings.model_fields)
    assert not re.search(r"trusted|client_host|peer|allow_remote|loopback", names)
    assert "ALLOWED_PEERS" not in Path(auth.__file__).read_text(encoding="utf-8").split("ALLOWED_PEERS = LOOPBACK")[0]


def test_forged_cookie_useless_in_dev_default_secret(con):
    """공격 프로브: 기본 비밀값으로 서명한 쿠키로 /life, /api/drafts 접근."""
    app = dev_app(con)
    c = TestClient(app, base_url="http://127.0.0.1:8010")
    for path in ("/", "/api/drafts"):
        r = c.get(path, headers={"Cookie": f"pa_session={forged_cookie()}"})
        assert r.status_code in (401, 403), (path, r.status_code)


def test_dev_mode_default_or_short_secret_is_randomised():
    a, b = Settings(dev_skip_tailscale=True), Settings(dev_skip_tailscale=True, session_secret="short")
    c = Settings(dev_skip_tailscale=True)
    assert a.session_secret != "dev-only-change-me" and len(a.session_secret) >= 32
    assert len(b.session_secret) >= 32 and b.session_secret != "short"
    assert a.session_secret != c.session_secret
    keep = "k" * 40
    assert Settings(dev_skip_tailscale=True, session_secret=keep).session_secret == keep


def test_build_refuses_weak_secret_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "false")
    monkeypatch.setenv("PA_SESSION_SECRET", "dev-only-change-me")
    with pytest.raises(SystemExit):
        main.build()


# --- websocket / pure ASGI ---

def test_guards_are_pure_asgi():
    import inspect as _i
    from starlette.middleware.base import BaseHTTPMiddleware
    for cls in (auth.ClientGuard, auth.HostAllowlist, auth.TailscaleGuard, auth.SecurityHeaders):
        assert not issubclass(cls, BaseHTTPMiddleware), cls
        assert _i.iscoroutinefunction(cls.__call__)


def _ws_app(con):
    from fastapi import WebSocket
    app = make_app(con)

    @app.websocket("/zz-ws-attack")
    async def ws(w: WebSocket):
        await w.accept()
        await w.send_text("secret")
        await w.close()

    return app


def test_websocket_wrong_host_without_tailscale_header_rejected(con):
    app = _ws_app(con)
    sent = asgi(app, "/zz-ws-attack", client=("127.0.0.1", 1), server=("127.0.0.1", 8010),
                headers={"Host": "evil.example"}, scope_type="websocket")
    assert status_of(sent) == ("ws", "websocket.close", 1008)
    sent = asgi(app, "/zz-ws-attack", client=("127.0.0.1", 1), server=("127.0.0.1", 8010),
                headers={"Host": "127.0.0.1:8010"}, scope_type="websocket")   # 호스트 OK, 헤더 없음
    assert status_of(sent) == ("ws", "websocket.close", 1008)


def test_websocket_non_loopback_server_rejected_even_with_header(con):
    app = _ws_app(con)
    sent = asgi(app, "/zz-ws-attack", client=("192.168.1.5", 1), server=("192.168.1.9", 8010),
                headers={"Host": "127.0.0.1", "Tailscale-User-Login": LOGIN}, scope_type="websocket")
    assert status_of(sent) == ("ws", "websocket.close", 1008)


def test_websocket_testclient_rejected_without_headers(con):
    from starlette.websockets import WebSocketDisconnect
    c = TestClient(_ws_app(con), base_url="http://evil.example")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/zz-ws-attack"):
            pass
    ok = TestClient(_ws_app(con))
    with ok.websocket_connect("/zz-ws-attack", headers=TS) as w:   # 가드를 통과하는 정상 경로(라우트 자체는 감사가 잡는다)
        assert w.receive_text() == "secret"


# --- 헤더: 3xx, 500 ---

def test_no_store_on_redirect_with_set_cookie_and_everything_but_health(con):
    app = _app_with_session_route(con)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    r = c.get("/zz-session", headers=TS)
    assert r.status_code == 200 and "set-cookie" in r.headers
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/zz-nothing", headers=TS).headers["cache-control"] == "no-store"   # 404
    assert c.get("/").headers["cache-control"] == "no-store"   # 403 가드 응답
    assert "cache-control" not in c.get("/health").headers


def test_security_headers_on_500_and_on_guard_rejects(con):
    app = make_app(con)

    @app.get("/zz-boom")
    async def boom():
        raise RuntimeError("boom")

    c = TestClient(app, raise_server_exceptions=False)
    for r in (c.get("/zz-boom", headers=TS), c.get("/zz-boom"), c.get("/", headers={"Host": "evil.example"})):
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors" in r.headers["content-security-policy"]
    assert c.get("/zz-boom", headers=TS).status_code == 500
    assert c.get("/zz-boom", headers=TS).headers["cache-control"] == "no-store"


# --- 시작 경고/설정 ---

def test_build_no_host_warning_when_configured(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("PA_ALLOWED_TAILSCALE_LOGIN", LOGIN)
    monkeypatch.setenv("PA_ALLOWED_HOSTS", "pc.tail1.tailnet.example")
    with caplog.at_level("WARNING", logger="pa"):
        main.build()
    assert not any("PA_ALLOWED_HOSTS" in r.getMessage() for r in caplog.records)


def test_settings_ignore_dotenv_in_tests(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PA_ALLOWED_TAILSCALE_LOGIN=from-dotenv@x\n")
    monkeypatch.chdir(tmp_path)
    assert Settings().allowed_tailscale_login == ""


def test_nothing_in_repo_binds_non_loopback():
    root = Path(__file__).resolve().parent.parent
    pat = re.compile(r"0\.0\.0\.0|--host[ =]|host\s*=\s*[\"'](?!127\.0\.0\.1|localhost)")
    hits = []
    for base in ("pa", "scripts"):
        for f in (root / base).rglob("*"):
            if f.suffix in {".py", ".sh", ".ps1", ".bat", ".cmd", ".toml", ".yaml", ".yml"} and f.is_file():
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if pat.search(line):
                        hits.append((str(f.relative_to(root)), i, line.strip()))
    for name in ("README.md", "pyproject.toml", "Makefile", "Dockerfile", "docker-compose.yml"):
        f = root / name
        if f.exists():
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if pat.search(line):
                    hits.append((name, i, line.strip()))
    assert hits == []


# --- fix round 2: tailscale serve 경유(프록시 헤더가 client 를 100.x 로 바꿔도) 소유자는 잠기지 않는다 ---

PC_HOST = "pc.tail1.tailnet.example"


def serve_app(con, monkeypatch):
    monkeypatch.setenv("PA_ALLOWED_HOSTS", f"127.0.0.1,localhost,testserver,{PC_HOST}")
    return make_app(con)


def test_four_peer_cases(con):
    app = make_app(con)
    h = {"Host": "127.0.0.1:8010"}
    # 1. 루프백 서버 + 루프백 client: 허용
    assert status_of(asgi(app, "/health", client=("127.0.0.1", 1), server=("127.0.0.1", 8010), headers=h)) == 200
    # 2. 루프백 서버 + 100.x client(프록시 헤더 재작성 결과): 허용(소유자 정상 경로)
    assert status_of(asgi(app, "/health", client=("100.64.0.7", 1), server=("127.0.0.1", 8010), headers=h)) == 200
    # 3. LAN 서버 + LAN client: 거부
    assert status_of(asgi(app, "/health", client=("192.168.1.5", 1), server=("192.168.1.9", 8010), headers=h)) == 403
    # 4. LAN 서버 + loopback 으로 위조된 client: 거부
    assert status_of(asgi(app, "/health", client=("127.0.0.1", 1), server=("192.168.1.9", 8010), headers=h)) == 403


def _proxied(app, path, client, server, xff):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    hdrs = {"Host": PC_HOST, "Tailscale-User-Login": LOGIN, "X-Forwarded-For": xff}
    return status_of(asgi(wrapped, path, client=client, server=server, headers=hdrs))


def test_tailscale_serve_through_proxy_headers_middleware_not_locked_out(con, monkeypatch):
    app = serve_app(con, monkeypatch)
    ok = (("127.0.0.1", 50000), ("127.0.0.1", 8010))
    assert _proxied(app, "/health", *ok, "100.64.0.7") == 200
    assert _proxied(app, "/", *ok, "100.64.0.7") == 401   # 가드 통과, 소유자 세션만 없음


def test_forged_xff_loopback_from_non_loopback_server_still_403(con, monkeypatch):
    app = serve_app(con, monkeypatch)
    assert _proxied(app, "/health", ("192.168.1.5", 50000), ("192.168.1.9", 8010), "127.0.0.1") == 403
    assert _proxied(app, "/", ("127.0.0.1", 50000), ("192.168.1.9", 8010), "127.0.0.1") == 403


def test_real_uvicorn_default_proxy_headers_not_403(con, monkeypatch):
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request
    import uvicorn
    app = serve_app(con, monkeypatch)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))  # 기본 proxy_headers
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started

        def get(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={
                "Host": PC_HOST, "Tailscale-User-Login": LOGIN, "X-Forwarded-For": "100.64.0.7"})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        assert get("/health") == 200
        assert get("/") == 401
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_main_disables_proxy_headers(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "build", lambda: "APP")
    monkeypatch.setattr(main.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    main.main()
    assert seen["proxy_headers"] is False and seen["forwarded_allow_ips"] == ""
    assert seen["host"] == main.BIND_HOST == "127.0.0.1"


# --- Origin / Sec-Fetch-Site (CSRF) 검사 ---

CSRF_MSG = "cross-origin request blocked"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _unsafe_routes(app):
    return [(m, e) for e in endpoints(app) if e.cls in {"APIRoute", "Route"} for m in sorted(e.methods & UNSAFE)]


def _csrf_probe(c, m, e, headers):
    return probe(c, e, m, headers)[0]


@pytest.mark.parametrize("origin", ["https://evil.com", "null", "https://testserver.evil.com", "http://testserver:9999"])
def test_every_unsafe_route_rejects_cross_origin(con, origin):
    app = make_app(con)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    routes = _unsafe_routes(app)
    assert len(routes) > 20
    bad = []
    for m, e in routes:
        r = _csrf_probe(c, m, e, {**TS, "Origin": origin})
        if r.status_code != 403 or r.json().get("detail") != CSRF_MSG:
            bad.append((m, e.path, r.status_code))
    assert bad == []


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_every_unsafe_route_rejects_sec_fetch_site(con, site):
    app = make_app(con)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    bad = [(m, e.path) for m, e in _unsafe_routes(app)
           if (_csrf_probe(c, m, e, {**TS, "Sec-Fetch-Site": site}).status_code != 403)]
    assert bad == []


@pytest.mark.parametrize("hdrs", [{}, {"Origin": "https://testserver"}, {"Sec-Fetch-Site": "same-origin"},
                                  {"Sec-Fetch-Site": "none"},
                                  {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}])
def test_same_origin_and_non_browser_pass_csrf_check(con, hdrs):
    app = make_app(con)
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    for m, e in _unsafe_routes(app):
        r = _csrf_probe(c, m, e, {**TS, **hdrs})
        assert not (r.status_code == 403 and CSRF_MSG in r.text), (m, e.path)


def test_cross_origin_nonce_blocked_even_with_owner_session(con):
    c, _ = _login(make_app(con))
    assert c.post("/api/nonce", headers={**TS, "Origin": "https://evil.com"}).status_code == 403
    assert c.post("/api/nonce", headers={**TS, "Origin": "null"}).status_code == 403
    assert c.post("/api/nonce", headers={**TS, "Origin": "https://testserver"}).status_code == 200
    assert c.post("/api/nonce", headers=TS).status_code == 200


def test_safe_methods_ignore_origin(con):
    c = TestClient(make_app(con), base_url="https://testserver")
    assert c.get("/health", headers={"Origin": "https://evil.com"}).status_code == 200


def test_csrf_guard_is_pure_asgi_and_wraps_late_routers(con):
    assert not issubclass(auth.CsrfGuard, __import__("starlette.middleware.base", fromlist=["x"]).BaseHTTPMiddleware)


def test_sec_fetch_site_is_decisive_for_origin_null(con):
    """Referrer-Policy 때문에 일부 브라우저는 같은 출처 POST 에 Origin: null 을 보낸다 → Sec-Fetch-Site: same-origin 이면 통과해야 한다."""
    c, _ = _login(make_app(con))
    ok = c.post("/api/nonce", headers={**TS, "Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200
    # 교차 사이트 페이지는 Sec-Fetch-Site 를 위조할 수 없다: cross-site 이면 Origin 이 같아 보여도 막는다
    bad = c.post("/api/nonce", headers={**TS, "Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"})
    assert bad.status_code == 403


def test_loopback_direct_is_owner_but_proxied_without_session_never_passes(con):
    """직접 루프백(이 PC의 브라우저)은 소유자. 프록시 헤더가 붙은 요청은 세션이 없으면 공개 목록 밖이 모두 401/403."""
    app = make_app(con)
    kw = {"base_url": "http://localhost:8010", "client": ("127.0.0.1", 50000)}
    c = TestClient(app, follow_redirects=False, **kw)
    assert c.post("/api/nonce").status_code == 200
    assert dynamic_failures(app, {**TS, "X-Forwarded-For": "100.64.0.7"}, kw) == []
    assert c.get("/static/app.css").status_code == 200 and c.get("/static/tokens.css").status_code == 200
