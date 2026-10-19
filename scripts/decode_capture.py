#!/usr/bin/env python3
"""Decode an EcoFlow Cloud DEBUG log capture, offline.

With DEBUG on for ``custom_components.ecoflow_cloud.api.ecoflow_mqtt``, every
MQTT message the integration accepts is logged as::

    <time> DEBUG (...) [...ecoflow_mqtt] Message for <SN> and Topic <topic> : <payload hex>

and every request it sends as ``Sending b'...'``. This script reads those lines
and decodes the DP3-style protobuf payloads (DELTA Pro 3 / DPU X / Smart
Gateway / Smart Home Panel 3 / River 3 ...): each header's src/dest/cmd/seq,
XOR-decoded pdata, and the payload's fields by number. It needs nothing but
the Python standard library, so it runs anywhere.

Usage::

    scripts/decode_capture.py capture.log                    # every frame, changed fields only
    scripts/decode_capture.py capture.log --full             # every frame, all fields
    scripts/decode_capture.py capture.log --sn P101 --fields 4,6,120,756
    scripts/decode_capture.py capture.log --gaps 10          # push pauses > 10 s, get_reply repeats
    scripts/decode_capture.py capture.log --from 22:36:50 --to 22:37:30

Output values: varints as integers, 32-bit floats rounded to 3 places,
nested messages as {sub-field: value}, strings when printable, else hex.
Field numbers are raw; see the device profiles under
custom_components/ecoflow_cloud/devices/internal for their meanings.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import re
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) .*?"
    r"(?:Message for (?P<sn>\S+) and Topic (?P<topic>\S+) : (?P<hex>[0-9a-f]*)"
    r"|Sending (?P<sent>b(?:'.*'|\".*\")) :)"
)

WIRE_VARINT, WIRE_F64, WIRE_LEN, WIRE_F32 = 0, 1, 2, 5

# DP3Header field numbers (ef_dp3_iobroker.proto).
H_PDATA, H_SRC, H_DEST, H_ENC, H_CMD_FUNC, H_CMD_ID, H_SEQ, H_FROM, H_DEVICE_SN = 1, 2, 3, 6, 8, 9, 14, 23, 25


# ---------------------------------------------------------------- protobuf --


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def parse(b: bytes) -> tuple[dict[int, list[tuple[int, Any]]], bool]:
    """{field: [(wire_type, value), ...]} and whether all of b parsed cleanly."""
    out: dict[int, list[tuple[int, Any]]] = {}
    i, n = 0, len(b)
    while i < n:
        try:
            tag, i = _varint(b, i)
            fn, wt = tag >> 3, tag & 7
            if fn == 0:
                return out, False
            v: Any
            if wt == WIRE_VARINT:
                v, i = _varint(b, i)
            elif wt == WIRE_LEN:
                ln, i = _varint(b, i)
                if i + ln > n:
                    return out, False
                v, i = b[i : i + ln], i + ln
            elif wt == WIRE_F32:
                if i + 4 > n:
                    return out, False
                v, i = struct.unpack("<f", b[i : i + 4])[0], i + 4
            elif wt == WIRE_F64:
                if i + 8 > n:
                    return out, False
                v, i = struct.unpack("<d", b[i : i + 8])[0], i + 8
            else:
                return out, False
        except IndexError:
            return out, False
        out.setdefault(fn, []).append((wt, v))
    return out, True


def first(fields: dict[int, list[tuple[int, Any]]], no: int, default: Any = None) -> Any:
    vals = fields.get(no)
    return vals[0][1] if vals else default


def render(wt: int, v: Any, depth: int = 0) -> Any:
    """A readable value: nested messages as dicts, text when printable."""
    if wt == WIRE_F32 or wt == WIRE_F64:
        return round(v, 3)
    if wt != WIRE_LEN:
        return v
    if v and depth < 4:
        sub, ok = parse(v)
        if ok and sub:
            text = v.decode("utf-8", "ignore")
            # A short printable string can also parse as a message; prefer the text.
            if not (text.isprintable() and len(text) == len(v) and len(v) >= 4):
                return {k: _render_list(vals, depth + 1) for k, vals in sorted(sub.items())}
    text = v.decode("utf-8", "ignore")
    if text and text.isprintable() and len(text) == len(v):
        return text
    return v.hex()


def _render_list(vals: list[tuple[int, Any]], depth: int) -> Any:
    out = [render(wt, v, depth) for wt, v in vals]
    return out[0] if len(out) == 1 else out


# ------------------------------------------------------------------ frames --


@dataclass
class Header:
    src: int
    dest: int
    cmd_func: int
    cmd_id: int
    seq: int
    sender: str
    pdata: bytes  # XOR-decoded

    @property
    def cmd(self) -> str:
        return f"{self.cmd_func}/{self.cmd_id}"


@dataclass
class Frame:
    time: datetime
    sn: str
    topic: str
    raw: bytes
    headers: list[Header] = field(default_factory=list)

    @property
    def kind(self) -> str:
        t = self.topic
        if t.endswith("/get_reply"):
            return "get_reply"
        if t.endswith("/set_reply"):
            return "set_reply"
        if t.endswith("/get"):
            return "get"
        if t.endswith("/set"):
            return "set"
        if "/status" in t:
            return "status"
        return "push"


def decode_headers(raw: bytes) -> list[Header]:
    msg, ok = parse(raw)
    if not ok:
        return []
    headers = []
    for wt, hb in msg.get(1, []):
        if wt != WIRE_LEN:
            continue
        h, _ = parse(hb)
        src, seq = first(h, H_SRC, 0), first(h, H_SEQ, 0)
        pdata = first(h, H_PDATA, b"") or b""
        if first(h, H_ENC, 0) == 1 and src != 32:
            key = seq & 0xFF
            pdata = bytes(x ^ key for x in pdata)
        sender = first(h, H_FROM, b"")
        headers.append(
            Header(
                src=src,
                dest=first(h, H_DEST, 0),
                cmd_func=first(h, H_CMD_FUNC, 0),
                cmd_id=first(h, H_CMD_ID, 0),
                seq=seq,
                sender=sender.decode("utf-8", "ignore") if isinstance(sender, bytes) else "",
                pdata=pdata,
            )
        )
    return headers


def read_capture(path: str) -> tuple[list[Frame], list[datetime]]:
    frames: list[Frame] = []
    requests: list[datetime] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = LINE_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f")  # noqa: DTZ007 - the log's own local time
            if m["sent"]:
                try:
                    payload = ast.literal_eval(m["sent"])
                except (ValueError, SyntaxError):
                    continue
                hs = decode_headers(payload) if isinstance(payload, bytes) else []
                # A "get all" request: one header, no payload.
                if hs and not hs[0].pdata:
                    requests.append(ts)
                continue
            raw = bytes.fromhex(m["hex"])
            frames.append(Frame(ts, m["sn"], m["topic"], raw, decode_headers(raw)))
    return frames, requests


def _t(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S.%f")[:-3]


def snapshot_hash(frame: Frame) -> str | None:
    """Hash of a get_reply's 254/21 payload: identical hashes = the same snapshot."""
    for h in frame.headers:
        if h.cmd == "254/21" and h.pdata:
            return hashlib.sha1(h.pdata).hexdigest()[:10]
    return None


