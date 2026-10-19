"""Schema-less frame decoding and on-disk capture for the PowerPulse profile.

Kept free of Home Assistant imports so it can be exercised on its own. The
PowerPulse device profile (powerpulse.py) feeds every MQTT frame it sees into
``decode_frame`` and, while a capture is running, into ``CaptureSession``.

Nothing about the PowerPulse's message layout is assumed. A frame is decoded
as JSON when it looks like JSON, otherwise as a DP3 header message (the
envelope the newer EcoFlow devices and the Smart Gateway use), with each
header's payload XOR-decoded the same way as the DELTA 3 profile does and then
walked as raw protobuf: field numbers, wire values, and nested messages where
the bytes parse cleanly as one. Anything else is kept as hex. The raw bytes
are always written to the capture file too, so a decoding guess that turns out
wrong never loses data.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import struct
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

# Bounds on the schema-less walk.
MAX_DEPTH = 8
# Entries kept in memory for the diagnostics download (the file keeps everything).
TIMELINE_MAX = 3000

_WIRE_VARINT, _WIRE_F64, _WIRE_LEN, _WIRE_F32 = 0, 1, 2, 5

# DP3 header fields copied into the decoded frame (pdata is decoded separately).
_HEADER_FIELDS = (
    "src",
    "dest",
    "d_src",
    "d_dest",
    "enc_type",
    "check_type",
    "cmd_func",
    "cmd_id",
    "data_len",
    "need_ack",
    "is_ack",
    "seq",
    "product_id",
    "version",
    "payload_ver",
    "time_snap",
    "is_rw_cmd",
    "is_queue",
    "ack_type",
    "code",
    "from",
    "module_sn",
    "device_sn",
)

# Header "from" values the EcoFlow app uses for its own commands.
APP_ORIGINS = frozenset({"ios", "android", "iOS", "Android"})


# --------------------------------------------------------------------------- #
# Raw protobuf walk
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


def parse_strict(b: bytes) -> list[tuple[int, int, Any]] | None:
    """Parse bytes as a protobuf message without a schema.

    Returns [(field_no, wire_type, value)] only if the whole buffer parses
    cleanly (no groups, no truncation, no field 0); otherwise None.
    """
    out: list[tuple[int, int, Any]] = []
    i, n = 0, len(b)
    while i < n:
        tag = _read_varint(b, i)
        if tag is None:
            return None
        t, i = tag
        fn, wt = t >> 3, t & 7
        if fn == 0:
            return None
        v: Any
        if wt == _WIRE_VARINT:
            r = _read_varint(b, i)
            if r is None:
                return None
            v, i = r
        elif wt == _WIRE_F64:
            if i + 8 > n:
                return None
            v, i = b[i : i + 8], i + 8
        elif wt == _WIRE_LEN:
            r = _read_varint(b, i)
            if r is None:
                return None
            ln, i = r
            if i + ln > n:
                return None
            v, i = b[i : i + ln], i + ln
        elif wt == _WIRE_F32:
            if i + 4 > n:
                return None
            v, i = b[i : i + 4], i + 4
        else:
            return None
        out.append((fn, wt, v))
    return out


def _plausible_float(f: float) -> bool:
    return math.isfinite(f) and (f == 0.0 or 1e-4 <= abs(f) <= 1e9)


def _is_printable(b: bytes) -> bool:
    return all(0x20 <= c < 0x7F for c in b)


def _render_len(b: bytes, depth: int) -> Any:
    if not b:
        return ""
    # Serials, names and version strings: plain ASCII text.
    if len(b) >= 4 and _is_printable(b):
        return b.decode("ascii")
    if depth < MAX_DEPTH:
        nested = parse_strict(b)
        if nested:
            return _render_fields(nested, depth + 1)
    try:
        text = b.decode("utf-8")
        if text.isprintable():
            return text
    except UnicodeDecodeError:
        pass
    return "0x" + b.hex()


def _render_value(wt: int, v: Any, depth: int) -> Any:
    if wt == _WIRE_VARINT:
        return v - (1 << 64) if v >= (1 << 63) else v
    if wt == _WIRE_F32:
        f = struct.unpack("<f", v)[0]
        return round(f, 4) if _plausible_float(f) else struct.unpack("<I", v)[0]
    if wt == _WIRE_F64:
        d = struct.unpack("<d", v)[0]
        return round(d, 6) if _plausible_float(d) else struct.unpack("<Q", v)[0]
    return _render_len(v, depth)


def _render_fields(fields: list[tuple[int, int, Any]], depth: int = 0) -> dict[str, Any]:
    """{field_no: value}, or {field_no: [values]} for a field that repeats."""
    grouped: dict[str, list[Any]] = {}
    for fn, wt, v in fields:
        grouped.setdefault(str(fn), []).append(_render_value(wt, v, depth))
    return {k: (vals[0] if len(vals) == 1 else vals) for k, vals in grouped.items()}


def schemaless(b: bytes) -> dict[str, Any] | str:
    """Decode a protobuf payload without a schema, or return it as hex."""
    if not b:
        return {}
    fields = parse_strict(b)
    if fields is None:
        return "0x" + b.hex()
    return _render_fields(fields)


# --------------------------------------------------------------------------- #
# Frame decoding
# --------------------------------------------------------------------------- #


def _xor(pdata: bytes, seq: int) -> bytes:
    k = seq & 0xFF
    return bytes(c ^ k for c in pdata)


def _maybe_base64(raw: bytes) -> bytes:
    stripped = raw.strip()
    if not stripped or len(stripped) % 4:
        return raw
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return raw


def _header_dict(header: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _HEADER_FIELDS:
        try:
            if header.HasField(name):
                out[name] = getattr(header, name)
        except ValueError:
            continue
    return out


def decode_frame(raw: bytes, header_message_cls: Any) -> dict[str, Any]:
    """Decode one MQTT payload into {"format": ..., ...}.

    - JSON: {"format": "json", "json": <object>}
    - DP3 envelope: {"format": "dp3", "headers": [{"key": "254_21",
      "header": {...}, "payload": <schema-less dict or hex>}, ...]}
    - otherwise: {"format": "raw"}
    """
    head = raw[:1]
    if head in (b"{", b"["):
        try:
            return {"format": "json", "json": json.loads(raw.decode("utf-8", errors="replace"))}
        except ValueError:
            pass

    headers = _decode_dp3(raw, header_message_cls)
    if headers:
        return {"format": "dp3", "headers": headers}
    unwrapped = _maybe_base64(raw)
    if unwrapped is not raw:
        headers = _decode_dp3(unwrapped, header_message_cls)
        if headers:
            return {"format": "dp3", "base64": True, "headers": headers}
    return {"format": "raw"}


def dp3_headers(raw: bytes, header_message_cls: Any) -> list[tuple[dict[str, Any], bytes]] | None:
    """The (header fields, decoded payload bytes) of each header in a DP3
    envelope, or None if the bytes aren't one. Payloads are XOR-decoded as the
    DELTA 3 profile does (enc_type 1, not sent by the app)."""
    if header_message_cls is None:
        return None
    try:
        msg = header_message_cls()
        msg.ParseFromString(raw)
    except Exception:
        return None
    headers = list(getattr(msg, "header", []))
    if not headers:
        return None
    out: list[tuple[dict[str, Any], bytes]] = []
    for h in headers:
        info = _header_dict(h)
        if "cmd_func" not in info and "cmd_id" not in info and not h.pdata:
            # Doesn't look like a DP3 header at all.
            return None
        pdata = bytes(h.pdata)
        if pdata and info.get("enc_type", 0) == 1 and info.get("src", 0) != 32:
            pdata = _xor(pdata, int(info.get("seq", 0)))
        out.append((info, pdata))
    return out


def _decode_dp3(raw: bytes, header_message_cls: Any) -> list[dict[str, Any]] | None:
    headers = dp3_headers(raw, header_message_cls)
    if headers is None:
        return None
    return [
        {"key": f"{info.get('cmd_func', 0)}_{info.get('cmd_id', 0)}", "header": info, "payload": schemaless(pdata)}
        for info, pdata in headers
    ]


def frame_payloads(decoded: dict[str, Any]) -> list[tuple[str, Any]]:
    """(key, payload) pairs of a decoded frame: one per DP3 header, or the JSON body."""
    if decoded.get("format") == "dp3":
        return [(h["key"], h["payload"]) for h in decoded["headers"]]
    if decoded.get("format") == "json":
        body = decoded["json"]
        if isinstance(body, dict):
            # Device data is usually under "params"; latestQuotas replies under data.quotaMap.
            data = body.get("data")
            if isinstance(body.get("params"), dict):
                return [("json", body["params"])]
            if isinstance(data, dict) and isinstance(data.get("quotaMap"), dict):
                return [("json", data["quotaMap"])]
        return [("json", body)]
    return []


def frame_origin(decoded: dict[str, Any]) -> str | None:
    """The header "from" of a DP3 frame (who sent a command), if any."""
    if decoded.get("format") == "dp3":
        for h in decoded["headers"]:
            origin = h["header"].get("from")
            if origin:
                return str(origin)
    if decoded.get("format") == "json" and isinstance(decoded["json"], dict):
        origin = decoded["json"].get("from")
        return str(origin) if origin else None
    return None


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """{"a": {"b": 1}, "c": [2, 3]} -> {"a.b": 1, "c[0]": 2, "c[1]": 3}."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


