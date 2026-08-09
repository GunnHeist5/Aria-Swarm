"""queueing.killswitch: two-store semantics and the fail-closed matrix.

Redis is faked with a minimal in-memory object (fakeredis is not installed);
kv_state is faked by monkeypatching orchestrator.db.kv_get/kv_set. The
interesting rows of the matrix: Redis errors count as ENGAGED unless the
durable kv record explicitly says released, and a Redis flush never releases
a switch that kv still holds.
"""

from __future__ import annotations

import pytest
from conftest import make_settings

import orchestrator.db
from orchestrator.queueing import killswitch


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def set(self, key, value):
        self._check()
        self.store[key] = value

    def delete(self, key):
        self._check()
        self.store.pop(key, None)


@pytest.fixture
def stores(monkeypatch):
    state = {"redis": FakeRedis(), "kv": {}, "kv_fail": False}

    def kv_get(cfg, key):
        if state["kv_fail"]:
            raise ConnectionError("postgres down")
        return state["kv"].get(key)

    def kv_set(cfg, key, value):
        if state["kv_fail"]:
            raise ConnectionError("postgres down")
        state["kv"][key] = value

    monkeypatch.setattr(killswitch, "_redis", lambda cfg: state["redis"])
    monkeypatch.setattr(orchestrator.db, "kv_get", kv_get)
    monkeypatch.setattr(orchestrator.db, "kv_set", kv_set)
    return state


@pytest.fixture
def cfg():
    return make_settings()


def test_fresh_system_is_not_engaged(stores, cfg):
    assert killswitch.is_engaged(cfg) is False


def test_engage_sets_both_stores(stores, cfg):
    killswitch.engage(cfg, "operator halt")
    assert killswitch.REDIS_KEY in stores["redis"].store
    assert stores["kv"][killswitch.KV_KEY]["engaged"] is True
    assert stores["kv"][killswitch.KV_KEY]["reason"] == "operator halt"
    assert killswitch.is_engaged(cfg) is True


def test_release_clears(stores, cfg):
    killswitch.engage(cfg, "halt")
    killswitch.release(cfg)
    assert killswitch.REDIS_KEY not in stores["redis"].store
    assert stores["kv"][killswitch.KV_KEY]["engaged"] is False
    assert killswitch.is_engaged(cfg) is False


def test_redis_flush_does_not_release(stores, cfg):
    # Postgres is authoritative; Redis is transport (architecture principle).
    killswitch.engage(cfg, "halt")
    stores["redis"].store.clear()
    assert killswitch.is_engaged(cfg) is True


def test_redis_error_fails_closed_with_no_kv_record(stores, cfg):
    stores["redis"].fail = True
    assert killswitch.is_engaged(cfg) is True


def test_redis_error_with_kv_engaged_is_engaged(stores, cfg):
    stores["kv"][killswitch.KV_KEY] = {"engaged": True, "reason": "x"}
    stores["redis"].fail = True
    assert killswitch.is_engaged(cfg) is True


def test_redis_error_with_explicit_kv_release_is_not_engaged(stores, cfg):
    # The one documented exception: the durable authority explicitly says
    # released, so a Redis outage alone does not halt dialing.
    stores["kv"][killswitch.KV_KEY] = {"engaged": False}
    stores["redis"].fail = True
    assert killswitch.is_engaged(cfg) is False


def test_both_stores_down_is_engaged(stores, cfg):
    stores["redis"].fail = True
    stores["kv_fail"] = True
    assert killswitch.is_engaged(cfg) is True


def test_engage_survives_redis_failure(stores, cfg):
    stores["redis"].fail = True
    killswitch.engage(cfg, "halt")  # kv write succeeded — no raise
    stores["redis"].fail = False
    assert killswitch.is_engaged(cfg) is True


def test_engage_survives_kv_failure(stores, cfg):
    stores["kv_fail"] = True
    killswitch.engage(cfg, "halt")  # redis write succeeded — no raise
    stores["kv_fail"] = False
    assert killswitch.is_engaged(cfg) is True  # redis flag present


def test_engage_raises_when_nothing_can_record_it(stores, cfg):
    stores["redis"].fail = True
    stores["kv_fail"] = True
    with pytest.raises(RuntimeError):
        killswitch.engage(cfg, "halt")


def test_release_requires_the_durable_store(stores, cfg):
    killswitch.engage(cfg, "halt")
    stores["kv_fail"] = True
    with pytest.raises(ConnectionError):
        killswitch.release(cfg)
    stores["kv_fail"] = False
    assert killswitch.is_engaged(cfg) is True  # release did not happen
