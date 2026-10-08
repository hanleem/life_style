"""생활 루틴 설정값과 제안(core.db). 물품 도착 일수 제안에 쓴다."""
import json
import re
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pa.core import coach_copy
from pa.core.errors import NotFound
from pa.db import transactional
from pa.ids import new_id

KST = ZoneInfo("Asia/Seoul")
REEXPOSE_DAYS = 14
CORE_KEYS: dict = {}   # 사용자 설정 키(지금은 없음). 물품 도착 일수 제안만 LEAD_PREFIX 키로 다룬다
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
LEAD_PREFIX = "supply_lead:"


def _iso(now):
    from datetime import timezone
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware datetime이 필요합니다")
    return now.astimezone(timezone.utc).isoformat()


def _validate(key: str, value):
    if key not in CORE_KEYS:
        raise ValueError(f"허용되지 않는 키: {key}")
    typ, lo, hi, _ = CORE_KEYS[key]
    if typ is bool:
        if not isinstance(value, bool):
            raise ValueError("true/false 값이 필요합니다")
    elif typ is int:
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{lo}~{hi} 사이 정수가 필요합니다")
    else:
        if not isinstance(value, str) or not _HHMM.match(value):
            raise ValueError("HH:MM 형식이 필요합니다")
    return value


def _validate_any(con, key: str, value):
    if key.startswith(LEAD_PREFIX):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 14:
            raise ValueError("도착 일수는 0~14 사이 정수입니다")
        from pa.core import supply
        supply.get_item(con, key[len(LEAD_PREFIX):])
        return value
    return _validate(key, value)


def get_value(con, key: str):
    if key not in CORE_KEYS:
        raise ValueError(f"허용되지 않는 키: {key}")
    row = con.execute("SELECT value_json FROM routine_plan WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else CORE_KEYS[key][3]


def set_value(con, key: str, value, *, source: Literal["user", "coach_accepted"], now) -> None:
    if source not in ("user", "coach_accepted"):
        raise ValueError("source")
    _validate(key, value)
    ts = _iso(now)
    con.execute(
        "INSERT INTO routine_plan(key,value_json,source,effective_from,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, source=excluded.source, "
        "effective_from=excluded.effective_from, updated_at=excluded.updated_at",
        (key, json.dumps(value), source, now.astimezone(KST).date().isoformat(), ts),
    )


def propose(con, key: str, value, *, basis: str, now) -> dict | None:
    _validate_any(con, key, value)
    vj = json.dumps(value)
    cutoff = _iso(now - timedelta(days=REEXPOSE_DAYS))
    seen = con.execute(
        "SELECT 1 FROM routine_proposal WHERE key=? AND value_json=? AND shown_at > ?",
        (key, vj, cutoff),
    ).fetchone()
    if seen:
        return None
    pid = new_id()
    con.execute(
        "INSERT INTO routine_proposal(id,key,value_json,basis,shown_at) VALUES(?,?,?,?,?)",
        (pid, key, vj, basis, _iso(now)),
    )
    return {"id": pid, "key": key, "value": value, "basis": basis,
            "actions": coach_copy.PROPOSAL_ACTIONS}


@transactional
def respond(con, proposal_id: str, response: Literal["accepted", "edited", "passed"], *,
            value=None, now) -> None:
    if response not in ("accepted", "edited", "passed"):
        raise ValueError("response")
    row = con.execute(
        "SELECT key,value_json,response FROM routine_proposal WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise NotFound(proposal_id)
    key, vj, done = row
    if done is not None:
        raise ValueError("이미 응답한 제안입니다")
    if response != "passed":
        if response == "edited":
            if value is None:
                raise ValueError("고친 값이 필요합니다")
            new, source = value, "user"
        else:
            new, source = json.loads(vj), "coach_accepted"
        _validate_any(con, key, new)
        if key.startswith(LEAD_PREFIX):
            from pa.core import supply
            supply.update_item(con, key[len(LEAD_PREFIX):], lead_days=new)
        else:
            set_value(con, key, new, source=source, now=now)
    con.execute(
        "UPDATE routine_proposal SET response=?, responded_at=? WHERE id=?",
        (response, _iso(now), proposal_id),
    )


def open_proposals(con, *, now) -> list[dict]:
    cutoff = _iso(now - timedelta(days=REEXPOSE_DAYS))
    rows = con.execute(
        "SELECT id,key,value_json,basis FROM routine_proposal "
        "WHERE response IS NULL AND shown_at > ? ORDER BY shown_at", (cutoff,)
    ).fetchall()
    return [{"id": r[0], "key": r[1], "value": json.loads(r[2]), "basis": r[3],
             "actions": coach_copy.PROPOSAL_ACTIONS} for r in rows]
