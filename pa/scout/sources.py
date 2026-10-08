from __future__ import annotations

from typing import Iterable, Literal
from urllib.parse import urlsplit

SourceKind = Literal["official", "sns", "map", "review", "video", "other"]

_MAP = ("map.naver.com", "naver.me", "map.kakao.com", "place.map.kakao.com", "maps.app.goo.gl")
_REVIEW = ("blog.naver.com", "m.blog.naver.com", "tistory.com", "diningcode.com",
           "mangoplate.com", "cafe.naver.com")
_SNS = ("instagram.com", "facebook.com", "pf.kakao.com")
_VIDEO = ("youtube.com", "youtu.be")
_OFFICIAL_SUFFIX = (".go.kr", ".or.kr", ".ac.kr")


def _match(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source(url: str, official_domains: Iterable[str] = ()) -> SourceKind:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if _match(host, _MAP) or (_match(host, ("google.com",)) and path.startswith("/maps")):
        return "map"
    if _match(host, _REVIEW):
        return "review"
    if _match(host, _VIDEO):
        return "video"
    if _match(host, _SNS):
        return "sns"
    if host.endswith(_OFFICIAL_SUFFIX) or _match(host, {d.lower() for d in official_domains}):
        return "official"
    return "other"
