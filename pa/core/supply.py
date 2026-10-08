"""Meal-supply engine: estimated remaining meals, reorder point R = d*(L+S), reminders."""
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pa.core import coach_copy, routine_plan
from pa.core.errors import NotFound
from pa.db import atomic, get_cursor, set_cursor, transactional
from pa.ids import new_id

KST = ZoneInfo("Asia/Seoul")
MEAL_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
_HOLIDAYS: frozenset[date] = frozenset()   # 구글 '대한민국의 휴일' 캘린더에서 읽은 날짜(pa.core.holidays 가 채움)


def set_holidays(days) -> None:
    global _HOLIDAYS
    _HOLIDAYS = frozenset(days)


def is_meal_day(day: date) -> bool:
    """평일이면서 휴일 캘린더에 없는 날. 휴일은 주말과 같게 센다."""
    return day.weekday() in MEAL_WEEKDAYS and day not in _HOLIDAYS
STALE_DAYS = 7
LEAD_SAMPLE = 3
MIN_HORIZON_DAYS = 21
CATCHUP_DAYS = 14
CURSOR_KEY = "supply:consumed_through"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class SupplyItem:
    id: str
    label: str
    meals_per_workday: int
    meals_per_weekend_day: int
    lead_days: int
    safety_days: int
    order_url: str | None
    order_cutoff: str | None
    cutoff_asked: int
    paused_from: str | None
    paused_until: str | None
    active: int
    remaining: int
    last_reminded_at: str | None
    snoozed_until: str | None
    stale_prompted_at: str | None


def _iso(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware datetime이 필요합니다")
    return now.astimezone(timezone.utc).isoformat()


def _kst_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(KST).date()


def validate_https(url: str | None) -> str | None:
    if url is None or url == "":
        return None
    err = ValueError("https 링크만 저장할 수 있습니다")
    if any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") or c == "\\" for c in url):
        raise err
    try:
        p = urlparse(url)
        host = p.hostname
        p.port  # noqa: B018  (validates the port)
    except ValueError:
        raise err from None
    if p.scheme != "https" or not host or "@" in p.netloc or p.username or p.password:
        raise err
    if not host.isascii():
        raise err
    return "https" + url[5:]


def _validate_cutoff(c: str | None) -> str | None:
    if c is None or c == "":
        return None
    if not _HHMM.match(c):
        raise ValueError("마감 시간은 HH:MM 형식입니다")
    return c


def _check_range(name: str, v: int, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name}은(는) {lo}~{hi} 사이 정수입니다")
    return v


_SELECT = (
    "SELECT i.id,i.label,i.meals_per_workday,i.meals_per_weekend_day,i.lead_days,i.safety_days,"
    "i.order_url,i.order_cutoff,i.cutoff_asked,i.paused_from,i.paused_until,i.active,"
    "s.remaining,s.last_reminded_at,s.snoozed_until,s.stale_prompted_at "
    "FROM supply_item i JOIN supply_stock s ON s.item_id=i.id"
)


def add_item(con, *, label: str, meals_per_workday: int, meals_per_weekend_day: int = 0,
             lead_days: int = 1, safety_days: int = 1, order_url: str | None = None,
             order_cutoff: str | None = None, remaining: int = 0, now: datetime) -> str:
    if not label or not label.strip():
        raise ValueError("이름이 필요합니다")
    _check_range("평일 끼니", meals_per_workday, 1, 6)
    _check_range("주말 끼니", meals_per_weekend_day, 0, 6)
    _check_range("도착 일수", lead_days, 0, 14)
    _check_range("여유 일수", safety_days, 0, 14)
    _check_range("남은 끼니", remaining, 0, 1000)
    url = validate_https(order_url)
    cutoff = _validate_cutoff(order_cutoff)
    iid, ts = new_id(), _iso(now)
    with atomic(con):
        con.execute(
            "INSERT INTO supply_item(id,label,meals_per_workday,meals_per_weekend_day,lead_days,"
            "safety_days,order_url,order_cutoff,cutoff_asked,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (iid, label.strip(), meals_per_workday, meals_per_weekend_day, lead_days, safety_days,
             url, cutoff, 1 if cutoff else 0, ts),
        )
        con.execute("INSERT INTO supply_stock(item_id,remaining,updated_at) VALUES(?,?,?)",
                    (iid, remaining, ts))
    return iid


_UPDATABLE = {"label", "meals_per_workday", "meals_per_weekend_day", "lead_days",
              "safety_days", "order_url", "order_cutoff", "active"}


