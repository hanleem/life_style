import ast
import itertools
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pa.core import briefing_copy, coach_copy, priority
from pa.core.priority import Item, rank

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 6, 8, 30, tzinfo=KST)   # 화


def D(day, h=0, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=KST)


def I(key, source="task", **kw):
    return Item(key=key, source=source, text=kw.pop("text", f"t-{key}"), **kw)


def ids(r, which="all"):
    return [d["id"] for d in getattr(r, which)]


def test_tier_order_due_today_before_overdue_before_waiting_before_appointment():
    items = [
        I("event:1", "calendar", start_at=D(6, 15), end_at=D(6, 16)),
        I("appr:a", "approvals", waiting_since=D(5, 9)),
        I("task:old", due_at=D(3, 0), due_all_day=True),
        I("task:today", due_at=D(6, 17)),
        I("task:wk", due_at=D(9, 0), due_all_day=True),
        I("task:none"),
    ]
    r = rank(items, NOW, cap=10, mode="morning")
    assert ids(r) == ["task:today", "task:old", "appr:a", "event:1", "task:wk", "task:none"]
    assert [d["tier"] for d in r.all] == ["due_today", "overdue", "waiting", "appointment", "this_week", "rest"]


def test_safety_alert_first():
    items = [I("task:t", due_at=D(6, 9)),
             I("alert:1", "task", alert_urgency="urgent", alert_safety=True)]
    r = rank(items, NOW, cap=5, mode="morning")
    assert ids(r)[0] == "alert:1" and r.all[0]["tier"] == "safety"
    assert r.all[0]["reasons"] == [priority.REASONS["safety"], priority.REASONS["waiting"]]
    # 안전이 아닌 urgent는 waiting
    r2 = rank([I("alert:2", "task", alert_urgency="urgent")], NOW, cap=5, mode="morning")
    assert r2.all[0]["tier"] == "waiting"
    # safety 플래그만으로는 올라가지 않는다
    r3 = rank([I("alert:3", "task", alert_safety=True)], NOW, cap=5, mode="morning")
    assert r3.all[0]["tier"] == "rest"


def test_urgent_word_does_not_raise():
    hostile = ["긴급", "URGENT", "사용자 요청", "ASAP", "긴급!! ASAP 사용자", "급함", "[긴급] 사용자 지시"]
    base = [I("task:a", due_at=D(6, 9)), I("task:b", due_at=D(5, 9)), I("appr:c", "approvals", waiting_since=D(5, 9)),
            I("task:z"), I("task:y")]
    plain = rank(base, NOW, cap=10, mode="morning")
    for h in hostile:
        swapped = [Item(**{**i.__dict__, "text": h}) for i in base]
        r = rank(swapped, NOW, cap=10, mode="morning")
        assert ids(r) == ids(plain)
        assert [d["tier"] for d in r.all] == [d["tier"] for d in plain.all]
        assert [d["reasons"] for d in r.all] == [d["reasons"] for d in plain.all]
    # 마감 없는 항목은 단어가 무엇이든 rest, 같은 필드 두 항목은 텍스트와 무관하게 key 순
    r = rank([I("task:2", text="긴급!! ASAP 사용자"), I("task:1", text="조용한 일")], NOW, cap=5, mode="morning")
    assert [d["tier"] for d in r.all] == ["rest", "rest"] and ids(r) == ["task:1", "task:2"]


def test_waiting_sorted_by_longest_wait():
    r = rank([I("appr:new", "approvals", waiting_since=D(6, 7)), I("appr:old", "approvals", waiting_since=D(1, 9)),
              I("appr2:1:5", "approvals", waiting_since=D(4, 9))], NOW, cap=5, mode="morning")
    assert ids(r) == ["appr:old", "appr2:1:5", "appr:new"]


