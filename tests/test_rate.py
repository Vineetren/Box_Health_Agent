import time

from box_health_agent.collectors.rate import RateCounter, RateCounterStore


def test_rate_basic():
    rc = RateCounter()
    t0 = 1000.0
    assert rc.rate(t0, 1000) is None
    assert rc.rate(t0 + 1, 1100) == 100.0


def test_rate_counter_reset():
    rc = RateCounter()
    t0 = 1000.0
    rc.rate(t0, 5000)
    assert rc.rate(t0 + 1, 100) == 100.0


def test_rate_store_keys():
    store = RateCounterStore()
    t = 2000.0
    assert store.rate("a", t, 0) is None
    assert store.rate("a", t + 2, 200) == 100.0
    assert store.rate("b", t, 0) is None