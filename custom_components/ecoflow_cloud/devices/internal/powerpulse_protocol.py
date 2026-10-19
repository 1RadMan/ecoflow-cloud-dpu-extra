"""PowerPulse EV Charger (9.6 kW, US): message fields, decoded from captures.

Kept free of Home Assistant imports so it can be exercised on its own.

The charger speaks DP3 envelopes (see powerpulse_capture.py). Its own frames
come from src 2 with these cmd_func/cmd_id pairs; the meanings below were read
off captures of a charger on a Smart Gateway's inlet port AC1, with a 2019
Model X: idle, plugged in, failed starts, successful sessions in Custom (6 A)
and Solar and Battery modes, a user stop, and the app changing mode and
current. Fields not listed are not understood yet.

2/33 status (a full frame on every change, and in get_reply snapshots):
  1    state: 1 unplugged, 2 connected, 3 charging, 4 fault, 6 stopped.
       A failed start goes 3 -> 4, then back to 3 when the charger retries.
  2    active fault codes, as a packed list of uint32 bitmap words: code
       = word * 32 + bit + 1. Checked against 2/131's event code (81, 83).
       2/40 field 1 carries the same list.
  9    lifetime energy delivered, Wh (82 -> 108 after a 26 Wh session).
  16   charging mode: 1 Fast Charge, 2 Custom, 3 Solar and Battery.
  17   Custom-mode current, 0.1 A (60 = 6 A; the app's 16/32/40 A -> 160/320/400).
  18   maximum current, 0.1 A (400).
  24   control pilot level, V: 12 no vehicle, 9 connected, 6 vehicle asking
       for power, 0 while the charger shuts off.
  25   control pilot duty cycle, %: 100 when not offering power, ~10-11 at 6 A.
  28   output power, W (F32; sent as varint 0 when idle).
  29   input voltage, V (4-byte little-endian float in a LEN field).
  30   output current, A (same encoding).
  40   current/last session start, Unix time.
  41   seconds since the session started.
  42   session energy, Wh.
  101  session phase: 0 idle, 2 charging, 3 stopped, 5 fault.
2/2 live meter (every ~3 s while a session runs):
  1    output power, W (F32; omitted when 0).
  3    output current, A (4-byte float).
  4    voltage, V (4-byte float).
2/41: 7 = Wi-Fi signal, dBm.
2/131 last event: 1 = {1: start, 2: end (Unix time, fixed32), 3: code}.
240/210: 105 = 1 while power flows; 203.2 = power the system currently
  allots the charger, W (6 A x 240 V = 1440 in Custom 6 A; varies in Solar and
  Battery mode); 203.3 = rated power (9600).

Commands (the app's, replayed as-is): cmd 2/81 to dest 2, fields
  4 = 1 start charging, 2 stop charging;
  7 = charging mode (as 2/33 field 16);
  5 = Custom-mode current, 0.1 A, always sent with 7 = 2 (the app does so).
The charger acknowledges each with an empty 2/81 set_reply carrying the
command's seq.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

WIRE_VARINT, WIRE_F64, WIRE_LEN, WIRE_F32 = 0, 1, 2, 5

# Charger state (2/33 field 1).
STATE_UNPLUGGED, STATE_CONNECTED, STATE_CHARGING, STATE_FAULT, STATE_STOPPED = 1, 2, 3, 4, 6
STATE_NAMES: dict[int, str] = {
    STATE_UNPLUGGED: "unplugged",
    STATE_CONNECTED: "connected",
    STATE_CHARGING: "charging",
    STATE_FAULT: "fault",
    STATE_STOPPED: "stopped",
}
STATE_OPTIONS = [*STATE_NAMES.values(), "unknown"]

MODES: dict[str, int] = {"Fast Charge": 1, "Custom": 2, "Solar and Battery": 3}
MODE_CUSTOM = 2

CURRENT_MIN_A, CURRENT_MAX_A = 6, 40

# Fault codes the EcoFlow app names.
FAULT_NAMES: dict[int, str] = {19: "Electrical leakage", 83: "Unknown error"}

# Command fields (cmd 2/81).
CMD_FUNC, CMD_ID, CMD_DEST = 2, 81, 2
CMD_CHARGE, CMD_CURRENT, CMD_MODE = 4, 5, 7
CHARGE_START, CHARGE_STOP = 1, 2


# --------------------------------------------------------------------------- #
# Low-level readers
# --------------------------------------------------------------------------- #


def _read_varint(b: bytes, i: int) -> tuple[int, int] | None:
    r = s = 0
    while i < len(b) and s < 70:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i
    return None


def parse_fields(b: bytes) -> dict[int, list[tuple[int, Any]]]:
    """{field_no: [(wire_type, raw value), ...]}; stops at the first malformed byte."""
    out: dict[int, list[tuple[int, Any]]] = {}
    i, n = 0, len(b)
    while i < n:
        tag = _read_varint(b, i)
        if tag is None:
            break
        t, i = tag
        fn, wt = t >> 3, t & 7
        v: Any
        if wt == WIRE_VARINT:
            r = _read_varint(b, i)
            if r is None:
                break
            v, i = r
        elif wt == WIRE_LEN:
            r = _read_varint(b, i)
            if r is None or r[1] + r[0] > n:
                break
            ln, i = r
            v, i = b[i : i + ln], i + ln
        elif wt == WIRE_F32 and i + 4 <= n:
            v, i = b[i : i + 4], i + 4  # raw: a float or a uint32, depending on the field
        elif wt == WIRE_F64 and i + 8 <= n:
            v, i = b[i : i + 8], i + 8
        else:
            break
        out.setdefault(fn, []).append((wt, v))
    return out


def _num(fields: dict[int, list[tuple[int, Any]]], no: int) -> float | int | None:
    """A field's numeric value, whichever way the charger encoded it."""
    vals = fields.get(no)
    if not vals:
        return None
    wt, v = vals[0]
    if wt == WIRE_VARINT:
        return v - (1 << 64) if v >= (1 << 63) else v
    if wt == WIRE_F32:
        return struct.unpack("<f", v)[0]
    if wt == WIRE_F64:
        return struct.unpack("<d", v)[0]
    if wt == WIRE_LEN and len(v) == 4:
        return struct.unpack("<f", v)[0]
    return None


