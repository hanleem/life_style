"""추천 카드(화면·알림에 보여 줄 한 건). 나들이 추천이 만든다."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Card:
    rec_id: str
    domain: str
    title: str
    url: str
    reason: str
    verify_label: str
    explore: bool = False
    meta: dict = field(default_factory=dict)
