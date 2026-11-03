#!/usr/bin/env python3
"""Path A: convert a stock SolaX Pocket WiFi dongle to ESPHome over SolaX's MQTT OTA.

Prerequisites (see docs/conversion.md):
  * build/factory.bin   ESPHome factory image          (make esphome)
  * build/flasher.ota.bin + .json  flasher built for it (make flasher)
  * the dongle connected to an MQTT broker you control  (docs/mqtt-ota.md)

What it does:
  1. checks the flasher was built for this exact factory.bin;
  2. asks you to type the dongle serial to confirm the push;
  3. starts tools/ota_server.py on the port in REPORT_URL;
  4. pushes the flasher with tools/push_ota.py --arm (skipped with --resume);
  5. waits for the flasher: hello -> 4 MB backup (16/16) -> "ready"
     (= factory.bin downloaded and MD5-verified on the dongle);
  6. asks you to type GO; only then creates the go-file;
  7. waits for "done" (flash written, read back and MD5-verified; rebooting).

From step 4 on the dongle runs the flasher on every boot until it finishes: the
stock bootloader has no rollback. If you stop before GO, nothing has been
written yet; re-run with --resume and power-cycle the dongle (the flasher then
starts over: backup, verify, ready, wait for GO; without the power cycle it never
posts "ready" again) or restore stock by serial.
"""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import queue
import subprocess
import sys
import threading
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
TOOLS = ROOT / "tools"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ask(prompt, expected):
    try:
        return input(prompt).strip() == expected
    except EOFError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sn", required=True, help="dongle serial / registration no. (10 chars)")
    ap.add_argument("--broker", help="MQTT broker the dongle is connected to (not needed with --resume)")
    ap.add_argument("--broker-port", type=int, default=1883, help="broker port for push_ota.py (default 1883)")
    ap.add_argument("--tls", action="store_true", help="TLS to the broker (cert not verified)")
    ap.add_argument("--username", help="broker username")
    ap.add_argument("--password", help="broker password")
    ap.add_argument("--factory", default=str(ROOT / "build/factory.bin"), help="ESPHome factory.bin")
    ap.add_argument("--flasher", default=str(ROOT / "build/flasher.ota.bin"), help="wrapped flasher image")
    ap.add_argument("--workdir", default=str(ROOT / "build/ota-server"), help="dumps, backups, logs, go-file")
    ap.add_argument("--version", default="V9.000.10", help="OTA version string (must start with V)")
    ap.add_argument("--bind", default="0.0.0.0", help="HTTP bind address (default 0.0.0.0)")
    ap.add_argument("--resume", action="store_true", help="do not push; the flasher is already on the dongle")
    a = ap.parse_args()

    # ---- 1. consistency -------------------------------------------------
    meta_path = pathlib.Path(a.flasher + ".json")
    if not meta_path.exists():
        sys.exit(f"{meta_path} missing: build the flasher with flasher/build.sh (make flasher)")
    meta = json.loads(meta_path.read_text())
    factory = pathlib.Path(a.factory).read_bytes()
    md5 = hashlib.md5(factory).hexdigest()
    if (len(factory), md5) != (meta["factory_len"], meta["factory_md5"]):
        sys.exit(f"{a.factory} ({len(factory)} B, {md5}) is not the image the flasher was built for "
                 f"({meta['factory_len']} B, {meta['factory_md5']}); rebuild the flasher")
    check = load_tool("check_factory")
    errs, _ = check.check(factory)
    if errs:
        sys.exit("factory.bin check failed:\n  " + "\n  ".join(errs))
    url = urllib.parse.urlparse(meta["report_url"])
    port = url.port
    print(f"factory.bin   {len(factory)} B md5 {md5}")
    print(f"flasher       {a.flasher} ({meta['ota_len']} B), reports to {meta['report_url']}")
    print(f"dongle        {a.sn}")
    if not a.resume and not a.broker:
        sys.exit("--broker is required unless --resume")

    # ---- 2. confirm the push --------------------------------------------
    if not a.resume:
        print("\nAbout to push the flasher over SolaX's MQTT OTA. Once it boots, the dongle runs it on\n"
              "every boot until it finishes (no rollback). Have a serial rig ready (docs/serial.md).")
        if not ask(f"Type the dongle serial ({a.sn}) to push: ", a.sn):
            sys.exit("not confirmed; nothing done")

    # ---- 3. OTA server --------------------------------------------------
    events = queue.Queue()
    ota = load_tool("ota_server")
    srv = ota.OtaServer(a.workdir, a.factory, on_event=lambda k, t: events.put((k, t)))
    if srv.go_file.exists():
        sys.exit(f"{srv.go_file} exists from an earlier run; delete it first")
    httpd = srv.serve(a.bind, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        # ---- 4. push ----------------------------------------------------
        if not a.resume:
            cmd = [sys.executable, "-I", str(TOOLS / "push_ota.py"), "--image", a.flasher, "--sn", a.sn,
                   "--version", a.version, "--broker", a.broker, "--port", str(a.broker_port), "--arm"]
            if a.tls:
                cmd.append("--tls")
            if a.username:
                cmd += ["--username", a.username, "--password", a.password or ""]
            rc = subprocess.call(cmd)
            if rc != 0:
                print("push_ota.py did not report success. If it FAILED the dongle is still on stock.\n"
                      "If it timed out, the flasher may still come up; waiting for it anyway (Ctrl-C to stop).")

        # ---- 5. wait for ready -------------------------------------------
        if a.resume:
            print("\n--resume: power-cycle the dongle now; the flasher only posts 'ready' after a fresh boot.")
        print("\nwaiting for the flasher: hello -> backup 16/16 -> ready ...")
        ready, backup, run_backups = None, None, len(srv.backups)
        while ready is None or backup is None:
            kind, text = events.get()
            if kind == "HALT":
                sys.exit(f"flasher HALTED: {text}\nSee docs/conversion.md (troubleshooting).")
            if kind == "hello":
                ready, backup, run_backups = None, None, len(srv.backups)
            if kind == "backup" and len(srv.backups) > run_backups:
                backup = srv.backups[-1]
            if kind == "ready":
                ready = text
        print(f"\nREADY: {ready}")
        print(f"backup: {backup[0]} md5 {backup[1]}")
        print("Keep that backup safe: it is the only way back to stock (serial write-flash 0x0).\n"
              "Restore with its *-restorable.bin twin (otadata boots stock, not the flasher; see\n"
              "the 'restorable' line above, or tools/make_restorable.py if it was not written).")

        # ---- 6. GO gate ----------------------------------------------------
        print("\nTyping GO lets the flasher overwrite app, partition table, otadata and finally the\n"
              "bootloader. Do not power-cycle the dongle until it reports done.")
        if not ask("Type GO to write ESPHome: ", "GO"):
            print("not confirmed. The flasher keeps polling /go; nothing has been written. "
                  "To continue: re-run with --resume, then power-cycle the dongle.")
            return
        srv.go_file.write_text("GO\n")

        # ---- 7. wait for done ---------------------------------------------
        while True:
            kind, text = events.get()
            if kind == "HALT":
                sys.exit(f"flasher HALTED after GO: {text}\nThe dongle may need serial recovery "
                         "(docs/serial.md); the backup above restores stock.")
            if kind == "done":
                print(f"\nDONE: {text}\nThe dongle reboots into ESPHome. Find it in the ESPHome dashboard / "
                      "Home Assistant and verify with tools/mbverify.py --host <ip>.")
                time.sleep(2)
                return
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if srv.go_file.exists():
            srv.go_file.unlink()
        httpd.shutdown()


if __name__ == "__main__":
    main()
