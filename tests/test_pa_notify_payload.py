import pytest
from pa.core.notify import ALLOWED, render, send_notify, unread


def test_allowed_set_matches_spec():
    assert ALLOWED == {"approval_request": "/approvals",
                       "briefing_morning": "/", "briefing_evening": "/", "decision_needed": "/",
                       "system": "/settings/health",
                       "routine_supply": "/routine", "routine_close": "/routine/evening", "reminder": "/",
                       "meal_plan": "/meals"}
    assert render("routine_supply", 3) == "이번 주 주문 확인"
    assert render("routine_close", 3) == "하루 마무리 시간입니다"
    assert render("reminder", 3) == "챙길 일이 있습니다"
    assert render("meal_plan", 1) == "이번 주 식단이 나왔어요"
    assert render("approval_request", 2) == "승인 요청 2건"


async def test_rejects_unknown_kind(con):
    with pytest.raises(ValueError):
        await send_notify(con, "free_text", 1, "x")


async def test_dedupe(con):
    assert await send_notify(con, "approval_request", 1, "appr:abc") is True
    assert await send_notify(con, "approval_request", 1, "appr:abc") is False
    got = unread(con)
    assert len(got) == 1 and set(got[0]) == {"id", "at", "kind", "title", "href"}
    assert got[0]["title"] == "승인 요청 1건" and got[0]["href"] == "/approvals"
