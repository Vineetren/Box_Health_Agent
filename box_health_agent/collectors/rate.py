"""Compute rates from monotonic counters with wrap/reset handling."""

from __future__ import annotations


class RateCounter:
  def __init__(self) -> None:
    self._last_value: float | None = None
    self._last_ts: float | None = None

  def rate(self, ts: float, value: float) -> float | None:
    if self._last_value is None or self._last_ts is None:
      self._last_value = value
      self._last_ts = ts
      return None
    dt = ts - self._last_ts
    if dt <= 0:
      return None
    delta = value - self._last_value
    if delta < 0:
      delta = value
    self._last_value = value
    self._last_ts = ts
    return delta / dt


class RateCounterStore:
  def __init__(self) -> None:
    self._counters: dict[str, RateCounter] = {}

  def rate(self, key: str, ts: float, value: float) -> float | None:
    if key not in self._counters:
      self._counters[key] = RateCounter()
    return self._counters[key].rate(ts, value)