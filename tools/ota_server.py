#!/usr/bin/env python3
"""HTTP side-channel for the in-app factory flasher (and the boot probe example).

Serves:
  GET  /factory.bin   the ESPHome factory image (HTTP Range supported)
  GET  /go            "GO" once the go-file exists, otherwise "WAIT"
  POST /dump/<hex>    one 256 KB piece of the dongle's flash (16 pieces = 4 MB);
                      when all 16 of a run have arrived they are reassembled into
                      <dir>/stock-backup.bin and its MD5 is printed, plus
                      <dir>/stock-backup-restorable.bin (otadata rewritten to boot
                      the stock slot instead of the flasher; see make_restorable.py)
  POST /<anything>    status messages (hello, step, ready, waiting, GO, done, HALT,
                      or the probe's alive/ptbl/blhdr); printed and logged

The first complete backup is written to <dir>/stock-backup.bin and is never
overwritten; later runs (e.g. after a power cycle) go to <dir>/backup-<run>.bin,
because once the flasher has started writing, a new dump is no longer stock.

The GO gate is a file: nothing is written to the dongle's flash until you create it
(convert.py does that only after an explicit confirmation).

Example:
    python3 -I tools/ota_server.py --dir build/ota-server --factory build/factory.bin --port 8080
"""
import argparse
import hashlib
import http.server
import importlib.util
import pathlib
import re
import sys
import threading
import time

PIECE = 0x40000
FLASH = 0x400000
NPIECES = FLASH // PIECE


