from datetime import timedelta

import pytest

from pa import clock
from pa.core import devices
from pa.settings import Settings


def test_join_token_is_single_use(con):
    t = devices.create_join_token(con)
    assert len(t) >= 30
    assert devices.consume_join_token(con, t) is True
    assert devices.consume_join_token(con, t) is False


def test_join_token_expires_after_five_minutes(con):
    t = devices.create_join_token(con)
    con.execute("UPDATE auth_nonce SET expires_at=? WHERE value=?", ((clock.utcnow() - timedelta(seconds=1)).isoformat(), t))
    assert devices.consume_join_token(con, t) is False
    fresh = devices.create_join_token(con)
    exp = con.execute("SELECT expires_at FROM auth_nonce WHERE value=?", (fresh,)).fetchone()[0]
    assert timedelta(minutes=4) < clock.utcnow().fromisoformat(exp) - clock.utcnow() <= timedelta(minutes=5, seconds=1)


def test_unknown_or_other_purpose_tokens_do_not_work(con):
    assert devices.consume_join_token(con, "nope") is False
    con.execute("INSERT INTO auth_nonce(value,purpose,expires_at) VALUES('n1','approve',?)", ((clock.utcnow() + timedelta(minutes=2)).isoformat(),))
    assert devices.consume_join_token(con, "n1") is False          # 승인용 nonce 로는 기기를 등록할 수 없다


def test_add_list_and_revoke_devices(con):
    a = devices.add_device(con, "엄마 아이폰")
    b = devices.add_device(con, "아빠 아이폰")
    assert devices.is_active(con, a) and devices.is_active(con, b)
    assert [d["name"] for d in devices.list_devices(con)] == ["엄마 아이폰", "아빠 아이폰"]
    assert devices.revoke(con, a) is True
    assert not devices.is_active(con, a) and devices.is_active(con, b)
    assert [d["name"] for d in devices.list_devices(con)] == ["아빠 아이폰"]
    assert devices.revoke(con, a) is False and devices.revoke(con, "nope") is False


def test_unknown_device_is_not_active(con):
    assert devices.is_active(con, None) is False and devices.is_active(con, "nope") is False


def test_device_name_is_cleaned_and_defaulted(con):
    assert devices.list_devices(con) == []
    devices.add_device(con, "  ")
    devices.add_device(con, "<b>x</b>" + "가" * 60)
    names = [d["name"] for d in devices.list_devices(con)]
    assert names[0] == "내 기기" and len(names[1]) <= 30


def test_revoking_removes_the_devices_push_subscriptions(con):
    a = devices.add_device(con, "폰")
    con.execute("INSERT INTO push_subscription(id,device_id,endpoint,p256dh,auth,created_at) VALUES('s1',?,?,?,?,'t')", (a, "https://p/1", "k", "a"))
    devices.revoke(con, a)
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


def test_touch_is_throttled(con):
    a = devices.add_device(con, "폰")
    t0 = clock.utcnow()
    devices.touch(con, a, t0)
    first = con.execute("SELECT last_seen_at FROM device WHERE id=?", (a,)).fetchone()[0]
    devices.touch(con, a, t0 + timedelta(minutes=3))
    assert con.execute("SELECT last_seen_at FROM device WHERE id=?", (a,)).fetchone()[0] == first
    devices.touch(con, a, t0 + timedelta(minutes=11))
    assert con.execute("SELECT last_seen_at FROM device WHERE id=?", (a,)).fetchone()[0] != first


def test_join_url_uses_public_url_or_the_tailnet_host():
    s = Settings(session_secret="t" * 32, public_url="https://home.example.test/")
    assert devices.join_url(s, "tok") == "https://home.example.test/join/tok"
    s2 = Settings(session_secret="t" * 32, allowed_hosts=["localhost", "127.0.0.1", "pc.example.test"])
    assert devices.join_url(s2, "tok") == "https://pc.example.test/join/tok"
    s3 = Settings(session_secret="t" * 32, allowed_hosts=["localhost", "127.0.0.1"])
    assert devices.join_url(s3, "tok") is None                      # 폰이 닿을 주소를 모르면 QR 을 만들지 않는다


def test_qr_svg_is_black_on_white_and_contains_no_script():
    svg = devices.qr_svg("https://home.example.test/join/tok")
    assert svg.lstrip().startswith("<svg") and "<script" not in svg
    low = svg.lower()
    assert ("#ffffff" in low or "#fff" in low) and ("#000000" in low or "#000" in low)