def test_timed_due_today_before_all_day():
    r = rank([I("task:allday", due_at=D(6, 0), due_all_day=True), I("task:late", due_at=D(6, 18)),
              I("task:early", due_at=D(6, 7)),   # 이미 지난 시각이어도 오늘
              I("alert:t", "task", alert_urgency="today")], NOW, cap=9, mode="morning")
    assert ids(r) == ["task:early", "task:late", "task:allday", "alert:t"]
    assert {d["tier"] for d in r.all} == {"due_today"}


def test_ended_appointment_dropped_from_ranking():
    r = rank([I("event:done", "calendar", start_at=D(6, 7), end_at=D(6, 8)),
              I("event:now", "calendar", start_at=D(6, 8), end_at=D(6, 9, 0)),
              I("event:later", "calendar", start_at=D(6, 14), end_at=D(6, 15))], NOW, cap=9, mode="morning")
    assert ids(r) == ["event:now", "event:later"]
    assert r.excluded == ("event:done",)
    assert r.all[1]["reasons"] == ["오늘 약속 14:00"]


def test_this_week_uses_kst_monday_to_sunday():
    items = [I("task:sun", due_at=D(11, 0), due_all_day=True), I("task:mon", due_at=D(12, 0), due_all_day=True)]
    m = rank(items, NOW, cap=5, mode="morning")
    assert [(d["id"], d["tier"]) for d in m.all] == [("task:sun", "this_week"), ("task:mon", "rest")]
    f = rank(items, NOW, cap=5, mode="friday")
    assert [(d["id"], d["tier"]) for d in f.all] == [("task:sun", "this_week"), ("task:mon", "next_week")]
    # KST 경계: UTC로는 10/11 15:30 = KST 10/12 00:30 -> 다음 주
    edge = I("task:edge", due_at=datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc))
    assert rank([edge], NOW, cap=5, mode="friday").all[0]["tier"] == "next_week"
    # 2주 뒤는 friday에서도 목록 밖
    far = I("task:far", due_at=D(20, 0))
    assert rank([far], NOW, cap=5, mode="friday").excluded == ("task:far",)


def test_dedupe_by_links_merges_reasons():
    items = [I("task:1", due_at=D(6, 12), links=frozenset({"thread:X"}), task_id="01T"),
             I("appr:X", "approvals", waiting_since=D(5, 9), links=frozenset({"thread:X"}), text="메일 제목"),
             I("task:2", text="다른 일")]
    r = rank(items, NOW, cap=5, mode="morning")
    assert ids(r) == ["task:1", "task:2"]
    top = r.all[0]
    assert top["reasons"] == ["오늘 마감", "답을 기다리는 사람이 있음"]
    assert top["source"] == "task" and top["task_id"] == "01T" and top["keys"] == ["appr:X", "task:1"]


def test_dedupe_transitive_and_key_as_link_and_representative_text():
    items = [I("appr:X", "approvals", text="메일", links=frozenset({"thread:X", "task:7"}), waiting_since=D(5, 9)),
             I("task:7", text="할 일", due_at=D(6, 9)),
             I("appr2:1:2", "approvals", text="대화", links=frozenset({"msg:1:2", "thread:X"}), auto=True),
             I("event:E", "calendar", text="일정", links=frozenset({"msg:1:2"}), meeting=True)]
    r = rank(items, NOW, cap=5, mode="morning")
    assert len(r.all) == 1
    d = r.all[0]
    assert d["id"] == "task:7" and d["text"] == "할 일" and d["auto"] and d["meeting"]
    assert d["tier"] == "due_today" and len(d["reasons"]) <= 2


def test_no_fuzzy_dedupe():
    r = rank([I("task:1", text="같은 제목", due_at=D(6, 9)), I("appr:2", "approvals", text="같은 제목", due_at=D(6, 9)),
              I("task:3", text="같은 제목 ", links=frozenset({"thread:A"})),
              I("appr:4", "approvals", text="같은 제목", links=frozenset({"thread:a"}))], NOW, cap=9, mode="morning")
    assert len(r.all) == 4