def update_item(con, item_id: str, **fields) -> None:
    get_item(con, item_id)
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"수정할 수 없는 항목: {sorted(bad)}")
    vals = dict(fields)
    if "label" in vals:
        if not str(vals["label"]).strip():
            raise ValueError("이름이 필요합니다")
        vals["label"] = str(vals["label"]).strip()
    if "meals_per_workday" in vals:
        _check_range("평일 끼니", vals["meals_per_workday"], 1, 6)
    if "meals_per_weekend_day" in vals:
        _check_range("주말 끼니", vals["meals_per_weekend_day"], 0, 6)
    if "lead_days" in vals:
        _check_range("도착 일수", vals["lead_days"], 0, 14)
    if "safety_days" in vals:
        _check_range("여유 일수", vals["safety_days"], 0, 14)
    if "order_url" in vals:
        vals["order_url"] = validate_https(vals["order_url"])
    if "order_cutoff" in vals:
        vals["order_cutoff"] = _validate_cutoff(vals["order_cutoff"])
        vals["cutoff_asked"] = 1
    if "active" in vals:
        vals["active"] = 1 if vals["active"] else 0
    if not vals:
        return
    cols = ", ".join(f"{k}=?" for k in vals)
    con.execute(f"UPDATE supply_item SET {cols} WHERE id=?", (*vals.values(), item_id))


def _active_item(con, item_id: str) -> SupplyItem:
    it = get_item(con, item_id)
    if not it.active:
        raise NotFound(item_id)
    return it


def get_item(con, item_id: str) -> SupplyItem:
    row = con.execute(_SELECT + " WHERE i.id=?", (item_id,)).fetchone()
    if row is None:
        raise NotFound(item_id)
    return SupplyItem(*tuple(row))


def list_items(con, *, active_only: bool = True) -> list[SupplyItem]:
    q = _SELECT + (" WHERE i.active=1" if active_only else "") + " ORDER BY i.created_at, i.id"
    return [SupplyItem(*tuple(r)) for r in con.execute(q).fetchall()]


def is_paused(item: SupplyItem, day: date) -> bool:
    if not item.paused_from or not item.paused_until:
        return False
    return date.fromisoformat(item.paused_from) <= day <= date.fromisoformat(item.paused_until)


def meals_on(item: SupplyItem, day: date) -> int:
    return item.meals_per_workday if is_meal_day(day) else item.meals_per_weekend_day


def _planned(item: SupplyItem, day: date) -> int:
    return 0 if is_paused(item, day) else meals_on(item, day)


def reorder_point(item: SupplyItem, today: date) -> int:
    """Sum of d over the next L+S days that have meals (equals d*(L+S) when d is uniform)."""
    need = item.lead_days + item.safety_days
    horizon = max(MIN_HORIZON_DAYS, 2 * need + 7)
    total = counted = 0
    for k in range(1, horizon + 1):
        if counted >= need:
            break
        d = _planned(item, today + timedelta(days=k))
        if d > 0:
            total += d
            counted += 1
    return total


@transactional
def consume_through(con, today: date, *, now: datetime) -> int:
    cur = get_cursor(con, CURSOR_KEY)
    floor = today - timedelta(days=CATCHUP_DAYS - 1)
    start = date.fromisoformat(cur) + timedelta(days=1) if cur else today
    start = max(start, floor)
    n = 0
    ts = _iso(now)
    day = start
    while day <= today:
        for item in list_items(con):
            if day < _kst_date(con.execute("SELECT created_at FROM supply_item WHERE id=?",
                                           (item.id,)).fetchone()[0]):
                continue
            d = _planned(item, day)
            if d <= 0:
                continue
            row = con.execute("SELECT remaining FROM supply_stock WHERE item_id=?", (item.id,)).fetchone()
            qty = min(row[0], d)
            c = con.execute(
                "INSERT OR IGNORE INTO supply_event(id,item_id,kind,qty,for_date,created_at) "
                "VALUES(?,?,'consumed',?,?,?)", (new_id(), item.id, qty, day.isoformat(), ts))
            if c.rowcount != 1:
                continue
            con.execute("UPDATE supply_stock SET remaining=MAX(0, remaining-?), updated_at=? "
                        "WHERE item_id=?", (d, ts, item.id))
            n += 1
        day += timedelta(days=1)
    if cur is None or date.fromisoformat(cur) < today:
        set_cursor(con, CURSOR_KEY, today.isoformat())
    return n


