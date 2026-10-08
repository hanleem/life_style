import hashlib
import json


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def payload_hash(payload: dict, scheduled_send_at: str | None = None) -> str:
    raw = canonical_json(payload) + "|" + (scheduled_send_at or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
