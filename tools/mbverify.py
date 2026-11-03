#!/usr/bin/env python3
"""Verify the dongle's Modbus TCP server the way the Home Assistant integration uses it.

Client A reads the inverter serial (holding 0x0 x7), the firmware block (holding
0x7d x4), energies (input 0x46 x8) and a wide holding block (0x0 x50); client B
concurrently reads input 0x0 x40 and holding 0x80 x20. Reports latency and
exception/error counts per client, which exercises the component's request queue.

Example:
    python3 -I tools/mbverify.py --host <dongle-ip> --rounds 10
"""
import argparse
import socket
import struct
import threading
import time


def mb(sock, tid, fc, addr, n, unit, timeout=4.0):
    sock.sendall(struct.pack(">HHHBBHH", tid, 0, 6, unit, fc, addr, n))
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 6:
        c = sock.recv(6 - len(hdr))
        if not c:
            raise ConnectionError("closed")
        hdr += c
    rtid, _proto, ln = struct.unpack(">HHH", hdr)
    body = b""
    while len(body) < ln:
        c = sock.recv(ln - len(body))
        if not c:
            raise ConnectionError("closed")
        body += c
    if rtid != tid:
        raise ValueError(f"tid {rtid} != {tid}")
    if body[1] & 0x80:
        return None, body[2]
    return body[3:3 + body[2]], None


def regs(data):
    return struct.unpack(">%dH" % (len(data) // 2), data)


def run(a, label, items, stats):
    s = socket.create_connection((a.host, a.port), timeout=5)
    tid = 0
    for r in range(a.rounds):
        for (fc, addr, n, name) in items:
            tid = (tid + 1) & 0xFFFF
            t0 = time.time()
            try:
                data, exc = mb(s, tid, fc, addr, n, a.unit)
            except Exception as e:
                stats["err"] += 1
                print(f"{label} {name}: {type(e).__name__} {e}")
                s.close()
                s = socket.create_connection((a.host, a.port), timeout=5)
                continue
            dt = (time.time() - t0) * 1000
            stats["lat"].append(dt)
            if exc is not None:
                stats["exc"] += 1
                print(f"{label} {name}: exception 0x{exc:02x} ({dt:.0f} ms)")
            else:
                stats["ok"] += 1
                if r == 0:
                    if name == "serial":
                        print(f"{label} {name}: {data.decode(errors='replace')!r} ({dt:.0f} ms)")
                    else:
                        print(f"{label} {name}: {regs(data)[:8]}... ({dt:.0f} ms)")
    s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="dongle IP or hostname")
    ap.add_argument("--port", type=int, default=502, help="Modbus TCP port (default 502)")
    ap.add_argument("--unit", type=int, default=1, help="Modbus unit id (default 1; the X1-Fit G4 answers 1 and 2)")
    ap.add_argument("--rounds", type=int, default=10, help="rounds per client (default 10)")
    a = ap.parse_args()

    items_a = [(3, 0x0000, 7, "serial"), (3, 0x007d, 4, "fw_block"), (4, 0x0046, 8, "energies"),
               (3, 0x0000, 50, "holding_0_50")]
    items_b = [(4, 0x0000, 40, "input_0_40"), (3, 0x0080, 20, "holding_80_20")]
    sa = {"ok": 0, "exc": 0, "err": 0, "lat": []}
    sb = {"ok": 0, "exc": 0, "err": 0, "lat": []}
    t = threading.Thread(target=run, args=(a, "B", items_b, sb))
    t0 = time.time()
    t.start()
    run(a, "A", items_a, sa)
    t.join()
    for lab, st in (("A", sa), ("B", sb)):
        lat = sorted(st["lat"]) or [0]
        print(f"{lab}: ok={st['ok']} exc={st['exc']} err={st['err']} "
              f"latency min/med/max = {lat[0]:.0f}/{lat[len(lat) // 2]:.0f}/{lat[-1]:.0f} ms")
    print(f"total wall {time.time() - t0:.1f}s for {a.rounds} rounds x 6 requests (2 clients)")


if __name__ == "__main__":
    main()
