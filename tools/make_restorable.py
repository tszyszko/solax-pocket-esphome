#!/usr/bin/env python3
"""Turn a path-A 4 MB backup into an image that boots stock when restored.

The backup the flasher uploads is taken while the flasher runs: one stock OTA
slot holds the flasher and stock otadata (0xD000, two 0x1000 sectors) selects
it, so `esptool write-flash 0x0 stock-backup.bin` boots the flasher again. This
rewrites otadata so the bootloader picks the *other* slot, i.e. stock:

  flasher in ota_1 (0x110000) -> stock in ota_0: sector 0 = ota_seq 1, sector 1 = FF
  flasher in ota_0 (0x010000) -> stock in ota_1: sector 0 = ota_seq 2, sector 1 = FF

Entry format (ESP-IDF 4.x, app rollback off): uint32 ota_seq, uint8 seq_label[20],
uint32 ota_state, uint32 crc, crc = esp_rom_crc32_le(UINT32_MAX, &ota_seq, 4).
The bootloader boots slot (seq - 1) % 2 of the valid entry with the highest seq.
Everything else in the image is left byte-for-byte as dumped.

The flasher's slot is given with --flasher-slot or detected from the image: the
slot whose app image starts with 0xE9 and whose app descriptor project name is
"solax_flasher" (override with --project-name).

Restore: esptool ... write-flash 0x0 stock-backup-restorable.bin

Examples:
    python3 -I tools/make_restorable.py build/ota-server/stock-backup.bin
    python3 -I tools/make_restorable.py backup.bin --flasher-slot ota_1 -o backup-restorable.bin
    python3 -I tools/make_restorable.py --selftest [--stock-image <4 MB serial backup>]
"""
import argparse
import hashlib
import pathlib
import struct
import sys
import zlib

FLASH = 0x400000
OTADATA = 0xD000            # stock partition table: otadata 0xD000, 2 x 0x1000
OTADATA_SEC = 0x1000
SLOTS = {"ota_0": 0x10000, "ota_1": 0x110000}
SLOT_SIZE = 0x100000
FLASHER_NAME = "solax_flasher"


def rom_crc32_le(init, data):
    """ESP ROM crc32_le(init, buf, len). It inverts on entry and exit exactly like
    zlib, so esp_rom_crc32_le(init, b) == zlib.crc32(b, init)."""
    return zlib.crc32(data, init) & 0xFFFFFFFF


def otadata_entry(seq):
    """32-byte esp_ota_select_entry_t: seq, label FF, state FF (undefined), crc."""
    s = struct.pack("<I", seq)
    return s + b"\xff" * 20 + b"\xff" * 4 + struct.pack("<I", rom_crc32_le(0xFFFFFFFF, s))


def slot_of_addr(addr):
    for name, base in SLOTS.items():
        if base <= addr < base + SLOT_SIZE:
            return name
    return None


def project_name(img, base):
    """IDF app descriptor project_name (image hdr 24 B + segment hdr 8 B -> desc at +0x20)."""
    if img[base] != 0xE9 or img[base + 0x20:base + 0x24] != bytes.fromhex("3254cdab"):
        return None
    return img[base + 0x50:base + 0x70].split(b"\0")[0].decode(errors="replace")


def detect_flasher_slot(img, name=FLASHER_NAME):
    hits = [s for s, base in SLOTS.items() if project_name(img, base) == name]
    return hits[0] if len(hits) == 1 else None


def make_restorable(img, flasher_slot):
    if len(img) != FLASH:
        raise ValueError(f"backup is {len(img)} B, expected {FLASH}")
    if flasher_slot not in SLOTS:
        raise ValueError(f"flasher slot must be ota_0 or ota_1, not {flasher_slot!r}")
    stock_slot = "ota_1" if flasher_slot == "ota_0" else "ota_0"
    if img[SLOTS[stock_slot]] != 0xE9:
        raise ValueError(f"no app image (0xE9) in the stock slot {stock_slot}")
    seq = 2 if stock_slot == "ota_1" else 1
    sec0 = otadata_entry(seq) + b"\xff" * (OTADATA_SEC - 32)
    out = bytearray(img)
    out[OTADATA:OTADATA + 2 * OTADATA_SEC] = sec0 + b"\xff" * OTADATA_SEC
    return bytes(out), stock_slot