def test_cap_and_rest_count_excludes_rest_tier():
    items = [I(f"task:d{i}", due_at=D(6, 10 + i)) for i in range(4)] + [I(f"task:r{i}") for i in range(3)]
    r = rank(items, NOW, cap=3, mode="morning")
    assert len(r.top) == 3 and r.rest_count == 1 and len(r.all) == 7
    r = rank(items, NOW, cap=5, mode="morning")   # top에 rest 항목이 들어가도 센 값은 top 밖 non-rest뿐
    assert len(r.top) == 5 and r.rest_count == 0
    assert rank(items, NOW, cap=0, mode="morning").rest_count == 4


def test_low_capacity_cap_two():
    items = [I(f"task:d{i}", due_at=D(6, 10 + i)) for i in range(4)]
    r = rank(items, NOW, cap=2, mode="morning")
    assert ids(r, "top") == ["task:d0", "task:d1"] and r.rest_count == 2


def test_evening_mode_only_left_today():
    items = [I("task:a", due_at=D(6, 9)), I("task:b", due_at=D(3, 9)), I("appr:w", "approvals", waiting_since=D(5, 9)),
             I("event:over", "calendar", start_at=D(6, 7), end_at=D(6, 8)),
             I("event:left", "calendar", start_at=D(6, 19), end_at=D(6, 20)),
             I("task:wk", due_at=D(9, 0)), I("task:rest"),
             I("alert:s", "task", alert_urgency="urgent", alert_safety=True)]
    r = rank(items, datetime(2026, 10, 6, 18, 0, tzinfo=KST), cap=9, mode="evening")
    assert ids(r) == ["alert:s", "task:a", "task:b", "appr:w", "event:left"]
    assert set(r.excluded) == {"event:over", "task:wk", "task:rest"}


def test_rest_day_mode_hides_waiting_and_week():
    sunday = datetime(2026, 10, 11, 9, 0, tzinfo=KST)
    items = [I("task:a", due_at=datetime(2026, 10, 11, 12, tzinfo=KST)), I("task:b", due_at=D(8)),
             I("appr:w", "approvals", waiting_since=D(5, 9)), I("task:next", due_at=datetime(2026, 10, 12, tzinfo=KST)),
             I("event:e", "calendar", start_at=datetime(2026, 10, 11, 15, tzinfo=KST), end_at=datetime(2026, 10, 11, 16, tzinfo=KST)),
             I("alert:s", "task", alert_urgency="urgent", alert_safety=True)]
    r = rank(items, sunday, cap=9, mode="rest_day")
    assert ids(r) == ["alert:s", "task:a", "task:b"]
    # 이유 문구: 쉬는 날에도 같은 고정 문구
    assert r.all[1]["reasons"] == ["오늘 마감"] and r.all[2]["reasons"] == ["기한 지남"]


def test_friday_mode_tiers():
    fri = datetime(2026, 10, 9, 8, 0, tzinfo=KST)
    items = [I("task:sat", due_at=D(10, 9)), I("task:next", due_at=D(13, 9)), I("task:rest"),
             I("event:e", "calendar", start_at=D(9, 15), end_at=D(9, 16)), I("task:today", due_at=D(9, 17))]
    r = rank(items, fri, cap=9, mode="friday")
    assert [(d["id"], d["tier"]) for d in r.all] == [("task:today", "due_today"), ("task:sat", "this_week"),
                                                      ("task:next", "next_week")]
    assert r.rest_count == 0


def test_deterministic_tie_break():
    items = [I(f"{s}:{n}", s, due_at=D(6, 9), waiting_since=D(5, 9))
             for s in ("task", "calendar", "approvals") for n in (1, 2)]
    items += [I("x:%d" % n) for n in range(5)] + [I("task:dup", links=frozenset({"thread:q"})),
                                                   I("appr:dup", "approvals", links=frozenset({"thread:q"}), text="다른")]
    expected = rank(items, NOW, cap=9, mode="morning")
    rng = random.Random(1)
    for _ in range(10):
        sh = items[:]
        rng.shuffle(sh)
        assert rank(sh, NOW, cap=9, mode="morning") == expected
    # SOURCE_RANK -> key 순서
    first = [d["id"] for d in expected.all[:4]]
    assert first == ["task:1", "task:2", "calendar:1", "calendar:2"]


