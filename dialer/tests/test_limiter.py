"""pacing.limiter: CPM fixed-window counting, TTL, and fail-closed Redis.

fakeredis is not installed; a minimal in-memory fake stands in. The unix
minute is pinned via the module's _unix_minute hook so a test can never flake
across a real minute boundary.
"""

from __future__ import annotations

import pytest
from conftest import make_settings

from orchestrator.pacing import limiter


class FakeRedis:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self.fail = False

    def incr(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        if self.fail:
            raise ConnectionError("redis down")
        self.expires[key] = ttl


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(limiter, "_redis", lambda cfg: fake)
    monkeypatch.setattr(limiter, "_unix_minute", lambda: 29_000_000)
    return fake


def test_allows_up_to_cpm_then_refuses(fake_redis):
    cfg = make_settings(calls_per_minute=2)
    assert limiter.try_acquire_call_slot(cfg) is True
    assert limiter.try_acquire_call_slot(cfg) is True
    assert limiter.try_acquire_call_slot(cfg) is False
    assert limiter.try_acquire_call_slot(cfg) is False


def test_new_minute_resets_the_window(fake_redis, monkeypatch):
    cfg = make_settings(calls_per_minute=1)
    assert limiter.try_acquire_call_slot(cfg) is True
    assert limiter.try_acquire_call_slot(cfg) is False
    monkeypatch.setattr(limiter, "_unix_minute", lambda: 29_000_001)
    assert limiter.try_acquire_call_slot(cfg) is True


def test_key_shape_and_ttl(fake_redis):
    cfg = make_settings(calls_per_minute=5)
    limiter.try_acquire_call_slot(cfg)
    key = "pacing:cpm:29000000"
    assert fake_redis.counters == {key: 1}
    # TTL outlives the minute so a slow worker can't resurrect a dead window,
    # but Redis still garbage-collects it.
    assert fake_redis.expires[key] == 120


def test_redis_error_fails_closed(fake_redis):
    cfg = make_settings(calls_per_minute=100)
    fake_redis.fail = True
    assert limiter.try_acquire_call_slot(cfg) is False


def test_factory_error_fails_closed(monkeypatch):
    def boom(cfg):
        raise ConnectionError("cannot even connect")

    monkeypatch.setattr(limiter, "_redis", boom)
    monkeypatch.setattr(limiter, "_unix_minute", lambda: 29_000_000)
    assert limiter.try_acquire_call_slot(make_settings()) is False
