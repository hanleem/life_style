"""웹 푸시(VAPID): 키 자동 생성, 구독 저장, 알림과 같은 지점에서 전송, 실패는 알림을 막지 않는다."""
import base64
import json

import pytest

from pa.core import devices, notify, push


@pytest.fixture(autouse=True)
def _reset_push():
    push.configure(None)
    yield
    push.configure(None)


SUB = {"endpoint": "https://web.push.apple.com/abc123", "keys": {"p256dh": "BPubKey_-1", "auth": "AuthKey_-1"}}


def test_vapid_keys_are_created_once_and_reused(tmp_path):
    pub1 = push.ensure_keys(tmp_path)
    pub2 = push.ensure_keys(tmp_path)
    assert pub1 == pub2 and (tmp_path / push.KEY_FILE).exists()
    raw = base64.urlsafe_b64decode(pub1 + "=" * (-len(pub1) % 4))
    assert len(raw) == 65 and raw[0] == 4                       # 압축 안 된 P-256 공개키(브라우저 applicationServerKey 형식)
    assert "=" not in pub1


def test_private_key_file_is_not_world_readable_on_posix(tmp_path):
    import os
    push.ensure_keys(tmp_path)
    if os.name == "posix":
        assert (tmp_path / push.KEY_FILE).stat().st_mode & 0o077 == 0


def test_subscribe_stores_and_replaces_by_endpoint(con):
    d = devices.add_device(con, "폰")
    sid = push.subscribe(con, d, SUB)
    assert sid and con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 1
    again = push.subscribe(con, d, {**SUB, "keys": {"p256dh": "BNew", "auth": "ANew"}})
    row = con.execute("SELECT p256dh, auth FROM push_subscription").fetchone()
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 1 and row["p256dh"] == "BNew" and again


@pytest.mark.parametrize("bad", [
    {"endpoint": "http://web.push.apple.com/x", "keys": SUB["keys"]},            # https 만
    {"endpoint": "https://127.0.0.1/x", "keys": SUB["keys"]},
    {"endpoint": "https://localhost/x", "keys": SUB["keys"]},
    {"endpoint": "https://evil.example.com/x", "keys": SUB["keys"]},             # 알려진 푸시 서비스만(서버가 이 주소로 요청한다)
    {"endpoint": "https://web.push.apple.com.evil.test/x", "keys": SUB["keys"]},
    {"endpoint": "https://web.push.apple.com/x", "keys": {"p256dh": "", "auth": "a"}},
    {"endpoint": "https://web.push.apple.com/x", "keys": {"p256dh": "k"}},
    {"endpoint": "https://web.push.apple.com/x", "keys": {"p256dh": "k!!", "auth": "a"}},
    {"endpoint": "x" * 600, "keys": SUB["keys"]}, {}, {"endpoint": 5}, None, "x",
])
def test_bad_subscriptions_are_rejected(con, bad):
    d = devices.add_device(con, "폰")
    with pytest.raises(ValueError):
        push.subscribe(con, d, bad)
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


@pytest.mark.parametrize("host", ["web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com",
                                  "wns2-par02p.notify.windows.com"])
def test_known_push_services_are_accepted(con, host):
    d = devices.add_device(con, "폰")
    assert push.subscribe(con, d, {"endpoint": f"https://{host}/x", "keys": SUB["keys"]})


def test_unsubscribe_removes_only_that_device(con):
    a, b = devices.add_device(con, "a"), devices.add_device(con, "b")
    push.subscribe(con, a, SUB)
    push.subscribe(con, b, {**SUB, "endpoint": "https://web.push.apple.com/other"})
    assert push.unsubscribe(con, a, SUB["endpoint"]) is True
    assert push.unsubscribe(con, a, SUB["endpoint"]) is False
    assert con.execute("SELECT device_id FROM push_subscription").fetchone()[0] == b


class Sent:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, sub, payload, key_path, contact):
        self.calls.append((sub, json.loads(payload), key_path, contact))
        if self.exc:
            raise self.exc


