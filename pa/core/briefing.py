import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pa.logsafe import log_failure
from pa.clock import utcnow_iso
from pa.core import briefing_copy, coach_copy, dismiss, meals, priority, priority_sources, supply
from pa.core.notify import send_notify
from pa.core.quiet import should_deliver
from pa.ids import new_id
from pa.scheduler import SCOUT_JOBS, due_jobs, mark_run

log = logging.getLogger("pa.briefing")
KST = ZoneInfo("Asia/Seoul")
_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
_RETRO = ["오늘 가장 잘된 일은 무엇이었나요?", "오늘 막힌 일은 무엇이었나요?", "내일 가장 먼저 할 일은 무엇인가요?",
          "오늘 미룬 일이 있다면 이유는 무엇인가요?", "이번 주 가장 만족스러웠던 일은 무엇인가요?",
          "쉬는 날에 꼭 챙길 일이 있나요?", "이번 주를 한 줄로 돌아본다면?"]
_FAIL_CAL = "캘린더를 불러오지 못했습니다"


def _supply_section(con, today, now: datetime) -> dict | None:
    """식사 공급 요약. due는 평일·일시정지·미룸·재주문점으로 판단하고 last_reminded_at은 보지 않는다(08:25 점검이 이미 reminded 표시를 했음)."""
    try:
        items = supply.list_items(con)
        if not items:
            return None
        return {"items": [{"label": it.label, "remaining": it.remaining,
                           "due": supply.is_due_today(it, today), "hint": supply.cutoff_hint(it, now)}
                          for it in items]}
    except Exception as e:
        log_failure(log, "briefing: supply section failed", e, logging.WARNING)
        return None


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%H:%M")


def _mode(kind: str, now: datetime, rules) -> str:
    if kind == "evening":
        return "evening"
    if kind == "friday":
        return "friday"
    return "rest_day" if now.astimezone(KST).weekday() in getattr(rules, "rest_days", ()) else "morning"


_KIND_BY_PREFIX = {"task": "task", "event": "event", "approvals": "approval"}


def _shown(ranked: list[dict]) -> list[dict]:
    """음성 개인정보: 각 항목에 진짜 출처 종류(source)와 출처 태그(origin)를 단다. 음성은 이 태그로 출처별 낭독 설정을 적용한다."""
    out = []
    for d in ranked:
        keys = d.get("keys") or [d["id"]]
        kinds = {_KIND_BY_PREFIX[k.split(":", 1)[0]] for k in keys if k.split(":", 1)[0] in _KIND_BY_PREFIX}
        src = {"calendar": "event", "approvals": "approval"}.get(d["source"], d["source"])
        out.append({**{k: v for k, v in d.items() if k != "keys"}, "source": src, "origin": sorted(kinds | {src})})
    return out


REST_SHOWN = 30      # 상세 화면에 보여 줄 '그 외' 항목 수(저장 크기 제한)


