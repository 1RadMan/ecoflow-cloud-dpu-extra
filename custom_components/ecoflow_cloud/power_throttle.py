"""Decide when a new reading is worth publishing to Home Assistant.

EcoFlow devices push readings every few seconds, and the values jitter. Publishing
every change writes a recorder row on nearly every update: a single Smart Gateway
circuit can produce more than 10,000 rows a day. A reading is only published
straight away when it has moved meaningfully; smaller differences are still
published at least once per heartbeat, so graphs, 5-minute statistics and the
integrated kWh sensors stay accurate.

Kept free of Home Assistant imports so the rules can be tested on their own.
"""

from datetime import datetime, timedelta
from typing import Any

# Power sensors (W): publish straight away when a reading moves by at least this
# many watts...
MIN_DELTA_W = 5.0
# ...or by this percentage of the last published value, whichever is larger.
MIN_DELTA_PCT = 2.0
# Any smaller difference is published once this much time has passed.
HEARTBEAT = timedelta(seconds=60)

# Remaining-time sensors (minutes): the devices recalculate these every few seconds
# from the instantaneous load, so they swing by a few minutes at a time.
REMAIN_MIN_DELTA_MIN = 5.0
REMAIN_MIN_DELTA_PCT = 5.0
REMAIN_HEARTBEAT = timedelta(minutes=5)

# Integrated kWh sensors are written at most this often. The integral itself is
# still updated on every power reading, so totals are unaffected.
ENERGY_WRITE_INTERVAL = timedelta(seconds=60)


def should_publish(
    old: Any,
    new: float,
    last_published_at: datetime | None,
    now: datetime,
    min_delta: float,
    min_delta_pct: float,
    heartbeat: timedelta,
) -> bool:
    """Return True when ``new`` should replace the last published value ``old``."""
    if last_published_at is None or isinstance(old, bool) or not isinstance(old, (int, float)):
        return True
    if new == old:
        return False
    # Starting or stopping (to or from 0) is always shown immediately.
    if (old == 0) != (new == 0):
        return True
    threshold = max(min_delta, abs(old) * min_delta_pct / 100)
    if abs(new - old) >= threshold:
        return True
    return now - last_published_at >= heartbeat


def should_publish_power(
    old: Any,
    new: float,
    last_published_at: datetime | None,
    now: datetime,
    min_delta_w: float = MIN_DELTA_W,
    min_delta_pct: float = MIN_DELTA_PCT,
    heartbeat: timedelta = HEARTBEAT,
) -> bool:
    """The publish rule with the power-sensor defaults."""
    return should_publish(old, new, last_published_at, now, min_delta_w, min_delta_pct, heartbeat)


def should_write(
    last_written_at: datetime | None,
    now: datetime,
    interval: timedelta = ENERGY_WRITE_INTERVAL,
) -> bool:
    """Return True when an integrated energy sensor may write its state again."""
    return last_written_at is None or now - last_written_at >= interval