def _configure(tmp_path, sender):
    push.ensure_keys(tmp_path)
    push.configure(push.Config(data_dir=tmp_path, contact="mailto:admin@example.com", sender=sender))


async def test_nothing_happens_when_push_is_not_configured(con):
    d = devices.add_device(con, "폰")
    push.subscribe(con, d, SUB)
    assert await push.deliver(con, "제목", "/") == 0


async def test_deliver_sends_to_every_subscription_with_title_and_link_only(con, tmp_path):
    sent = Sent()
    _configure(tmp_path, sent)
    a, b = devices.add_device(con, "a"), devices.add_device(con, "b")
    push.subscribe(con, a, SUB)
    push.subscribe(con, b, {**SUB, "endpoint": "https://fcm.googleapis.com/z"})
    assert await push.deliver(con, "이번 주 식단이 나왔어요", "/meals") == 2
    assert [c[1] for c in sent.calls] == [{"title": "이번 주 식단이 나왔어요", "href": "/meals"}] * 2
    assert sent.calls[0][3] == "mailto:admin@example.com" and sent.calls[0][2].endswith(push.KEY_FILE)
    assert {c[0]["endpoint"] for c in sent.calls} == {SUB["endpoint"], "https://fcm.googleapis.com/z"}


async def test_gone_subscriptions_are_removed(con, tmp_path):
    class Gone(Exception):
        status_code = 410
    sent = Sent(Gone())
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    assert await push.deliver(con, "x", "/") == 0
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


async def test_flaky_subscription_is_dropped_after_repeated_failures_only(con, tmp_path):
    sent = Sent(RuntimeError("boom"))
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    for _ in range(push.MAX_FAILURES - 1):
        await push.deliver(con, "x", "/")
    assert con.execute("SELECT failed_count FROM push_subscription").fetchone()[0] == push.MAX_FAILURES - 1
    await push.deliver(con, "x", "/")
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


async def test_success_resets_the_failure_counter(con, tmp_path):
    sent = Sent(RuntimeError("boom"))
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    await push.deliver(con, "x", "/")
    sent.exc = None
    await push.deliver(con, "x", "/")
    assert con.execute("SELECT failed_count FROM push_subscription").fetchone()[0] == 0


async def test_failure_messages_never_log_the_endpoint_or_payload(con, tmp_path, caplog):
    sent = Sent(RuntimeError("https://web.push.apple.com/abc123 secret-body"))
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    with caplog.at_level("WARNING"):
        await push.deliver(con, "x", "/")
    assert "abc123" not in caplog.text and "secret-body" not in caplog.text and "RuntimeError" in caplog.text


async def test_notify_pushes_fixed_text_for_every_new_notification_once(con, tmp_path):
    sent = Sent()
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    assert await notify.send_notify(con, "meal_plan", 1, "k1") is True
    assert await notify.send_notify(con, "meal_plan", 1, "k1") is False          # 같은 키는 한 번만 → 푸시도 한 번
    assert [c[1] for c in sent.calls] == [{"title": "이번 주 식단이 나왔어요", "href": "/meals"}]


async def test_announce_pushes_a_generic_title_not_the_free_text(con, tmp_path):
    sent = Sent()
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    await notify.announce(con, "화장지 2개 남았어요 · 지난번 상품 링크 https://x", "ann1")
    assert sent.calls[0][1] == {"title": "챙길 일이 있습니다", "href": "/"}
    assert "화장지" not in json.dumps(sent.calls[0][1], ensure_ascii=False)


async def test_a_failing_push_never_breaks_the_notification(con, tmp_path):
    sent = Sent(RuntimeError("down"))
    _configure(tmp_path, sent)
    push.subscribe(con, devices.add_device(con, "a"), SUB)
    assert await notify.send_notify(con, "reminder", 1, "k2") is True
    assert [n["kind"] for n in notify.unread(con)] == ["reminder"]