def is_empty_reply(frame: Frame) -> bool:
    return len(frame.headers) == 1 and not frame.headers[0].pdata and frame.headers[0].cmd == "0/0"


# ------------------------------------------------------------------- modes --


def print_frames(frames: list[Frame], args: argparse.Namespace) -> None:
    wanted = {int(f) for f in args.fields.split(",")} if args.fields else None
    last: dict[tuple[str, str], dict[int, Any]] = {}
    for fr in frames:
        if args.sn and not fr.sn.startswith(args.sn):
            continue
        if not fr.headers:
            print(f"{_t(fr.time)} {fr.sn} {fr.kind} (not a DP3 frame, {len(fr.raw)} bytes)")
            continue
        for h in fr.headers:
            fields, _ = parse(h.pdata) if h.pdata else ({}, True)
            values = {k: _render_list(v, 0) for k, v in fields.items()}
            key = (fr.sn, h.cmd)
            if wanted is not None:
                shown = {k: v for k, v in values.items() if k in wanted}
            elif args.full or fr.kind != "push":
                shown = values
            else:
                prev = last.get(key, {})
                shown = {k: v for k, v in values.items() if prev.get(k) != v}
            last.setdefault(key, {}).update(values)
            if wanted is not None and not shown:
                continue
            extra = ""
            if fr.kind == "get_reply" and h.cmd == "254/21" and h.pdata:
                extra = f" snapshot={hashlib.sha1(h.pdata).hexdigest()[:10]}"
            if fr.kind == "get_reply" and is_empty_reply(fr):
                extra = " EMPTY"
            sender = f" from={h.sender}" if h.sender else ""
            body = " ".join(f"{k}={v}" for k, v in sorted(shown.items()))
            print(
                f"{_t(fr.time)} {fr.sn} {fr.kind:9} src={h.src} dest={h.dest} cmd={h.cmd} "
                f"seq={h.seq}{sender} len={len(h.pdata)}{extra}  {body}"
            )


def print_gaps(frames: list[Frame], requests: list[datetime], threshold: float) -> None:
    """Push pauses per device, plus get_replies that repeat the previous snapshot."""
    by_sn: dict[str, list[Frame]] = {}
    for fr in frames:
        by_sn.setdefault(fr.sn, []).append(fr)
    for sn, frs in sorted(by_sn.items()):
        pushes = [f.time for f in frs if f.kind == "push"]
        if len(pushes) < 2:
            continue
        deltas = sorted((b - a).total_seconds() for a, b in itertools.pairwise(pushes))
        median = deltas[len(deltas) // 2]
        print(f"{sn}: {len(pushes)} pushes, median interval {median:.2f} s")
        for a, b in itertools.pairwise(pushes):
            gap = (b - a).total_seconds()
            if gap > threshold:
                print(f"  push pause {_t(a)} -> {_t(b)}  {gap:.1f} s")
        prev: str | None = None
        for f in frs:
            if f.kind != "get_reply":
                continue
            if is_empty_reply(f):
                print(f"  get_reply {_t(f.time)} EMPTY (one header, cmd 0/0, no pdata)")
                continue
            h = snapshot_hash(f)
            if h is not None and h == prev:
                print(f"  get_reply {_t(f.time)} repeats the previous snapshot {h}")
            prev = h if h is not None else prev
    if requests:
        print(f'{len(requests)} "get all" requests sent')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--sn", help="only frames for serials starting with this")
    ap.add_argument("--fields", help="comma-separated payload field numbers to show")
    ap.add_argument("--full", action="store_true", help="all fields of every push, not just the changed ones")
    ap.add_argument("--gaps", type=float, metavar="SEC", help="report push pauses longer than SEC and snapshot repeats")
    ap.add_argument("--from", dest="since", help="HH:MM:SS start")
    ap.add_argument("--to", dest="until", help="HH:MM:SS end")
    args = ap.parse_args(argv)

    frames, requests = read_capture(args.capture)
    if args.since:
        frames = [f for f in frames if _t(f.time) >= args.since]
    if args.until:
        frames = [f for f in frames if _t(f.time) <= args.until]
    if args.gaps is not None:
        print_gaps(frames, requests, args.gaps)
    else:
        print_frames(frames, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