def _uint(fields: dict[int, list[tuple[int, Any]]], no: int) -> int | None:
    """A field as an unsigned integer: a varint, or a fixed32 (the event log's
    timestamps are fixed32, not floats)."""
    vals = fields.get(no)
    if not vals:
        return None
    wt, v = vals[0]
    if wt == WIRE_VARINT:
        return int(v)
    if wt == WIRE_F32:
        return struct.unpack("<I", v)[0]
    return None


def _sub(fields: dict[int, list[tuple[int, Any]]], no: int) -> dict[int, list[tuple[int, Any]]]:
    vals = fields.get(no)
    if not vals or vals[0][0] != WIRE_LEN:
        return {}
    return parse_fields(vals[0][1])


def fault_codes(b: bytes) -> list[int]:
    """Active codes from a packed list of uint32 bitmap words (code = word*32 + bit + 1)."""
    codes: list[int] = []
    i, word = 0, 0
    while i < len(b):
        r = _read_varint(b, i)
        if r is None:
            break
        v, i = r
        codes.extend(word * 32 + bit + 1 for bit in range(32) if v >> bit & 1)
        word += 1
    return codes


def describe_codes(codes: list[int]) -> str:
    return ", ".join(f"{c} ({FAULT_NAMES[c]})" if c in FAULT_NAMES else str(c) for c in codes) or "none"


def _timestamp(v: float | int | None) -> datetime | None:
    """Unix time -> datetime; None for anything outside 2001..2100 (unset or
    placeholder values such as a NaN pattern)."""
    if v is None or not 1_000_000_000 <= v <= 4_102_444_800:
        return None
    return datetime.fromtimestamp(int(v), UTC)


# --------------------------------------------------------------------------- #
# Frame -> named state
# --------------------------------------------------------------------------- #


