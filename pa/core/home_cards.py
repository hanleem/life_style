"""홈 화면의 관심사 카드. 고른 관심사의 카드를 위쪽에 먼저 보여 주고, 안 고른 것은 숨긴다(설정에서 켠다).

카드가 있는 관심사만 IMPLEMENTED 에 둔다. 새 카드는 템플릿 `_card_<관심사>.html` 과 data() 의 항목만 더하면 된다.
(아이 교육 = 6단계, 가족 건강·생활비 = 4단계에서 추가)
"""
import logging

log = logging.getLogger("pa.home_cards")

IMPLEMENTED = ("housekeeping", "outing")


def visible(prof: dict | None) -> list[str]:
    """고른 관심사 중 카드가 있는 것(고른 순서)."""
    return [i for i in (prof or {}).get("interests") or [] if i in IMPLEMENTED]


def _outing(con) -> dict:
    n = con.execute("SELECT COUNT(*) FROM recommendation WHERE domain='outing'").fetchone()[0]
    return {"count": n}


def _housekeeping(con) -> dict:
    from pa.core import pantry, supplies
    wanted = [o["name"] for o in supplies.list_orders(con) if o["status"] == "wanted"]
    return {"wanted": wanted, "low": [s["name"] for s in pantry.low_staples(con)]}


_DATA = {"outing": _outing, "housekeeping": _housekeeping}


def data(con, keys: list[str]) -> dict[str, dict]:
    """카드별 표시 자료. 하나가 실패해도 그 카드만 빈 자료로 그린다."""
    out = {}
    for k in keys:
        try:
            out[k] = _DATA[k](con)
        except Exception as e:  # noqa: BLE001 - 홈 화면은 어떤 카드가 실패해도 그려져야 한다
            log.warning("home card %s failed (%s)", k, type(e).__name__)
            out[k] = {}
    return out