def _local_tasks(con, now: datetime) -> tuple[list[dict], int]:
    """(오늘 완료한 할 일, 최근 7일 완료 수) — task 표에서. 오늘 기준은 KST."""
    day0 = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    done = [{"text": r["title"]} for r in con.execute(
        "SELECT title FROM task WHERE status='done' AND completed_at>=? ORDER BY completed_at", (day0,))]
    wk0 = (now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    done_week = con.execute("SELECT COUNT(*) FROM task WHERE status='done' AND completed_at>=?", (wk0,)).fetchone()[0]
    return done, done_week


def _assemble(kind, now, events, cal_err, approvals, rules, *, supply_sec=None, con=None, cap=None) -> dict:
    cap = cap if cap is not None else rules.max_today_must
    mode = _mode(kind, now, rules)
    items = priority_sources.collect(con, now, events=events, kind=kind)
    rank_error = None
    try:
        r_all = priority.rank(items, now, cap=10**6, mode=mode).all
    except Exception as e:   # 잘못된 입력 하나가 브리핑 전체를 막지 않게: 빈 목록 + 눈에 보이는 경고
        log.warning("briefing: rank failed (%s)", type(e).__name__)
        r_all, rank_error = [], briefing_copy.RANK_ERROR_LINE
    # 막연한 백로그(rest 단계)는 "오늘 꼭 할 일"에 넣지 않는다. 승인 대기는 아래 '승인 대기 n건' 줄이 이미 세므로 목록에서는 뺀다.
    gone = dismiss.keys(con) if con is not None else set()   # 옆으로 밀어 읽음 처리한 항목은 다시 나오지 않는다
    listed = [d for d in r_all if d["tier"] != "rest" and d["source"] != "approvals"
              and not gone.intersection(d.get("keys") or [d["id"]])]
    shown = _shown(listed)
    done_today, done_week = _local_tasks(con, now) if con is not None else ([], 0)
    return {
        "today_must": shown[:cap], "rest_count": max(len(shown) - cap, 0),
        "rest_items": [{k: d.get(k) for k in ("id", "text", "source", "reasons", "date")} for d in shown[cap:cap + REST_SHOWN]],
        "calendar": events, "calendar_note": cal_err,
        "rank_error": rank_error, "approvals": approvals, "supply": supply_sec,
        "done_today": done_today, "left_today": shown, "done_week": done_week,
        "retro": _RETRO[now.astimezone(KST).weekday() % len(_RETRO)],
    }


def _render(kind: str, s: dict) -> str:
    out: list[str] = []
    cal = ["- " + (f"{e['time']} " if e["time"] else "") + e["title"] for e in s["calendar"]] or ["- 일정 없음"]
    if s["calendar_note"]:
        cal = [f"- {s['calendar_note']}"]
    if kind == "evening":
        out += ["## 완료한 일"] + ([f"- {t['text']}" for t in s["done_today"]] or ["- 없음"])
        out += ["## 남은 일"] + ([f"- {t['text']}" for t in s["left_today"]] or ["- 없음"])
        if s.get("rank_error"):
            out.append(f"- {s['rank_error']}")
        out += ["## 회고 질문", s["retro"]]
    else:
        out.append("## 오늘 꼭 할 일" if kind == "morning" else "## 이번 주 정리")
        if kind == "friday":
            out.append(f"- 지난 7일 완료한 일 {s['done_week']}건")
        out += [f"{i}. {t['text']}" + (f" — {t['reasons'][0]}" if t.get("reasons") else "")
                for i, t in enumerate(s["today_must"], 1)] or ["- 없음"]
        if s.get("rank_error"):
            out.append(f"- {s['rank_error']}")
        if s["rest_count"]:
            out.append(f"(그 외 {s['rest_count']}건)")
        out += ["## 다음 주 일정" if kind == "friday" else "## 일정"] + cal
        sup = s.get("supply")
        if kind == "morning" and sup:
            out.append(coach_copy.SUPPLY_BRIEF_HEADING)
            for it in sup["items"]:
                line = f"- {it['label']} {coach_copy.REMAINING_LABEL} {it['remaining']}"
                if it["due"]:
                    line += f" — {coach_copy.SUPPLY_BRIEF_DUE}"
                if it["hint"]:
                    line += f" ({it['hint']})"
                out.append(line)
    out.append(f"## 승인 대기 {s['approvals']}건")
    return "\n".join(out)


async def build_briefing(con, kind: str, now: datetime, cal, router, rules) -> dict:
    now = now.astimezone(KST)
    for_date = now.strftime("%Y-%m-%d")
    row = con.execute("SELECT * FROM briefing WHERE kind=? AND for_date=?", (kind, for_date)).fetchone()
    if row:
        return {"id": row["id"], "kind": kind, "for_date": for_date,
                "sections": json.loads(row["sections_json"] or "{}"), "body_md": row["body_md"]}

    events, cal_err = [], None
    if cal is None:
        cal_err = "캘린더 미연결"
    else:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = (day, day + timedelta(days=1)) if kind != "friday" else (
            day + timedelta(days=3), day + timedelta(days=10))
        try:
            for e in await cal.events_between(start, end):
                events.append({"id": e.id, "title": e.title, "all_day": e.all_day,
                               "time": "" if e.all_day else _hhmm(e.start),
                               "start": e.start.isoformat(), "end": e.end.isoformat()})
        except Exception as e:
            log_failure(log, "briefing: calendar failed", e, logging.WARNING)
            cal_err = _FAIL_CAL

    approvals = con.execute("SELECT COUNT(*) FROM draft WHERE status='review'").fetchone()[0]
    sections = _assemble(kind, now, events, cal_err, approvals, rules, con=con,
                         supply_sec=_supply_section(con, now.date(), now) if kind == "morning" else None)
    body = _render(kind, sections)
    try:
        res = await router.run("summarize", "briefing", body, _SCHEMA)
        if res and res.data and not res.degraded and res.data.get("text"):
            body = f"> {res.data['text']}\n\n{body}"
    except Exception as e:
        log_failure(log, "briefing: summary failed", e, logging.WARNING)

    bid = new_id()
    con.execute("INSERT OR IGNORE INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES(?,?,?,?,?,?)",
                (bid, kind, for_date, body, json.dumps(sections, ensure_ascii=False, default=str), utcnow_iso()))
    row = con.execute("SELECT id FROM briefing WHERE kind=? AND for_date=?", (kind, for_date)).fetchone()
    return {"id": row["id"], "kind": kind, "for_date": for_date, "sections": sections, "body_md": body}


async def notify_gated(con, kind: str, count: int, dedupe_key: str, now: datetime, rules, *,
                       urgent: bool = False, user_requested: bool = False) -> str:
    verdict = should_deliver(kind, now, rules, urgent=urgent, user_requested=user_requested)
    if verdict == "deliver":
        await send_notify(con, kind, count, dedupe_key)
    elif verdict == "hold":
        con.execute("INSERT OR IGNORE INTO held_notify(id,kind,count,dedupe_key,held_at) VALUES(?,?,?,?,?)",
                    (new_id(), kind, count, dedupe_key, utcnow_iso()))
    return verdict


async def _deliver_held(con, now: datetime) -> None:
    day = now.astimezone(KST).strftime("%Y-%m-%d")
    rows = con.execute("SELECT id,kind,count FROM held_notify WHERE released_at IS NULL ORDER BY held_at, id").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["kind"], []).append(r)
    for kind, items in groups.items():
        await send_notify(con, kind, sum(r["count"] for r in items), f"held:{kind}:{day}")
        con.executemany("UPDATE held_notify SET released_at=? WHERE id=?",
                        [(utcnow_iso(), r["id"]) for r in items])


async def run_due_jobs(con, now: datetime, rules, cal, router) -> list[str]:
    ran: list[str] = []
    for job in due_jobs(con, now, rules):
        if job in SCOUT_JOBS:      # scout 잡은 scout_tick 이 실행·mark_run
            continue
        if not mark_run(con, job, now):
            continue
        ran.append(job)
        try:
            if job.startswith("brief_"):
                kind = job.removeprefix("brief_")
                b = await build_briefing(con, kind, now, cal, router, rules)
                nkind = "briefing_evening" if kind == "evening" else "briefing_morning"
                await notify_gated(con, nkind, 1, f"brief:{kind}:{b['for_date']}", now, rules)
            elif job == "deliver_held":
                await _deliver_held(con, now)
            elif job.startswith("meal_"):
                await meals.run_job(con, job, now, rules, router)
            elif job == "kids_prep":
                from pa.core import kids, profile
                from pa.settings import Settings
                await kids.run_prep_job(con, now, rules, profile.load(Settings().data_dir))
        except Exception as e:
            log_failure(log, f"job failed: {job}", e)
    return ran
