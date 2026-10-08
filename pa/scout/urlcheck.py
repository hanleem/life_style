from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_RE = re.compile(
    r"""<meta[^>]+property=["']og:title["'][^>]*content=["']([^"']*)["']""", re.I)
_LOGIN_HOSTS = ("accounts.google.com", "nid.naver.com")
MAX_BYTES = 65536
MAX_REDIRECTS = 5
_BLOCKED_SUFFIXES = (".local", ".internal", "." + "ts" + ".net", ".lan")   # 내부망·tailnet 이름은 가져오지 않는다
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_REDIRECT = (301, 302, 303, 307, 308)


def is_specific_item_url(url: str) -> bool:
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    path = p.path or "/"
    if host in ("youtu.be", "www.youtu.be"):
        return path.strip("/") != ""
    if host.endswith("youtube.com"):
        if path.startswith(("/@", "/channel/", "/c/", "/user/", "/playlist", "/results")):
            return False
        return path != "/"
    if host.endswith("spotify.com"):
        return path.startswith("/episode/")
    if host == "podcasts.apple.com":
        return "i" in parse_qs(p.query)
    if host == "search.naver.com":
        return False
    if host == "map.naver.com" and "/search" in path:
        return False
    if host.endswith("google.com") and path.startswith("/maps/search"):
        return False
    if re.match(r"^(www\.)?google\.[a-z.]+$", host) and path.startswith("/search"):
        return False
    return path != "/"


@dataclass
class UrlCheck:
    url: str
    final_url: str
    status: int | None
    title: str | None
    verify_status: Literal["page_ok", "page_failed", "unverified"]
    access: Literal["free", "login", "subscription", "region_locked", "unknown"]


def _extract_title(body: str) -> str | None:
    for rx in (_TITLE_RE, _OG_RE):
        m = rx.search(body)
        if m and m.group(1).strip():
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def _is_login(final_url: str) -> bool:
    p = urlsplit(final_url)
    return (p.hostname or "").lower() in _LOGIN_HOSTS or "/login" in p.path.lower()


def _ip_public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return False
    return not (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)


async def host_is_public(host: str) -> bool:
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return False
    try:
        return _ip_public(ipaddress.ip_address(host.split("%")[0]))
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), 5)
    except socket.gaierror:
        return False  # 해석 불가 호스트는 확인하지 않는다(다른 리졸버가 다른 답을 줄 수 있다)
    except (asyncio.TimeoutError, OSError):
        return False
    for info in infos:
        try:
            if not _ip_public(ipaddress.ip_address(str(info[4][0]).split("%")[0])):
                return False
        except ValueError:
            return False
    return True


def _peer_is_public(response: httpx.Response) -> bool:
    """연결 직후 실제 접속한 상대 IP 를 확인한다(DNS rebinding 방어). 확인할 정보가 없는 전송 계층
    (테스트용 mock 등)은 통과시키고, 있는데 공인 IP 가 아니면 거부한다."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return True
    try:
        addr = stream.get_extra_info("server_addr")
    except Exception:
        return False
    if not addr:
        return True
    try:
        return _ip_public(ipaddress.ip_address(str(addr[0]).split("%")[0]))
    except (ValueError, IndexError, TypeError):
        return False


async def _allowed(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme in ("http", "https") and bool(p.hostname) and await host_is_public(p.hostname)


async def check_url(client: httpx.AsyncClient, url: str) -> UrlCheck:
    unverified = UrlCheck(url, url, None, None, "unverified", "unknown")
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        if not await _allowed(cur):
            return unverified
        try:
            async with client.stream("GET", cur, follow_redirects=False) as r:
                if not _peer_is_public(r):
                    return unverified
                loc = r.headers.get("location")
                if r.status_code in _REDIRECT and loc:
                    cur = urljoin(cur, loc)
                    continue
                final = str(r.url)
                access = "login" if _is_login(final) else "unknown"
                if r.status_code == 200:
                    buf = b""
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) >= MAX_BYTES:
                            break
                    text = buf[:MAX_BYTES].decode("utf-8", errors="replace")
                    return UrlCheck(url, final, 200, _extract_title(text), "page_ok", access)
                status = "page_failed" if r.status_code >= 400 else "unverified"
                return UrlCheck(url, final, r.status_code, None, status, access)
        except httpx.HTTPError:
            return unverified
    return unverified


async def check_urls(client, urls, *, concurrency: int = 5) -> list[UrlCheck]:
    sem = asyncio.Semaphore(concurrency)

    async def one(u: str) -> UrlCheck:
        async with sem:
            return await check_url(client, u)

    return list(await asyncio.gather(*(one(u) for u in urls)))