@transactional
def deliver(con, item_id: str, qty: int, *, now: datetime) -> dict:
    _check_range("수량", qty, 1, 60)
    item = get_item(con, item_id)
    ts = _iso(now)
    con.execute(
        "UPDATE supply_stock SET remaining=remaining+?, updated_at=?, last_reminded_at=NULL, "
        "snoozed_until=NULL, stale_prompted_at=NULL WHERE item_id=?", (qty, ts, item_id))
    con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) "
                "VALUES(?,?,'delivered',?,?,?)",
                (new_id(), item_id, qty, now.astimezone(KST).date().isoformat(), ts))
    order = con.execute(
        "SELECT id FROM supply_order WHERE item_id=? AND delivered_at IS NULL "
        "ORDER BY ordered_at DESC LIMIT 1", (item_id,)).fetchone()
    if order:
        con.execute("UPDATE supply_order SET delivered_at=? WHERE id=?", (ts, order[0]))
    proposal = None
    cand = lead_candidate(con, item_id)
    if cand is not None and cand != item.lead_days and 0 <= cand <= 14:
        proposal = routine_plan.propose(
            con, f"{routine_plan.LEAD_PREFIX}{item_id}", cand,
            basis=coach_copy.LEAD_PROPOSAL.format(n=LEAD_SAMPLE, days=cand), now=now)
    return {"remaining": get_item(con, item_id).remaining, "lead_proposal": proposal}


@transactional
def skip(con, item_id: str, for_date: date, *, qty: int | None = None, now: datetime) -> None:
    row = con.execute("SELECT qty FROM supply_event WHERE item_id=? AND for_date=? AND kind='consumed'",
                      (item_id, for_date.isoformat())).fetchone()
    if row is None or row[0] <= 0:
        raise ValueError("이 날은 차감된 끼니가 없습니다")
    amount = row[0] if qty is None else min(qty, row[0])
    if amount <= 0:
        raise ValueError("되돌릴 수량은 1 이상입니다")
    ts = _iso(now)
    try:
        con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) "
                    "VALUES(?,?,'skipped',?,?,?)",
                    (new_id(), item_id, amount, for_date.isoformat(), ts))
    except Exception as e:  # unique index: already corrected that day
        if "UNIQUE" in str(e).upper():
            raise ValueError("이미 되돌린 날입니다") from e
        raise
    con.execute("UPDATE supply_stock SET remaining=remaining+?, updated_at=? WHERE item_id=?",
                (amount, ts, item_id))


@transactional
def adjust(con, item_id: str, remaining: int, *, now: datetime) -> None:
    _check_range("남은 끼니", remaining, 0, 1000)
    item = get_item(con, item_id)
    ts = _iso(now)
    con.execute("UPDATE supply_stock SET remaining=?, updated_at=?, stale_prompted_at=NULL "
                "WHERE item_id=?", (remaining, ts, item_id))
    con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) "
                "VALUES(?,?,'adjust',?,?,?)",
                (new_id(), item_id, remaining - item.remaining,
                 now.astimezone(KST).date().isoformat(), ts))


def is_low(item: SupplyItem, today: date) -> bool:
    """Stock at or below the reorder point (pure; ignores reminder bookkeeping)."""
    return item.remaining <= reorder_point(item, today)


def is_due_today(item: SupplyItem, today: date) -> bool:
    """Weekday gate, not paused, snooze elapsed, and low (pure; ignores last_reminded_at)."""
    if not is_meal_day(today):
        return False
    if is_paused(item, today):
        return False
    if item.snoozed_until and date.fromisoformat(item.snoozed_until) > today:
        return False
    return is_low(item, today)


def due_reminders(con, today: date) -> list[SupplyItem]:
    return [it for it in list_items(con) if it.last_reminded_at is None and is_due_today(it, today)]


def mark_reminded(con, item_ids, *, now: datetime) -> None:
    for i in item_ids:
        con.execute("UPDATE supply_stock SET last_reminded_at=? WHERE item_id=?", (_iso(now), i))


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while not is_meal_day(d):
        d += timedelta(days=1)
    return d


def snooze(con, item_id: str, today: date, *, now: datetime) -> date:
    get_item(con, item_id)
    nxt = _next_weekday(today)
    con.execute("UPDATE supply_stock SET snoozed_until=?, last_reminded_at=NULL, updated_at=? "
                "WHERE item_id=?", (nxt.isoformat(), _iso(now), item_id))
    return nxt


def pause(con, item_id: str, start: date, until: date) -> None:
    get_item(con, item_id)
    if start > until:
        raise ValueError("시작일이 종료일보다 늦습니다")
    con.execute("UPDATE supply_item SET paused_from=?, paused_until=? WHERE id=?",
                (start.isoformat(), until.isoformat(), item_id))


def resume(con, item_id: str) -> None:
    get_item(con, item_id)
    con.execute("UPDATE supply_item SET paused_from=NULL, paused_until=NULL WHERE id=?", (item_id,))