def restorable_path(path):
    p = pathlib.Path(path)
    return p.with_name(p.stem + "-restorable" + p.suffix)


def selftest(stock_image=None):
    # standard CRC-32 check value, both the ROM (init 0) and zlib forms
    assert rom_crc32_le(0, b"123456789") == 0xCBF43926
    # entries from a real dongle's otadata (seq 5 and 6, serial backup)
    assert otadata_entry(5).hex() == "05000000" + "ff" * 24 + "cd0f21c8", otadata_entry(5).hex()
    assert otadata_entry(6).hex() == "06000000" + "ff" * 24 + "23a094da", otadata_entry(6).hex()
    print("selftest: otadata CRC reproduces seq 5 -> cd0f21c8, seq 6 -> 23a094da: OK")
    if stock_image:
        img = pathlib.Path(stock_image).read_bytes()
        assert img[OTADATA:OTADATA + 32] == otadata_entry(5), "sector 0 differs from otadata_entry(5)"
        assert img[OTADATA + OTADATA_SEC:OTADATA + OTADATA_SEC + 32] == otadata_entry(6), "sector 1 differs"
        names = {s: project_name(img, b) for s, b in SLOTS.items()}
        print(f"selftest: {stock_image}: otadata entries match; slots {names}")
        for slot in SLOTS:
            out, stock = make_restorable(img, slot)
            seq = struct.unpack_from("<I", out, OTADATA)[0]
            assert out[OTADATA:OTADATA + 32] == otadata_entry(seq)
            assert set(out[OTADATA + 32:OTADATA + 2 * OTADATA_SEC]) == {0xFF}
            assert (seq - 1) % 2 == (0 if stock == "ota_0" else 1)
            assert out[:OTADATA] == img[:OTADATA] and out[OTADATA + 0x2000:] == img[OTADATA + 0x2000:]
            print(f"selftest: flasher in {slot} -> seq {seq} boots {stock}: OK")
        # auto-detect on a real image whose ota_1 holds a pushed custom app
        for s, n in names.items():
            if n and n != FLASHER_NAME:
                assert detect_flasher_slot(img, n) == s
        assert detect_flasher_slot(img) is None or names.get(detect_flasher_slot(img)) == FLASHER_NAME
        print("selftest: auto-detect by app descriptor project name: OK")
    print("selftest passed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", nargs="?", help="4 MB path-A backup (e.g. build/ota-server/stock-backup.bin)")
    ap.add_argument("-o", "--out", help="output (default <backup>-restorable.bin next to it)")
    ap.add_argument("--flasher-slot", choices=sorted(SLOTS), help="slot the flasher ran from (default: detect)")
    ap.add_argument("--project-name", default=FLASHER_NAME, help="app descriptor name used for detection")
    ap.add_argument("--selftest", action="store_true", help="check the otadata CRC against known entries")
    ap.add_argument("--stock-image", help="with --selftest: a 4 MB image with otadata seq 5/6 to check against (read-only)")
    a = ap.parse_args()
    if a.selftest:
        selftest(a.stock_image)
        return
    if not a.backup:
        ap.error("backup is required (or --selftest)")
    img = pathlib.Path(a.backup).read_bytes()
    slot, how = a.flasher_slot, "given"
    if slot is None:
        slot, how = detect_flasher_slot(img, a.project_name), f"detected ({a.project_name!r} app descriptor)"
        if slot is None:
            sys.exit(f"cannot tell which slot holds {a.project_name!r}; pass --flasher-slot ota_0|ota_1")
    out, stock = make_restorable(img, slot)
    dst = pathlib.Path(a.out) if a.out else restorable_path(a.backup)
    dst.write_bytes(out)
    print(f"flasher slot {slot} ({how}) -> otadata selects stock {stock}")
    print(f"wrote {dst} md5 {hashlib.md5(out).hexdigest()}")
    print(f"restore: esptool ... write-flash 0x0 {dst}")


if __name__ == "__main__":
    main()
