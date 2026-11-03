#!/usr/bin/env python3
"""Push a firmware image to a SolaX Pocket WiFi (ESP32-S2, 3.x firmware) over SolaX's MQTT OTA.

INERT BY DEFAULT: without --arm nothing is connected and nothing is published; the
script only prints the plan (topics, JSON, chunk count) for review.

The dongle must be connected to a broker you control (see docs/mqtt-ota.md). Flow:
  1. publish every chunk RETAINED on update/package/<taskId32>/<n> (n = 1..N),
     payload = 1024-byte slice + 2-byte big-endian CRC-16 of the slice;
  2. publish the command JSON on update/version/<SN>;
  3. the dongle subscribes to each chunk topic in turn (pull-by-subscribe), writes
     it with esp_ota_write and acks on update/ack/<taskId>/<SN>/ every 10 chunks
     (successFlag 2 + progress = chunks so far), then 1 = committed, 0 = failed;
  4. on a final ack the retained chunks are cleared.

Image requirements (checked here): ESP-IDF app image (first byte 0xE9), whole-image
CRC-16 == 0 (genuine SolaX .usb images already are; wrap anything else with
make_ota_image.py), fits a stock 1 MB OTA slot. The version string MUST start
with "V" and look like SolaX's ("V3.010.16", "V9.000.02"): without the leading V
the stock firmware skips its commit step and silently reverts at the very end.

WARNING: the stock bootloader has NO rollback. If the image you push boots, the
dongle stays on it. Only push images that carry their own update path.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_ota_image import crc16, load_table, self_test  # noqa: E402

CHUNK = 1024              # pagesize; stock esp_ota_write ping-pong buffer is 0x400
STOCK_SLOT = 0x100000     # stock ota_0 / ota_1 are 1 MB each


def bar(label, done, total, w=30):
    f = int(w * done / total) if total else w
    print(f"\r{label} [{'#' * f}{'.' * (w - f)}] {100 * done // total:3d}% ({done}/{total})", end="", flush=True)
    if done >= total:
        print()


def make_client(mqtt):
    if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    return mqtt.Client()


def connect(mqtt, a, on_message=None):
    c = make_client(mqtt)
    if a.username:
        c.username_pw_set(a.username, a.password)
    if a.tls:
        import ssl
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)
    if on_message:
        c.on_message = on_message
    c.connect(a.broker, a.port, 60)
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True, help="image to push (SolaX .usb, or output of make_ota_image.py)")
    ap.add_argument("--sn", required=True, help="dongle serial / registration no. (10 chars, e.g. SXXXXXXXXX)")
    ap.add_argument("--version", required=True, help='version string, must start with V, e.g. "V9.000.10"')
    ap.add_argument("--broker", required=True, help="host of the MQTT broker the dongle is connected to")
    ap.add_argument("--port", type=int, default=1883, help="broker port for this script (default 1883)")
    ap.add_argument("--tls", action="store_true", help="use TLS to the broker (certificate NOT verified)")
    ap.add_argument("--username", help="broker username, if your broker needs one")
    ap.add_argument("--password", help="broker password, if your broker needs one")
    ap.add_argument("--taskid", help="32-char task id (default: 32 random hex chars)")
    ap.add_argument("--wait", type=float, default=1200, help="seconds to wait for the final ack (default 1200)")
    ap.add_argument("--keep-retained", action="store_true", help="do not clear the retained chunks afterwards")
    ap.add_argument("--arm", action="store_true", help="actually connect and publish (the real push)")
    ap.add_argument("--clear", action="store_true",
                    help="with --arm and --taskid: only clear the retained chunks of an earlier run")
    a = ap.parse_args()

    tab = load_table()
    self_test(tab)

    if not a.version.startswith("V") or a.version.count(".") < 2:
        sys.exit(f'refusing: version "{a.version}" must start with "V" and have two dots, e.g. V9.000.10 '
                 "(without the V the dongle downloads everything and then silently reverts)")
    if not re.fullmatch(r"V\d+\.\d{3}\.\d{2}", a.version):
        print(f'warning: "{a.version}" does not have the SolaX shape V<n>.<3 digits>.<2 digits>; '
              "proven values: V3.010.16, V9.000.02")
    if len(a.sn) != 10:
        print(f"warning: dongle serial {a.sn!r} is not 10 characters")

    img = pathlib.Path(a.image).read_bytes()
    if img[:1] != b"\xe9":
        sys.exit("refusing: not an ESP-IDF app image (first byte != 0xE9)")
    if crc16(img, tab) != 0:
        sys.exit("refusing: whole-image CRC-16 != 0; the dongle would reject it at the end. "
                 "Wrap it first: tools/make_ota_image.py <app.bin> <out.ota.bin>")
    total = len(img)
    if total > STOCK_SLOT:
        sys.exit(f"refusing: {total} B does not fit a stock 1 MB OTA slot")
    nchunks = (total + CHUNK - 1) // CHUNK
    taskid = a.taskid or os.urandom(16).hex()  # 32 hex chars -> no %32.32s padding in topics
    if len(taskid) != 32:
        sys.exit("taskId must be exactly 32 chars (the firmware formats topics with %32.32s)")

    cmd_topic = f"update/version/{a.sn}"
    cmd = {"version": a.version, "taskId": taskid, "devicetype": 2, "scope": 8,
           "pagesize": CHUNK, "packagesize": total}

    def pkg_topic(n):
        return f"update/package/{taskid}/{n}"

    def chunk_payload(n):
        s = img[(n - 1) * CHUNK:n * CHUNK]
        c = crc16(s, tab)
        return s + bytes([c >> 8, c & 0xFF])  # big-endian per-chunk CRC

    print(f"image   {a.image}  {total} B  sha256 {hashlib.sha256(img).hexdigest()[:16]}...  "
          f"{nchunks} chunks (last {total - (nchunks - 1) * CHUNK} B)")
    print(f"taskId  {taskid}")
    print(f"PUBLISH (retained) {pkg_topic(1)} .. {pkg_topic(nchunks)}  [{CHUNK} B slice + 2 B CRC-16]")
    print(f"PUBLISH {cmd_topic}  {json.dumps(cmd)}")
    print(f"WATCH   update/ack/+/{a.sn}/")
    print(f"CLEANUP zero-length retained to each {pkg_topic('<n>')}")
    if not a.arm:
        print("\n[inert] --arm not given: nothing connected, nothing published.")
        return

    import paho.mqtt.client as mqtt

    if a.clear:
        if not a.taskid:
            sys.exit("--clear needs the --taskid of the run to clear")
        c = connect(mqtt, a)
        c.loop_start()
        for n in range(1, nchunks + 2):
            c.publish(pkg_topic(n), b"", qos=1, retain=True).wait_for_publish()
        c.loop_stop()
        c.disconnect()
        print(f"cleared retained {pkg_topic(1)} .. {pkg_topic(nchunks + 1)}")
        return

    state = {"ok": None}

    def on_msg(client, userdata, m):
        try:
            p = json.loads(m.payload.decode())
        except Exception:
            return
        flag, prog = str(p.get("successFlag")), p.get("progress")
        try:
            bar("dongle flashing  ", min(int(prog), nchunks), nchunks)
        except (TypeError, ValueError):
            pass
        if flag in ("0", "1"):
            print(f"\nfinal ack: {m.payload.decode(errors='replace')}")
            state["ok"] = flag == "1"

    c = connect(mqtt, a, on_msg)
    c.subscribe(f"update/ack/+/{a.sn}/")
    c.loop_start()
    for n in range(1, nchunks + 1):
        c.publish(pkg_topic(n), chunk_payload(n), qos=1, retain=True).wait_for_publish()
        if n % 20 == 0 or n == nchunks:
            bar("upload to broker ", n, nchunks)
    c.publish(cmd_topic, json.dumps(cmd), qos=1).wait_for_publish()
    print("command published; waiting for the dongle to pull and flash "
          f"(it may also ask for chunk {nchunks + 1}: harmless, not served)")
    t0 = time.time()
    while state["ok"] is None and time.time() - t0 < a.wait:
        time.sleep(2)
    print("RESULT:", {True: "success (dongle commits and reboots into the new image)",
                      False: "FAILED (dongle stays on its current firmware)",
                      None: "timeout (no final ack)"}[state["ok"]])
    if state["ok"] is not None and not a.keep_retained:
        for n in range(1, nchunks + 1):
            c.publish(pkg_topic(n), b"", qos=1, retain=True).wait_for_publish()
        print("retained chunks cleared")
    elif state["ok"] is None:
        print(f"timeout: retained chunks left in place for inspection; clear later with "
              f"--arm --clear --taskid {taskid}")
    c.loop_stop()
    c.disconnect()
    sys.exit(0 if state["ok"] else 1)


if __name__ == "__main__":
    main()
