import httpx
import pytest
import respx
from pa.scout.urlcheck import check_url, is_specific_item_url


@pytest.mark.parametrize("url,ok", [
    ("https://www.youtube.com/watch?v=abc123", True),
    ("https://youtu.be/abc123", True),
    ("https://www.youtube.com/@calmkorea", False),
    ("https://www.youtube.com/channel/UCxyz", False),
    ("https://www.youtube.com/playlist?list=PL1", False),
    ("https://www.youtube.com/results?search_query=명상", False),
    ("https://open.spotify.com/episode/123", True),
    ("https://open.spotify.com/show/123", False),
    ("https://podcasts.apple.com/kr/podcast/x/id1?i=100", True),
    ("https://podcasts.apple.com/kr/podcast/x/id1", False),
    ("https://search.naver.com/search.naver?query=x", False),
    ("https://example.com/", False),
    ("ftp://example.com/a", False),
])
def test_specific_item(url, ok):
    assert is_specific_item_url(url) is ok


@respx.mock
async def test_check_ok_with_title():
    respx.get("https://a.test/v/1").mock(return_value=httpx.Response(200, text="<html><title> 8분 호흡 </title></html>"))
    async with httpx.AsyncClient(follow_redirects=True) as c:
        r = await check_url(c, "https://a.test/v/1")
    assert r.verify_status == "page_ok" and r.title == "8분 호흡" and r.status == 200


@respx.mock
async def test_check_404_and_timeout():
    respx.get("https://a.test/gone").mock(return_value=httpx.Response(404))
    respx.get("https://a.test/slow").mock(side_effect=httpx.ConnectTimeout("t"))
    async with httpx.AsyncClient(follow_redirects=True) as c:
        assert (await check_url(c, "https://a.test/gone")).verify_status == "page_failed"
        assert (await check_url(c, "https://a.test/slow")).verify_status == "unverified"


@respx.mock
async def test_login_redirect_marks_login():
    respx.get("https://a.test/v/2").mock(return_value=httpx.Response(302, headers={"location": "https://a.test/login?next=/v/2"}))
    respx.get("https://a.test/login?next=/v/2").mock(return_value=httpx.Response(200, text="<title>로그인</title>"))
    async with httpx.AsyncClient(follow_redirects=True) as c:
        r = await check_url(c, "https://a.test/v/2")
    assert r.access == "login" and r.final_url.startswith("https://a.test/login")


# ---- fix round 1: DNS rebinding hardening ----
import asyncio  # noqa: E402
import socket  # noqa: E402


class _Net:
    def __init__(self, ip):
        self.ip = ip

    def get_extra_info(self, name):
        return (self.ip, 443) if name == "server_addr" else None


class _T(httpx.AsyncBaseTransport):
    def __init__(self, ip):
        self.ip, self.calls = ip, 0

    async def handle_async_request(self, request):
        self.calls += 1
        ext = {"network_stream": _Net(self.ip)} if self.ip else {}
        return httpx.Response(200, text="<title>x</title>", extensions=ext)


async def test_peer_ip_private_after_connect_is_unverified():
    t = _T("10.0.0.7")   # 이름은 공인 IP 로 풀렸지만 실제 접속은 사설 IP(rebinding)
    async with httpx.AsyncClient(transport=t, trust_env=False) as c:
        r = await check_url(c, "https://rebind.test/x")
    assert t.calls == 1 and r.verify_status == "unverified" and r.title is None


async def test_peer_ip_public_is_ok_and_loopback_mapped_is_not():
    async with httpx.AsyncClient(transport=_T("93.184.216.34")) as c:
        assert (await check_url(c, "https://ok.test/x")).verify_status == "page_ok"
    async with httpx.AsyncClient(transport=_T("::ffff:127.0.0.1")) as c:
        assert (await check_url(c, "https://ok.test/x")).verify_status == "unverified"


async def test_gaierror_is_not_public(monkeypatch):
    from pa.scout.urlcheck import host_is_public
    loop = asyncio.get_running_loop()

    async def boom(*a, **k):
        raise socket.gaierror(-2, "nx")

    monkeypatch.setattr(loop, "getaddrinfo", boom)
    assert await host_is_public("nx.example") is False
    async with httpx.AsyncClient(transport=_T("93.184.216.34")) as c:
        t = c._transport
        assert (await check_url(c, "https://nx.example/x")).verify_status == "unverified" and t.calls == 0


def test_main_http_client_ignores_env_proxies():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "pa" / "main.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src