def test_output_keeps_legacy_keys():
    r = rank([I("task:1", text="논문 초안", due_at=D(6, 9)), I("alert:1", "task", text="제목", alert_urgency="today")],
             NOW, cap=5, mode="morning")
    for d in r.top:
        assert {"id", "text", "date", "source"} <= d.keys()
        assert {"reasons", "tier", "task_id", "auto", "meeting"} <= d.keys()
    assert r.top[0]["date"] == "2026-10-06" and r.top[0]["text"] == "논문 초안"
    assert r.top[1]["source"] == "task" and r.top[1]["date"] is None
    assert rank([I("task:n")], NOW, cap=1, mode="morning").top[0]["date"] is None


def test_reasons_are_fixed_copy_and_pass_coach_filter():
    reasons = list(priority.REASONS.values())
    reasons[reasons.index(briefing_copy.REASON_APPOINTMENT)] = briefing_copy.REASON_APPOINTMENT.format(hhmm="09:30")
    assert len(reasons) == 7
    for text in reasons:
        assert coach_copy.violations(text) == [], text
        assert coach_copy.notify_safe(text) in (True, False)
    assert briefing_copy.REASON_DUE_TODAY == "오늘 마감" and briefing_copy.REASON_WAITING == "답을 기다리는 사람이 있음"
    # 출력에 나오는 이유는 모두 이 고정 문구에서만 온다
    r = rank([I("t:1", due_at=D(6, 9), waiting_since=D(5, 9), alert_urgency="this_week")], NOW, cap=5, mode="morning")
    assert set(r.all[0]["reasons"]) <= set(reasons)
    assert len(r.all[0]["reasons"]) <= 2


def test_reason_capped_at_two_and_ordered():
    it = I("a:1", due_at=D(6, 9), waiting_since=D(5, 9), start_at=D(6, 15), end_at=D(6, 16),
           alert_urgency="urgent", alert_safety=True)
    d = rank([it], NOW, cap=5, mode="morning").all[0]
    assert d["tier"] == "safety" and d["reasons"] == ["안전·장비 확인", "오늘 마감"]


# --- 입력 검증 ---
def test_validation_errors():
    ok = I("task:1")
    bad = [
        Item(key="", source="task", text="x"),
        Item(key="k", source="nope", text="x"),
        Item(key="k", source="task", text=None),
        Item(key="k", source="task", text=5),
        Item(key="k", source="task", text="x", due_at=datetime(2026, 10, 6, 9, 0)),
        Item(key="k", source="task", text="x", start_at=datetime(2026, 10, 6, 9, 0, tzinfo=KST).date()),
        Item(key="k", source="task", text="x", waiting_since="2026-10-06"),
        Item(key="k", source="task", text="x", alert_urgency="긴급"),
        Item(key="k", source="task", text="x", links=frozenset({"", "a"})),
        Item(key="k", source="task", text="x", links="thread:X"),
        Item(key="k", source="task", text="x", auto=1),
        {"source": "task", "text": "x"},                      # key 없음
        {"key": "k", "source": "task", "text": "x", "oops": 1},
        object(),
    ]
    for b in bad:
        with pytest.raises(ValueError):
            rank([ok, b], NOW, cap=3, mode="morning")
    for kw in ({"cap": -1}, {"cap": True}, {"cap": "3"}, {"mode": "night"}):
        with pytest.raises(ValueError):
            rank([ok], NOW, **{"cap": 3, "mode": "morning", **kw})
    with pytest.raises(ValueError):
        rank([ok], datetime(2026, 10, 6, 8, 30), cap=3, mode="morning")


