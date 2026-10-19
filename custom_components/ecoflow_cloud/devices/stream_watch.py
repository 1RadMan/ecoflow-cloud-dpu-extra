"""Watchdog for a device's live push stream.

App-API devices such as the Smart Gateway and the DELTA Pro Ultra X push a
frame on their property topic every ~2 s while they are being watched (the
gateway at ~2.25 s once this integration's "get all" refresh has put it in its
fast reporting profile). A pause much longer than that means the device, or
EcoFlow's cloud, has stopped passing its data on, and every entity silently
keeps its last value.

Captures from a gateway showed two ways this hides real events:

* Push pauses of 28 s to 4.4 min, during which the "get all" replies kept
  arriving within ~100 ms but carried the same frozen snapshot: byte-identical
  to each other, and equal to what the last push had already said. Merged as
  fresh data, they keep the device "online" with every value frozen.
* Replies with no payload at all (one header, cmd 0/0).

So, per watched device:

* Pushes (frames on the data topic) are timestamped. No push for
  ``stall_sec`` = "telemetry stalled", with an HA event when it starts and when
  the stream resumes (duration, plus what the refreshes got back).
* A get_reply that arrives while pushes are stalled and changes no value we
  already have is a replay of stale state: it is not merged and doesn't count
  as data (see BaseDevice._screen_reply). "Data stale" = stalled, and neither a
  push nor a reply with new values for longer than ``stall_sec``.
* Devices that answer "get all" (``refresh``) are asked again REFRESH_AFTER_SEC
  into a stall and then every REFRESH_EVERY_SEC for up to REFRESH_FOR_SEC,
  rather than waiting for their regular refresh; a device can also ask for one
  at once (request_refresh), e.g. when it reports a slow reporting profile.

The decisions are made in tick(), on the event loop; the on_* hooks are called
from the MQTT thread and only record times and counts.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.util import dt

_LOGGER = logging.getLogger(__name__)

EVENT_STREAM_STALLED = "ecoflow_cloud_stream_stalled"
EVENT_STREAM_RESUMED = "ecoflow_cloud_stream_resumed"

REPLY_FRESH = "fresh"
REPLY_REPLAYED = "replayed"
REPLY_EMPTY = "empty"

WATCH_TICK_SEC = 1.0
REFRESH_AFTER_SEC = 10.0
REFRESH_EVERY_SEC = 5.0
REFRESH_FOR_SEC = 180.0


@dataclasses.dataclass
class TickResult:
    refresh: bool = False
    changed: bool = False
    events: list[tuple[str, dict[str, Any]]] = dataclasses.field(default_factory=list)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


class StreamWatch:
    def __init__(self, sn: str, stall_sec: float, refresh: bool) -> None:
        self.sn = sn
        self.stall_sec = stall_sec
        self.refresh = refresh
        self.reset(dt.utcnow())
        self._listeners: list[Callable[[], None]] = []

    def reset(self, now: datetime) -> None:
        """Start (or restart) watching: the reference point counts as a push."""
        self.last_push: datetime = now
        self.last_fresh: datetime = now  # last push or reply that carried new values
        self.stalled = False
        self.data_stale = False
        self.stalled_since: datetime | None = None  # time of the last push before the stall
        self.last_stall_sec: float | None = None
        self.counts: dict[str, int] = {}  # this (or the last) stall's requests and replies
        self._refresh_soon = False
        self._last_refresh: datetime | None = None
        self._request_open = False
        self._request_time: datetime | None = None

    # --- called from the MQTT thread ------------------------------------

    def push_stalled(self, now: datetime | None = None) -> bool:
        return ((now or dt.utcnow()) - self.last_push).total_seconds() > self.stall_sec

    def on_push(self) -> None:
        self.last_push = self.last_fresh = dt.utcnow()

    def on_request(self) -> None:
        """A "get all" request went out (ours, seen coming back on the get topic)."""
        if self.push_stalled():
            if self._request_open:
                self._count("replies_missing")
            self._count("refresh_requests")
        self._request_open = True
        self._request_time = dt.utcnow()

    def on_reply(self, kind: str) -> None:
        self._request_open = False
        if kind == REPLY_FRESH:
            self.last_fresh = dt.utcnow()
        if kind == REPLY_EMPTY:
            _LOGGER.info("EcoFlow %s: reply to a refresh request carried no data", self.sn)
        if self.push_stalled():
            self._count(f"replies_{kind}")
            _LOGGER.debug("EcoFlow %s: %s reply while the push stream is stalled", self.sn, kind)

    def request_refresh(self) -> None:
        self._refresh_soon = True

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    # --- called from the event loop -------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def tick(self, now: datetime) -> TickResult:
        result = TickResult()
        age = (now - self.last_push).total_seconds()

        if not self.stalled and age > self.stall_sec:
            self.stalled, self.stalled_since, self.counts = True, self.last_push, {}
            result.changed = True
            result.events.append((EVENT_STREAM_STALLED, {"serial": self.sn, "last_push": _iso(self.last_push)}))
            _LOGGER.info("EcoFlow %s: no data pushed since %s (%.0f s)", self.sn, _iso(self.last_push), age)
        elif self.stalled and age <= self.stall_sec:
            assert self.stalled_since is not None
            if (
                self._request_open
                and self._request_time is not None
                and (now - self._request_time).total_seconds() > REFRESH_EVERY_SEC
            ):
                self._count("replies_missing")  # a request from the stall that was never answered
                self._request_open = False
            self.last_stall_sec = round((self.last_push - self.stalled_since).total_seconds(), 1)
            data = {
                "serial": self.sn,
                "stalled_since": _iso(self.stalled_since),
                "resumed_at": _iso(self.last_push),
                "duration_sec": self.last_stall_sec,
                **{k: self.counts.get(k, 0) for k in self.COUNT_KEYS},
            }
            self.stalled = False
            result.changed = True
            result.events.append((EVENT_STREAM_RESUMED, data))
            _LOGGER.info(
                "EcoFlow %s: push stream resumed after %.1f s (%s)",
                self.sn,
                self.last_stall_sec,
                ", ".join(f"{k} {data[k]}" for k in self.COUNT_KEYS),
            )

        stale = self.stalled and (now - self.last_fresh).total_seconds() > self.stall_sec
        if stale != self.data_stale:
            self.data_stale = stale
            result.changed = True

        if self.refresh:
            due = self._last_refresh is None or (now - self._last_refresh).total_seconds() >= REFRESH_EVERY_SEC
            in_window = REFRESH_AFTER_SEC <= age <= REFRESH_AFTER_SEC + REFRESH_FOR_SEC
            if due and (self._refresh_soon or in_window):
                self._refresh_soon = False
                self._last_refresh = now
                result.refresh = True
        return result

    COUNT_KEYS = ("refresh_requests", "replies_fresh", "replies_replayed", "replies_empty", "replies_missing")

    def attributes(self) -> dict[str, Any]:
        return {
            "stalled_since": _iso(self.stalled_since) if self.stalled else None,
            "last_stall_duration": self.last_stall_sec,
            **{k: self.counts.get(k, 0) for k in self.COUNT_KEYS},
        }
