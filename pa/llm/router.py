import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pa.clock import utcnow, utcnow_iso
from pa.llm.codex_cli import RateLimited

log = logging.getLogger("pa.llm")

# tier: "ai"(Codex CLI) / "ai_web"(웹 검색을 켠 Codex) / "cache" / "rule"(AI 실패 시 규칙 대체 동작).
TASK_TIER: dict[str, dict] = {
    "meal_plan": {"tier": "ai"},   # 주간 식단·레서피(웹 검색 없음)
    "summarize": {"tier": "ai"},
    "secretary_chat": {"tier": "ai"},
    "notice_extract": {"tier": "ai"},   # 가정통신문 사진 → 일정(Stage 6)
    "scout_search": {"tier": "ai_web", "allowed_tools": "WebSearch"},   # 나들이 추천에서만 웹 검색을 켠다
}

# 프롬프트의 {tone} 자리에 들어갈 말투 안내(인터뷰에서 friendly / polite 를 고른다)
TONES = {
    "friendly": "친근한 말투(해요체, 다정하고 편하게)",
    "polite": "공손한 존댓말(합니다체·해요체, 예의 바르게)",
}


@dataclass
class LLMResult:
    data: dict | None
    tier: str
    degraded: bool


class TaskRouter:
    """AI 호출 라우터. AI 가 실패하면 호출자가 넘긴 fallback(규칙 대체 동작)으로 채우고 tier="rule" 로 알린다.
    fallback 도 없으면 degraded=True, data=None."""

    def __init__(self, con, ai, budget, prompts_dir: Path):
        self.con, self.ai, self.budget, self.prompts_dir = con, ai, budget, prompts_dir
        self.tone: str = "polite"   # 프롬프트의 {tone} 자리에 들어갈 말투(인터뷰에서 정한다)

    def _cache_key(self, task: str, user_text: str, schema: dict | None = None) -> str:
        sch = json.dumps(schema, sort_keys=True, ensure_ascii=False) if schema else ""   # 스키마가 바뀌면 캐시도 새로
        return hashlib.sha256(f"{task}|{user_text}|{sch}|{self.tone}".encode()).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        row = self.con.execute("SELECT result_json FROM llm_cache WHERE key=? AND expires_at>?",
                               (key, utcnow_iso())).fetchone()
        return json.loads(row["result_json"]) if row else None

    def _cache_put(self, key: str, task: str, data: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO llm_cache(key,task,result_json,created_at,expires_at) VALUES(?,?,?,?,?)",
                         (key, task, json.dumps(data, ensure_ascii=False), utcnow_iso(),
                          (utcnow() + timedelta(hours=24)).isoformat()))

    def _system_text(self, system_name: str) -> str:
        """시스템 프롬프트 파일을 읽어 {tone} 을 채운다. 파일이 없으면 빈 문자열."""
        f = self.prompts_dir / f"{system_name}.md"
        if not f.exists():
            return ""
        return f.read_text(encoding="utf-8").replace("{tone}", TONES.get(self.tone, TONES["polite"]))

    async def run(self, task: str, system_name: str, user_text: str, schema: dict, *,
                  user_initiated: bool = False, fallback=None, images=()) -> LLMResult:
        """fallback: AI 를 못 쓸 때(한도·실패·예산) 규칙으로 dict 를 만들어 주는 인자 없는 함수."""
        tier = TASK_TIER[task]["tier"]
        key = self._cache_key(task, user_text, schema)
        if not user_initiated and not images and (cached := self._cache_get(key)) is not None:
            self.budget.record(task, "cache", True, user_initiated, None, 0)
            return LLMResult(cached, "cache", False)
        system = self._system_text(system_name)
        body = f"{user_text}\n\n다음 JSON 스키마로만 답하라:\n{json.dumps(schema, ensure_ascii=False)}"
        prompt = f"{system}\n\n{body}" if system else body
        web = tier == "ai_web"
        allowed = self.budget.allow_web(user_initiated) if web else self.budget.allow(user_initiated)
        if not allowed:
            log.warning("%s budget not allowed: %s", tier, task)
            return self._fallback(task, tier, fallback, user_initiated)
        t0 = time.monotonic()
        try:
            extra = {"images": list(images)} if images else {}
            data, usage = await self.ai.run_json(
                prompt, None, allowed_tools=TASK_TIER[task].get("allowed_tools", "") if web else "", **extra)
            self.budget.record(task, tier, True, user_initiated, usage, int((time.monotonic() - t0) * 1000))
            if not user_initiated and not images:
                self._cache_put(key, task, data)
            return LLMResult(data, tier, False)
        except RateLimited as e:
            log.warning("%s rate limited: %s", tier, type(e).__name__)
            self.budget.cooldown(30)
        except Exception as e:
            log.warning("%s failed (%s): %s", tier, task, type(e).__name__)
        self.budget.record(task, tier, False, user_initiated, None, int((time.monotonic() - t0) * 1000))
        return self._fallback(task, tier, fallback, user_initiated)

    def _fallback(self, task: str, tier: str, fallback, user_initiated: bool) -> LLMResult:
        """AI 를 못 쓸 때: fallback 이 있으면 규칙 결과(tier="rule"), 없으면 degraded."""
        if fallback is not None:
            try:
                data = fallback()
            except Exception as e:
                log.warning("rule fallback failed (%s): %s", task, type(e).__name__)
                data = None
            if data is not None:
                self.budget.record(task, "rule", True, user_initiated, None, 0, fallback_from=tier)
                return LLMResult(data, "rule", False)
        return LLMResult(None, tier, True)