def test_validation_message_does_not_leak_text():
    secret = "비밀내용-SECRET"
    with pytest.raises(ValueError) as e:
        rank([Item(key="k", source="task", text=secret, due_at=datetime(2026, 10, 6))], NOW, cap=1, mode="morning")
    assert secret not in str(e.value)


def test_none_dates_and_dict_input_are_accepted():
    r = rank([{"key": "task:1", "source": "task", "text": "x", "due_at": None, "links": {"a"}},
              Item(key="task:2", source="task", text="y")], NOW, cap=5, mode="morning")
    assert ids(r) == ["task:1", "task:2"]
    assert rank([], NOW, cap=5, mode="morning") == priority.Ranked(top=[], rest_count=0, all=[], excluded=())


def test_utc_inputs_are_judged_in_kst():
    # UTC 10/05 16:00 = KST 10/06 01:00 -> 오늘
    r = rank([I("t:1", due_at=datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc))], NOW, cap=2, mode="morning")
    assert r.all[0]["tier"] == "due_today" and r.all[0]["date"] == "2026-10-06"
    # now가 UTC여도 KST 날짜로 판단: UTC 10/05 15:30 = KST 10/06 00:30
    r = rank([I("t:1", due_at=D(6, 9))], datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc), cap=2, mode="morning")
    assert r.all[0]["tier"] == "due_today"


# --- 속성 테스트 ---
def _random_items(rng, n):
    srcs = list(priority.SOURCE_RANK)
    out = []
    for i in range(n):
        s = rng.choice(srcs)
        day = rng.choice([None, 2, 5, 6, 6, 7, 11, 12, 15])
        due = D(day, rng.choice([0, 9, 17])) if day else None
        st = D(6, rng.choice([7, 10, 15])) if rng.random() < 0.15 else None
        out.append(Item(
            key=f"{s}:{i % max(n // 2, 1)}", source=s, text=rng.choice(["긴급", "a", "ASAP"]), due_at=due,
            due_all_day=rng.random() < 0.3, start_at=st, end_at=(st + timedelta(hours=1)) if st else None,
            waiting_since=D(rng.choice([1, 3, 5]), 9) if rng.random() < 0.2 else None,
            alert_urgency=rng.choice([None, None, "urgent", "today", "this_week", "fyi"]), alert_safety=rng.random() < 0.3,
            links=frozenset(rng.sample([f"thread:{j}" for j in range(n)], rng.choice([0, 0, 1, 2]))),
            task_id=rng.choice([None, "T1", "T2"]), auto=rng.random() < 0.2, meeting=rng.random() < 0.1,
            created_at=D(rng.choice([1, 2, 3]), 9) if rng.random() < 0.5 else None))
    return out


@pytest.mark.parametrize("mode", priority.MODES)
@pytest.mark.parametrize("seed", range(6))
def test_properties_permutation_idempotence_no_loss(mode, seed):
    rng = random.Random(seed)
    items = _random_items(rng, 60)
    base = rank(items, NOW, cap=7, mode=mode)
    # 순열 불변
    for _ in range(3):
        sh = items[:]
        rng.shuffle(sh)
        assert rank(sh, NOW, cap=7, mode=mode) == base
    # 멱등: 같은 입력 반복·자기 자신과 합쳐도 같다
    assert rank(items, NOW, cap=7, mode=mode) == base
    assert rank(items + items, NOW, cap=7, mode=mode) == base
    # 손실 없음: ranked ∪ excluded == 입력(병합 후), 서로소, top+나머지 == all
    keys_in = {i.key for i in items}
    ranked_keys = [k for d in base.all for k in d["keys"]]
    assert len(ranked_keys) == len(set(ranked_keys))
    excluded_members = set()
    for rep in base.excluded:
        assert rep in keys_in
        excluded_members.add(rep)
    # 제외된 대표의 병합 멤버까지 포함해 확인: 제외 대표 key는 ranked에 없다
    assert not (excluded_members & set(ranked_keys))
    assert {d["id"] for d in base.all}.isdisjoint(base.excluded)
    assert set(ranked_keys) | excluded_members <= keys_in
    # 병합으로 사라진 key는 같은 그룹의 대표가 가진다 -> 모든 입력 key는 어딘가에 속함
    assert base.top == base.all[:7] and len(base.top) <= 7
    assert base.rest_count == sum(1 for d in base.all[7:] if d["tier"] != "rest")
    tiers = [priority._TIER_IDX[d["tier"]] for d in base.all]
    assert tiers == sorted(tiers)
    assert {d["tier"] for d in base.all} <= priority._MODE_TIERS[mode]