def decode_state(cmd_func: int, cmd_id: int, pdata: bytes) -> dict[str, Any]:
    """Named values from one of the charger's own payloads (empty if not understood)."""
    f = parse_fields(pdata)
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None:
            out[key] = value

    if (cmd_func, cmd_id) == (2, 33):
        state = _num(f, 1)
        if state is not None:
            out["pp_state"] = int(state)
            out["pp_charging"] = 1 if state in (STATE_CHARGING, STATE_FAULT) else 0
        faults = f.get(2)
        if faults and faults[0][0] == WIRE_LEN:
            codes = fault_codes(faults[0][1])
            out["pp_fault_codes"] = describe_codes(codes)
            out["pp_fault"] = 1 if codes else 0
        put("pp_total_energy", _num(f, 9))
        put("pp_mode", _num(f, 16))
        cur = _num(f, 17)
        put("pp_custom_current", round(cur / 10, 1) if cur is not None else None)
        mx = _num(f, 18)
        put("pp_max_current", round(mx / 10, 1) if mx is not None else None)
        put("pp_pilot_voltage", _num(f, 24))
        put("pp_pilot_duty", _num(f, 25))
        power = _num(f, 28)
        put("pp_power", round(float(power), 1) if power is not None else None)
        volt = _num(f, 29)
        put("pp_voltage", round(volt, 1) if volt is not None else None)
        amp = _num(f, 30)
        put("pp_current", round(amp, 1) if amp is not None else None)
        put("pp_session_start", _timestamp(_uint(f, 40)))
        put("pp_session_energy", _num(f, 42))
        put("pp_session_phase", _num(f, 101))
    elif (cmd_func, cmd_id) == (2, 2):
        # Live meter: a zero power is left out of the frame.
        power = _num(f, 1)
        out["pp_power"] = round(float(power), 1) if power is not None else 0.0
        amp = _num(f, 3)
        put("pp_current", round(amp, 1) if amp is not None else None)
        volt = _num(f, 4)
        if volt:  # 0 between sessions: keep the last real reading
            out["pp_voltage"] = round(volt, 1)
    elif (cmd_func, cmd_id) == (2, 40):
        faults = f.get(1)
        if faults and faults[0][0] == WIRE_LEN:
            codes = fault_codes(faults[0][1])
            out["pp_fault_codes"] = describe_codes(codes)
            out["pp_fault"] = 1 if codes else 0
    elif (cmd_func, cmd_id) == (2, 41):
        put("pp_wifi_rssi", _num(f, 7))
    elif (cmd_func, cmd_id) == (2, 131):
        ev = _sub(f, 1)
        code = _num(ev, 3)
        if code is not None:
            out["pp_last_event_code"] = int(code)
            out["pp_last_event"] = describe_codes([int(code)])
            put("pp_last_event_start", _timestamp(_uint(ev, 1)))
            put("pp_last_event_end", _timestamp(_uint(ev, 2)))
    elif (cmd_func, cmd_id) == (240, 210):
        put("pp_power_flowing", _num(f, 105))
        put("pp_allotted_power", _num(_sub(f, 203), 2))
    return out


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def command_pdata(values: dict[int, int]) -> bytes:
    """A 2/81 payload: varint fields in ascending order, as the app sends them."""
    return b"".join(_varint(no << 3 | WIRE_VARINT) + _varint(v) for no, v in sorted(values.items()))


def command_fields(pdata: bytes) -> dict[int, int]:
    """The varint fields of a 2/81 command payload (the app's or our own)."""
    out: dict[int, int] = {}
    for no, vals in parse_fields(pdata).items():
        if vals and vals[0][0] == WIRE_VARINT:
            out[no] = vals[0][1]
    return out


def charge_request(fields: dict[int, int]) -> str | None:
    """ "start" / "stop" for a command carrying field 4, else None."""
    return {CHARGE_START: "start", CHARGE_STOP: "stop"}.get(fields.get(CMD_CHARGE, 0))


def charge_values(on: bool) -> dict[int, int]:
    return {CMD_CHARGE: CHARGE_START if on else CHARGE_STOP}


def mode_values(mode: int, custom_current_a: float | None) -> dict[int, int]:
    """Switching to Custom carries the current, as the app's command does."""
    if mode == MODE_CUSTOM:
        return current_values(custom_current_a if custom_current_a else CURRENT_MIN_A)
    return {CMD_MODE: mode}


def current_values(amps: float) -> dict[int, int]:
    amps = min(max(float(amps), CURRENT_MIN_A), CURRENT_MAX_A)
    return {CMD_CURRENT: int(round(amps * 10)), CMD_MODE: MODE_CUSTOM}