# --------------------------------------------------------------------------- #
# Capture session
# --------------------------------------------------------------------------- #


class CaptureSession:
    """One recording: a JSONL file plus a compact in-memory timeline.

    Every frame is written to the file whole (decoded + raw hex + what changed
    since the previous frame of the same source/kind/key). The timeline keeps
    only the changes, for the integration's diagnostics download.

    ``record_*`` may be called from the MQTT thread; ``open``/``close`` are
    blocking file operations for an executor.
    """

    def __init__(self, path: str, started: datetime, tz: tzinfo | None, max_bytes: int) -> None:
        self.path = path
        self.started = started
        self.stopped: datetime | None = None
        self.stop_reason: str | None = None
        self.tz = tz
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.frames: dict[str, int] = {}
        self.app_commands = 0
        self.markers = 0
        self.timeline: deque[dict[str, Any]] = deque(maxlen=TIMELINE_MAX)
        self.timeline_dropped = 0
        # topic -> (source label, serial, kind) of other devices' topics to record
        self.watch: dict[str, tuple[str, str, str]] = {}
        self._last: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._fh: Any = None
        self._lock = threading.Lock()
        self._full = False

    # -- file handling (blocking) ------------------------------------------- #

    def open(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    @property
    def is_full(self) -> bool:
        return self._full

    # -- recording ---------------------------------------------------------- #

    def _ts(self, when: datetime) -> str:
        return when.astimezone(self.tz).isoformat(timespec="milliseconds")

    def _write(self, record: dict[str, Any]) -> bool:
        """Append one line; False once the size limit is reached."""
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            if self._fh is None or self._full:
                return False
            self._fh.write(line)
            self.bytes_written += len(line.encode("utf-8"))
            if self.bytes_written >= self.max_bytes:
                self._full = True
                return False
        return True

    def _timeline_add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if len(self.timeline) == self.timeline.maxlen:
                self.timeline_dropped += 1
            self.timeline.append(entry)

    def timeline_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.timeline)

    def record_event(self, when: datetime, event: str, **data: Any) -> bool:
        record = {"t": self._ts(when), "event": event, **data}
        self._timeline_add(
            {"t": record["t"][11:23], "event": event, **{k: v for k, v in data.items() if k != "baseline"}}
        )
        return self._write(record)

    def record_marker(self, when: datetime) -> bool:
        self.markers += 1
        return self.record_event(when, "marker", n=self.markers)

    def record_frame(
        self,
        when: datetime,
        source: str,
        sn: str,
        kind: str,
        raw: bytes,
        decoded: dict[str, Any],
        timeline_filter: Callable[[str, Any], bool] | None = None,
    ) -> bool:
        """Record one frame. ``timeline_filter(path, value)`` picks which changes
        go to the in-memory timeline (all of them when None); the file always
        gets every change."""
        self.frames[source] = self.frames.get(source, 0) + 1
        origin = frame_origin(decoded)
        is_app = kind == "set" and origin in APP_ORIGINS
        if is_app:
            self.app_commands += 1

        changes: dict[str, dict[str, list[Any]]] = {}
        for key, payload in frame_payloads(decoded):
            flat = flatten(payload) if isinstance(payload, (dict, list)) else {"": payload}
            last = self._last.setdefault((source, kind, key), {})
            diff = {p: [last.get(p), v] for p, v in flat.items() if p not in last or last[p] != v}
            last.update(flat)
            if diff:
                changes[key] = diff

        ts = self._ts(when)
        record = {
            "t": ts,
            "src": source,
            "sn": sn,
            "kind": kind,
            "from": origin,
            "app_cmd": is_app or None,
            "changes": changes,
            "decoded": decoded,
            "hex": raw.hex(),
        }

        # Commands (set/get) go to the timeline whole, except our own snapshot
        # requests; state frames go as their changes.
        if kind == "get" and origin == "HomeAssistant":
            pass
        elif kind in ("set", "get", "set_reply"):
            self._timeline_add(
                {
                    "t": ts[11:23],
                    "src": source,
                    "kind": kind,
                    "from": origin,
                    "frame": decoded if decoded.get("format") != "raw" else {"hex": raw.hex()},
                }
            )
        else:
            kept: dict[str, dict[str, list[Any]]] = {}
            for key, diff in changes.items():
                sel = {p: ov for p, ov in diff.items() if timeline_filter is None or timeline_filter(p, ov[1])}
                if sel:
                    kept[key] = sel
            if kept:
                self._timeline_add({"t": ts[11:23], "src": source, "kind": kind, "chg": kept})
            elif decoded.get("format") == "raw":
                self._timeline_add({"t": ts[11:23], "src": source, "kind": kind, "hex": raw.hex()[:400]})

        return self._write(record)

    def summary(self) -> dict[str, Any]:
        return {
            "file": self.path,
            "started": self._ts(self.started),
            "stopped": self._ts(self.stopped) if self.stopped else None,
            "stop_reason": self.stop_reason,
            "frames": dict(self.frames),
            "app_commands": self.app_commands,
            "markers": self.markers,
            "bytes_written": self.bytes_written,
            "timeline_entries": len(self.timeline),
            "timeline_dropped": self.timeline_dropped,
        }