def test_no_item_lost_exact_accounting():
    rng = random.Random(99)
    items = _random_items(rng, 80)
    r = rank(items, NOW, cap=5, mode="morning")
    accounted = set(k for d in r.all for k in d["keys"])
    # 제외 항목의 멤버: morning에서 제외되는 것은 종료된 일정뿐 -> 입력에서 직접 계산
    ended = {i.key for i in items if (i.start_at or i.end_at) and (i.end_at or i.start_at) <= NOW}
    assert accounted | set(r.excluded) | ended >= {i.key for i in items}
    assert len(r.all) + len(r.excluded) == len(priority._groups(items))


# --- 정적 검사 ---
SRC = Path(priority.__file__).read_text(encoding="utf-8")


def test_priority_module_reads_text_only_for_output():
    tree = ast.parse(SRC)
    allowed = {"_validate", "_present", "_facts"}   # _facts: 완전 동률일 때 표시 문구를 고정하는 마지막 키
    bad = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr == "text" and fn.name not in allowed:
                bad.append((fn.name, node.lineno))
    assert bad == []
    # _present에서는 결과 dict 생성 한 곳에서만 text를 읽는다
    pres = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_present")
    lines = {n.lineno for n in ast.walk(pres) if isinstance(n, ast.Attribute) and n.attr == "text"}
    assert len(lines) == 1
    # _facts: 정렬 키의 마지막 요소로만(우선순위·단계 판정에는 쓰지 않음)
    assert ", m.text)" in SRC


def test_priority_module_is_pure_and_has_no_urgency_words():
    tree = ast.parse(SRC)
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imports.add(n.module)
    assert imports <= {"__future__", "collections.abc", "dataclasses", "datetime", "typing", "zoneinfo", "pa.core"}, imports
    for banned in ("now()", "utcnow", "sqlite3", "random", "open(", "requests", "pa.private", "counselor", "time.time"):
        assert banned not in SRC, banned
    for w in ("긴급", "ASAP", "URGENT", "급함", "사용자", "사용자"):
        assert w not in SRC and w not in Path(briefing_copy.__file__).read_text(encoding="utf-8"), w
    assert "rank" not in [a.arg for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) for a in fn.args.args]


def test_performance_5k_items():
    rng = random.Random(7)
    items = _random_items(rng, 5000)
    t = time.perf_counter()
    r = rank(items, NOW, cap=5, mode="morning")
    elapsed = time.perf_counter() - t
    assert elapsed < 2.0, elapsed
    assert len(r.top) == 5
    # 병합 사슬이 길어도 선형: 전부 한 링크로 연결
    chain = [I(f"task:{i}", links=frozenset({f"x:{i}", f"x:{i + 1}"})) for i in range(5000)]
    t = time.perf_counter()
    assert len(rank(chain, NOW, cap=5, mode="morning").all) == 1
    assert time.perf_counter() - t < 2.0


def test_inputs_not_mutated():
    items = [I("task:1", due_at=D(6, 9), links=frozenset({"a"}))]
    snap = list(items)
    rank(items, NOW, cap=2, mode="morning")
    assert items == snap
