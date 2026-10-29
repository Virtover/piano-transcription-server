import fakeredis
import pytest

from app import slots
from app.slots import LeasedSlot, in_use


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_places_are_limited_across_holders(redis):
    first, second, third = (LeasedSlot(redis, "places", 2) for _ in range(3))

    assert first.try_take() and second.try_take()
    assert not third.try_take()
    assert in_use(redis, "places") == 2

    first.release()

    assert third.try_take()


def test_without_a_limit_every_holder_gets_a_place(redis):
    assert all(LeasedSlot(redis, "places", None).try_take() for _ in range(10))


def test_a_crashed_holders_place_frees_itself(redis, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(slots.time, "time", lambda: now)
    crashed = LeasedSlot(redis, "places", 1, lease=60)
    crashed._renew = lambda: None
    assert crashed.try_take()

    now += 61

    assert LeasedSlot(redis, "places", 1).try_take()


def test_taking_waits_until_the_timeout(redis):
    holder = LeasedSlot(redis, "places", 1)
    holder.try_take()

    assert not LeasedSlot(redis, "places", 1).take(timeout=0.05, poll=0.01)
