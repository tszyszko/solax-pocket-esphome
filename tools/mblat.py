#!/usr/bin/env python3
"""Modbus TCP latency profile of the dongle, one client, per request type.

Sends --count requests of each kind and prints n / exceptions / median / p90 / max
latency, plus the time offsets of any request slower than --slow-ms.

Example:
    python3 -I tools/mblat.py --host <dongle-ip> --count 40
"""
import argparse
import socket
import struct
import time

TESTS = [("sn_7regs", 3, 0, 7), ("hold_0_50", 3, 0, 50), ("input_0_40", 4, 0, 40),
         ("input_46_8", 4, 0x46, 8), ("hold_7d_4", 3, 0x7d, 4)]


def one(s, tid, fc, addr, n, unit):
    s.sendall(struct.pack(">HHHBBHH", tid, 0, 6, unit, fc, addr, n))
    s.settimeout(6)
    hdr = b""
    while len(hdr) < 6:
        c = s.recv(6 - len(hdr))
        if not c:
            raise ConnectionError("closed")
        hdr += c
    ln = struct.unpack(">H", hdr[4:6])[0]
    body = b""
    while len(body) < ln:
        c = s.recv(ln - len(body))
        if not c:
            raise ConnectionError("closed")
        body += c
    return body[1] & 0x80


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="dongle IP or hostname")
    ap.add_argument("--port", type=int, default=502, help="Modbus TCP port (default 502)")
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit id (default 1)")
    ap.add_argument("--count", type=int, default=40, help="requests per test (default 40)")
    ap.add_argument("--slow-ms", type=float, default=800, help="report requests slower than this (default 800)")
    a = ap.parse_args()

    s = socket.create_connection((a.host, a.port), timeout=8)
    tid, t_start = 0, time.time()
    for name, fc, addr, n in TESTS:
        lat, slow, exc = [], [], 0
        for _ in range(a.count):
            tid = (tid + 1) & 0xFFFF
            t0 = time.time()
            try:
                e = one(s, tid, fc, addr, n, a.unit)
            except Exception as ex:
                print(name, "ERR", ex)
                s.close()
                s = socket.create_connection((a.host, a.port), timeout=8)
                continue
            dt = (time.time() - t0) * 1000
            lat.append(dt)
            exc += bool(e)
            if dt > a.slow_ms:
                slow.append((round(time.time() - t_start, 1), round(dt)))
        if not lat:
            print(f"{name:12s} no successful requests")
            continue
        lat.sort()
        print(f"{name:12s} n={len(lat)} exc={exc} med={lat[len(lat) // 2]:.0f}ms "
              f"p90={lat[int(len(lat) * 0.9)]:.0f}ms max={lat[-1]:.0f}ms slow(t,ms)={slow}")
    print("elapsed", round(time.time() - t_start, 1), "s")


if __name__ == "__main__":
    main()
