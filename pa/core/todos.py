from datetime import date, timedelta


def next_workday(today: date) -> date:
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def suggest_carryover(todos: list[dict], today: date) -> list[dict]:
    """미완료이면서 날짜가 오늘보다 이전인 할 일 -> 다음 근무일 이월 후보(순수 함수)."""
    target = next_workday(today).isoformat()
    out = []
    for t in todos:
        if t.get("done"):
            continue
        d = t.get("date")
        if d and d < today.isoformat():
            out.append({"todo_id": t["id"], "text": t["text"], "date": target})
    return out