def _load_make_restorable():
    # loaded by path: python3 -I does not put this directory on sys.path
    spec = importlib.util.spec_from_file_location(
        "make_restorable", pathlib.Path(__file__).resolve().parent / "make_restorable.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


make_restorable = _load_make_restorable()


def ts():
    return time.strftime("%H:%M:%S")


class OtaServer:
    def __init__(self, workdir, factory=None, go_file=None, on_event=None):
        self.dir = pathlib.Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.go_file = pathlib.Path(go_file) if go_file else self.dir / "go"
        self.factory = pathlib.Path(factory).read_bytes() if factory else None
        self.factory_md5 = hashlib.md5(self.factory).hexdigest() if self.factory else None
        self.on_event = on_event
        self.lock = threading.Lock()
        self.run = None
        self.pieces = {}
        self.backups = []          # (path, md5) of completed backups
        self.phys = None           # flasher's running address, from this run's hello/ready
        self.restorable = None     # (backup path, flasher slot it was made for)
        self.log = open(self.dir / "events.log", "a", buffering=1)

    # ---- events -------------------------------------------------------
    def event(self, kind, text, peer=""):
        line = f"{ts()} {peer} {kind}: {text}"
        print(line, flush=True)
        self.log.write(line + "\n")
        if self.on_event:
            self.on_event(kind, text)

    def new_run(self):
        self.run = time.strftime("%Y%m%d-%H%M%S")
        self.pieces = {}
        self.phys = None
        self.restorable = None
        (self.dir / "runs" / self.run).mkdir(parents=True, exist_ok=True)

    # ---- dumps --------------------------------------------------------
    def add_piece(self, off, data, peer):
        with self.lock:
            if self.run is None:
                self.new_run()
            if off % PIECE or off >= FLASH or len(data) != PIECE:
                self.event("dump", f"REJECTED piece off=0x{off:x} len={len(data)}", peer)
                return False
            (self.dir / "runs" / self.run / f"dump_{off:08x}.bin").write_bytes(data)
            self.pieces[off] = data
            self.event("dump", f"backup {len(self.pieces)}/{NPIECES} (0x{off:06x})", peer)
            if len(self.pieces) == NPIECES:
                img = b"".join(self.pieces[o] for o in range(0, FLASH, PIECE))
                out = self.dir / "stock-backup.bin"
                if out.exists():
                    out = self.dir / f"backup-{self.run}.bin"
                    print(f"{ts()} NOTE: stock-backup.bin already exists, keeping it; this run -> {out.name}")
                out.write_bytes(img)
                md5 = hashlib.md5(img).hexdigest()
                self.backups.append((out, md5))
                self.event("backup", f"{out} {len(img)} B md5 {md5}", peer)
                self.write_restorable(out, img, peer)
            return True

    def note_slot(self, kind, text, peer):
        """Record the flasher's running address from 'hello' (phys=0x..) or 'ready'
        (slot=0x..). 'ready' arrives after the backup, so if it disagrees with the
        slot the restorable image was made for, remake it."""
        m = re.search(r"\b(?:slot|phys)=0x([0-9a-fA-F]+)", text)
        if not m or kind not in ("hello", "ready"):
            return
        with self.lock:
            self.phys = int(m.group(1), 16)
            slot = make_restorable.slot_of_addr(self.phys)
            if kind == "ready" and self.restorable and slot and self.restorable[1] != slot:
                path = self.restorable[0]
                self.event("restorable", f"'ready' says flasher in {slot}, not {self.restorable[1]}: remaking", peer)
                self.write_restorable(path, path.read_bytes(), peer)

    def write_restorable(self, backup, img, peer):
        """Write <backup>-restorable.bin: otadata rewritten to boot the stock slot."""
        slot = make_restorable.slot_of_addr(self.phys) if self.phys is not None else None
        how = f"from the flasher's reported address 0x{self.phys:x}" if slot else None
        if slot is None:
            slot = make_restorable.detect_flasher_slot(img)
            how = "detected from the image (slot whose app descriptor is 'solax_flasher')" if slot else None
        dst = make_restorable.restorable_path(backup)
        if slot is None:
            self.event("restorable", f"NOT written: flasher slot unknown; run tools/make_restorable.py "
                       f"{backup} --flasher-slot ota_0|ota_1", peer)
            return
        try:
            out, stock = make_restorable.make_restorable(img, slot)
        except ValueError as e:
            self.event("restorable", f"NOT written: {e}", peer)
            return
        dst.write_bytes(out)
        self.restorable = (backup, slot)
        self.event("restorable", f"{dst} md5 {hashlib.md5(out).hexdigest()}: flasher in {slot} ({how}), "
                   f"otadata now boots stock {stock}; restore with esptool write-flash 0x0 {dst.name}", peer)

    # ---- HTTP ---------------------------------------------------------
    def handler(self):
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            # HTTP/1.0 (one request per connection), as used by the original receiver

            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", extra=()):
                self.send_response(code)
                for k, v in extra:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                peer = self.client_address[0]
                path = self.path.strip("/")
                m = re.fullmatch(r"dump/([0-9a-fA-F]{1,8})", path)
                if m:
                    ok = srv.add_piece(int(m.group(1), 16), body, peer)
                    return self.reply(200 if ok else 400, b"ok" if ok else b"bad")
                kind = re.sub(r"[^A-Za-z0-9_.-]", "_", path) or "root"
                if kind == "hello":
                    with srv.lock:
                        srv.new_run()
                if n <= 512 and all(32 <= c < 127 or c in (9, 10, 13) for c in body):
                    srv.event(kind, body.decode().strip(), peer)
                    srv.note_slot(kind, body.decode(), peer)
                else:
                    p = srv.dir / "posts" / f"{kind}-{time.strftime('%H%M%S')}.bin"
                    p.parent.mkdir(exist_ok=True)
                    p.write_bytes(body)
                    srv.event(kind, f"{n} B binary -> {p}", peer)
                self.reply(200, b"ok")

            def do_GET(self):
                peer = self.client_address[0]
                if self.path == "/go":
                    body = b"GO" if srv.go_file.exists() else b"WAIT"
                    print(f"{ts()} {peer} GET /go -> {body.decode()}", flush=True)
                    return self.reply(200, body)
                if self.path != "/factory.bin" or srv.factory is None:
                    return self.reply(404)
                data, rng = srv.factory, self.headers.get("Range")
                start, end = 0, len(data) - 1
                if rng:
                    m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng.strip())
                    if not m or int(m.group(1)) >= len(data):
                        return self.reply(416, extra=[("Content-Range", f"bytes */{len(data)}")])
                    start = int(m.group(1))
                    end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                chunk = data[start:end + 1]
                print(f"{ts()} {peer} GET /factory.bin {rng or 'full'} -> {len(chunk)} B", flush=True)
                extra = [("Content-Type", "application/octet-stream")]
                if rng:
                    extra.append(("Content-Range", f"bytes {start}-{end}/{len(data)}"))
                self.reply(206 if rng else 200, chunk, extra)

        return H

    def serve(self, bind, port):
        httpd = http.server.ThreadingHTTPServer((bind, port), self.handler())
        httpd.daemon_threads = True
        print(f"{ts()} listening on {bind}:{port}; dir={self.dir}; go-file={self.go_file} "
              f"({'PRESENT' if self.go_file.exists() else 'absent'})")
        if self.factory is not None:
            print(f"{ts()} serving factory.bin {len(self.factory)} B md5 {self.factory_md5}")
        return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="working directory for dumps, backups, logs and the go-file")
    ap.add_argument("--factory", help="ESPHome factory.bin to serve at /factory.bin")
    ap.add_argument("--port", type=int, default=8080, help="TCP port (default 8080; must match REPORT_URL)")
    ap.add_argument("--bind", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--go-file", help="path of the go-file (default <dir>/go)")
    a = ap.parse_args()
    srv = OtaServer(a.dir, a.factory, a.go_file)
    if srv.go_file.exists():
        print(f"WARNING: {srv.go_file} already exists: a flasher that reaches 'ready' will start writing "
              "immediately. Delete it unless that is what you want.", file=sys.stderr)
    try:
        srv.serve(a.bind, a.port).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
