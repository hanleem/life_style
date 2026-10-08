"""아이 루틴: 등교·하교·학원 요일과 시간, 준비물, 하루 전 저녁 준비물 알림.

아이 순서는 가족 프로필의 아이(role=child) 순서와 같다(첫 아이 = children[0]). 설정은 DB 의 preference 에 JSON 으로 둔다.
준비물은 학원별 `bring` 과 가정통신문에서 읽은 준비물(notice_event)을 합쳐 보여 준다. 알림에는 개수만 담는다.
"""
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("pa.kids")
KST = ZoneInfo("Asia/Seoul")
PREF_KEY = "kids.routine"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABEL = dict(zip(DAYS, "월화수목금토일"))
WEEKDAYS = DAYS[:5]
MAX_KIDS = 4
MAX_CLASSES = 5
MAX_BRING = 8
NAME_MAX = 20
ITEM_MAX = 30
DEFAULT_PREP = "20:00"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_BIRTH_ORDER = ("첫째", "둘째", "셋째", "넷째", "다섯째", "여섯째", "일곱째", "여덟째")


class KidsError(ValueError):
    """사용자에게 보여 줄 수 있는 쉬운 한국어 메시지."""


def defaults() -> dict:
    return {"prep_time": DEFAULT_PREP, "children": []}


def _hhmm(v, label: str, *, blank_ok: bool = True) -> str:
    v = str(v or "").strip()
    if not v and blank_ok:
        return ""
    if not _HHMM.match(v):
        raise KidsError(f"{label}은(는) 24시간 형식(예: 08:40)으로 적어 주세요")
    return v


def _days(v, label: str, *, default=(), required=False) -> list[str]:
    if v is None:   # 키가 없을 때만 기본값(빈 목록은 '그 요일이 하나도 없음'이라는 뜻)
        v = list(default)
    days = [str(d).strip().lower() for d in v] if isinstance(v, (list, tuple)) else None
    if days is None or any(d not in DAYS for d in days):
        raise KidsError(f"{label} 요일을 다시 골라 주세요")
    if required and not days:
        raise KidsError(f"{label}은(는) 요일을 하나 이상 골라 주세요")
    return [d for d in DAYS if d in days]


def _words(v, limit: int) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    out: list[str] = []
    for w in v or []:
        w = str(w).strip()[:ITEM_MAX]
        if w and w not in out:
            out.append(w)
    return out[:limit]


def _clean_name(v, label: str) -> str:
    name = "".join(ch for ch in str(v or "") if ch not in "<>&\"'").strip()[:NAME_MAX]
    if not name:
        raise KidsError(f"{label} 이름을 적어 주세요")
    return name


def _klass(k) -> dict | None:
    if not isinstance(k, dict) or not str(k.get("name") or "").strip():
        return None   # 빈 줄은 건너뛴다
    return {"name": _clean_name(k.get("name"), "학원"), "days": _days(k.get("days"), "학원", required=True),
            "start": _hhmm(k.get("start"), "학원 시작 시각"), "bring": _words(k.get("bring"), MAX_BRING)}


def _child(c) -> dict:
    if not isinstance(c, dict):
        raise KidsError("아이 정보가 올바르지 않아요")
    classes = [k for k in (_klass(x) for x in c.get("classes") or []) if k]
    if len(classes) > MAX_CLASSES:
        raise KidsError(f"학원은 아이마다 {MAX_CLASSES}개까지 적을 수 있어요")
    return {"school_days": _days(c.get("school_days"), "등교", default=WEEKDAYS),
            "school_start": _hhmm(c.get("school_start"), "등교 시각"), "school_end": _hhmm(c.get("school_end"), "하교 시각"),
            "classes": classes}


def normalize(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("children", []), list):
        raise KidsError("아이 루틴 정보가 올바르지 않아요")
    kids_ = raw.get("children") or []
    if len(kids_) > MAX_KIDS:
        raise KidsError(f"아이는 {MAX_KIDS}명까지 적을 수 있어요")
    return {"prep_time": _hhmm(raw.get("prep_time") or DEFAULT_PREP, "알림 시각", blank_ok=False),
            "children": [_child(c) for c in kids_]}


def load(con) -> dict:
    row = con.execute("SELECT value FROM preference WHERE key=?", (PREF_KEY,)).fetchone()
    if not row or not row["value"]:
        return defaults()
    try:
        return normalize(json.loads(row["value"]))
    except (ValueError, KidsError):
        log.warning("kids routine unreadable; using defaults")
        return defaults()


def save(con, raw) -> dict:
    data = normalize(raw)
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES(?,?)", (PREF_KEY, json.dumps(data, ensure_ascii=False)))
    return data


def prep_time(con) -> time:
    h, m = load(con)["prep_time"].split(":")
    return time(int(h), int(m))


def child_labels(prof: dict | None) -> list[str]:
    """프로필의 아이들을 화면에 부르는 이름. 이름이 없으면 태어난 순서(첫째·둘째…)."""
    out = []
    for i, m in enumerate(c for c in (prof or {}).get("members") or [] if c.get("role") == "child"):
        out.append(f"{m.get('name') or _BIRTH_ORDER[min(i, len(_BIRTH_ORDER) - 1)]}({m.get('age')}세)")
    return out


def _label(labels: list[str], i) -> str:
    if i is None:
        return ""
    return labels[i] if 0 <= i < len(labels) else f"아이{i + 1}"


def day_plan(con, day: date, prof: dict | None) -> dict:
    """그날의 준비물(items)과 시간표 줄(lines). 학원은 그 요일에 있는 것만, 가정통신문은 그 날짜의 것."""
    data, labels = load(con), child_labels(prof)
    wd = DAYS[day.weekday()]
    items, lines = [], []
    for i, c in enumerate(data["children"]):
        who = _label(labels, i)
        if wd in c["school_days"] and (c["school_start"] or c["school_end"]):
            parts = [who] + ([f"등교 {c['school_start']}"] if c["school_start"] else []) + ([f"하교 {c['school_end']}"] if c["school_end"] else [])
            lines.append(" · ".join(parts))
        for k in c["classes"]:
            if wd not in k["days"]:
                continue
            lines.append(f"{who} · {k['name']}" + (f" {k['start']}" if k["start"] else ""))
            items += [{"child": who, "text": b, "why": k["name"] + (f" {k['start']}" if k["start"] else "")} for b in k["bring"]]
    for r in con.execute("SELECT title, items_json, child_index FROM notice_event WHERE event_date=? AND status!='dismissed' "
                         "ORDER BY created_at, id", (day.isoformat(),)):
        try:
            things = json.loads(r["items_json"] or "[]")
        except ValueError:
            things = []
        who = _label(labels, r["child_index"])
        if things:
            items += [{"child": who, "text": str(t), "why": r["title"]} for t in things]
        else:   # 준비물이 따로 없으면 행사 자체를 확인할 일로 알려 준다
            items.append({"child": who, "text": r["title"], "why": "가정통신문"})
    return {"items": items, "lines": lines}


async def run_prep_job(con, now: datetime, rules, prof: dict | None = None) -> str:
    """하루 전 저녁: 내일 가져갈 준비물이 있으면 개수만 알린다(방해 금지 시간이면 보류)."""
    from pa.core.briefing import notify_gated
    tomorrow = now.astimezone(KST).date() + timedelta(days=1)
    n = len(day_plan(con, tomorrow, prof)["items"])
    if n == 0:
        return "none"
    return await notify_gated(con, "kids_prep", n, f"kids_prep:{tomorrow.isoformat()}", now, rules)
