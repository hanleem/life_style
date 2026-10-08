"""비서 에이전트: 사용자의 짧은 질문·부탁에 답한다. 바깥에 영향을 주는 일(일정 쓰기, 장바구니)은 하지 않는다."""

REPLY_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["reply"],
}


async def ask(router, text: str, *, user_initiated: bool = True) -> tuple[str | None, bool]:
    """(답변, degraded). AI 가 실패하면 (None, True) — 규칙 대체 동작은 Stage 2 에서 붙인다."""
    res = await router.run("secretary_chat", "secretary", text, REPLY_SCHEMA, user_initiated=user_initiated)
    if res.degraded or not res.data or not isinstance(res.data.get("reply"), str):
        return None, True
    return res.data["reply"], False