def mark_ordered(con, item_id: str, *, now: datetime) -> dict:
    item = get_item(con, item_id)
    since = _iso(now - timedelta(hours=12))
    row = con.execute(
        "SELECT id FROM supply_order WHERE item_id=? AND delivered_at IS NULL AND ordered_at>=? "
        "ORDER BY ordered_at DESC LIMIT 1", (item_id, since)).fetchone()
    if row:
        oid = row[0]
    else:
        oid = new_id()
        con.execute("INSERT INTO supply_order(id,item_id,ordered_at,cutoff_seen) VALUES(?,?,?,?)",
                    (oid, item_id, _iso(now), item.order_cutoff))
    return {"order_id": oid, "ask_cutoff": not item.cutoff_asked, "order_url": item.order_url}


def set_cutoff(con, item_id: str, cutoff: str | None) -> None:
    get_item(con, item_id)
    c = _validate_cutoff(cutoff)
    con.execute("UPDATE supply_item SET order_cutoff=?, cutoff_asked=1 WHERE id=?", (c, item_id))


def lead_candidate(con, item_id: str) -> int | None:
    rows = con.execute(
        "SELECT ordered_at, delivered_at FROM supply_order WHERE item_id=? AND delivered_at IS NOT NULL "
        "ORDER BY delivered_at DESC LIMIT ?", (item_id, LEAD_SAMPLE)).fetchall()
    if len(rows) < LEAD_SAMPLE:
        return None
    days = [(_kst_date(d) - _kst_date(o)).days for o, d in rows]
    return int(statistics.median(days))


def cutoff_hint(item: SupplyItem, now: datetime) -> str | None:
    if not item.order_cutoff:
        return None
    local = now.astimezone(KST)
    if local.strftime("%H:%M") < item.order_cutoff:
        return coach_copy.CUTOFF_HINT.format(hhmm=item.order_cutoff)
    return None


def stale_check_needed(con, item_id: str, today: date) -> bool:
    item = get_item(con, item_id)
    if item.stale_prompted_at is not None:
        return False
    row = con.execute("SELECT MAX(created_at) FROM supply_event WHERE item_id=? "
                      "AND kind IN ('delivered','adjust')", (item_id,)).fetchone()
    base = row[0] or con.execute("SELECT created_at FROM supply_item WHERE id=?", (item_id,)).fetchone()[0]
    if (today - _kst_date(base)).days < STALE_DAYS:
        return False
    return con.execute("SELECT 1 FROM supply_event WHERE item_id=? AND kind='consumed' "
                       "AND created_at>? LIMIT 1", (item_id, base)).fetchone() is not None


def mark_stale_prompted(con, item_id: str, *, now: datetime) -> None:
    con.execute("UPDATE supply_stock SET stale_prompted_at=? WHERE item_id=?", (_iso(now), item_id))


def add_rotation(con, item_id: str, *, label: str, url: str, notes: str | None = None,
                 now: datetime) -> str:
    _active_item(con, item_id)
    if not label or not label.strip():
        raise ValueError("이름이 필요합니다")
    u = validate_https(url)
    if u is None:
        raise ValueError("https 링크가 필요합니다")
    rid = new_id()
    _iso(now)
    con.execute("INSERT INTO supply_rotation(id,item_id,label,url,verified_at,notes) VALUES(?,?,?,?,?,?)",
                (rid, item_id, label.strip(), u, _iso(now), notes))
    return rid


def _first_weekday(today: date) -> date:
    d = today.replace(day=1)
    while d.weekday() not in MEAL_WEEKDAYS:
        d += timedelta(days=1)
    return d


def rotation_card(con, today: date) -> dict | None:
    if today < _first_weekday(today):
        return None
    row = con.execute("SELECT value FROM preference WHERE key='routine.rotation_dismissed'").fetchone()
    if row and row[0] == today.strftime("%Y-%m"):
        return None
    cands = [{"id": r[0], "label": r[1], "url": r[2], "notes": r[3]} for r in con.execute(
        "SELECT r.id,r.label,r.url,r.notes FROM supply_rotation r "
        "JOIN supply_item i ON i.id=r.item_id AND i.active=1 WHERE r.verified_at IS NOT NULL "
        "ORDER BY r.verified_at, r.id").fetchall()]
    if not cands:
        return {"prompt": coach_copy.ROTATION_EMPTY, "candidates": []}
    return {"prompt": coach_copy.ROTATION_PROMPT, "candidates": cands}


def dismiss_rotation(con, today: date, *, now: datetime | None = None) -> None:
    from pa.clock import utcnow_iso
    ts = _iso(now) if now is not None else utcnow_iso()
    con.execute(
        "INSERT INTO preference(key,value,updated_at) VALUES('routine.rotation_dismissed',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (today.strftime("%Y-%m"), ts))
