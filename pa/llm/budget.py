from datetime import timedelta

from pa.clock import utcnow, utcnow_iso
from pa.ids import new_id


class Budget:
    def __init__(self, con, per_5h: int, reserve_for_user: int, *, per_day_web: int = 6):
        self.con, self.per_5h, self.reserve = con, per_5h, reserve_for_user
        self.per_day_web = per_day_web
        self._cooldown_until = None

    def _used(self) -> int:
        since = (utcnow() - timedelta(hours=5)).isoformat()
        return self.con.execute("SELECT COUNT(*) FROM llm_call WHERE tier IN ('ai','ai_web') AND at>=?",
                                (since,)).fetchone()[0]

    def allow(self, user_initiated: bool) -> bool:
        if self._cooldown_until and utcnow() < self._cooldown_until:
            return False
        limit = self.per_5h if user_initiated else self.per_5h - self.reserve
        return self._used() < limit

    def allow_web(self, user_initiated: bool) -> bool:
        if not self.allow(user_initiated):
            return False
        since = (utcnow() - timedelta(hours=24)).isoformat()
        n = self.con.execute("SELECT COUNT(*) FROM llm_call WHERE tier='ai_web' AND at>=?", (since,)).fetchone()[0]
        return n < self.per_day_web

    def cooldown(self, minutes: int) -> None:
        self._cooldown_until = utcnow() + timedelta(minutes=minutes)

    def record(self, task: str, tier: str, ok: bool, user_initiated: bool, usage: dict | None,
               duration_ms: int, fallback_from: str | None = None) -> None:
        u = usage or {}
        self.con.execute("INSERT INTO llm_call(id,at,task,tier,input_tokens,output_tokens,duration_ms,ok,user_initiated,fallback_from) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (new_id(), utcnow_iso(), task, tier, u.get("input_tokens"), u.get("output_tokens"),
                          duration_ms, int(ok), int(user_initiated), fallback_from))
