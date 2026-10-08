"""예약된 scout 잡 실행(나들이 추천)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pa.core import notify
from pa.scout import outing
from pa.scout.config import ScoutConfig


@dataclass
class ScoutCtx:
    con: Any
    router: Any
    http: Any
    rules: Any
    cfg: ScoutConfig


async def run_job(job: str, ctx: ScoutCtx, now: datetime) -> int:
    """잡 하나를 실행하고 만든 카드 수를 돌려준다."""
    if job != "scout_outing":
        return 0
    cards, _sr = await outing.build_outing(ctx.con, ctx.router, ctx.http, ctx.cfg, now, user_initiated=False)
    if cards:
        await notify.announce(ctx.con, "이번 주말 나들이 추천이 도착했어요", f"outing-{now.date().isoformat()}")
    return len(cards)
