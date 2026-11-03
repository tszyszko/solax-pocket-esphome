#!/usr/bin/env python3
"""Wrap an ESP-IDF app image for SolaX's MQTT OTA.

After the last chunk the stock dongle firmware reads the written partition back
and runs a CRC-16 over all `packagesize` bytes; the result must be 0. SolaX's own
images are built to satisfy that. For any other ESP-IDF app image we append a
2-byte big-endian CRC-16 trailer (SolaX's table, = CRC-16/UMTS: poly 0x8005,
init 0, MSB first) so the whole-image CRC comes out zero. esp_ota_end() ignores
the two trailing bytes, so the image still validates (magic 0xE9 + SHA-256).

Example:
    python3 -I tools/make_ota_image.py flasher/build/solax_flasher.bin build/flasher.ota.bin
"""
import argparse
import pathlib
import sys

TABLE_FILE = pathlib.Path(__file__).resolve().with_name("crctab.txt")


def load_table(path=TABLE_FILE):
    tab = [int(x) for x in path.read_text().split(",")]
    if len(tab) != 256:
        raise SystemExit(f"{path}: expected 256 entries, got {len(tab)}")
    return tab


def crc16(data, tab, crc=0):
    for b in data:
        crc = (tab[((crc >> 8) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFF)) & 0xFFFF
    return crc


def self_test(tab):
    if crc16(b"123456789", tab) != 0xFEE8:  # CRC-16/UMTS check value
        raise SystemExit("crctab.txt self-test failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="ESP-IDF app image (first byte 0xE9), e.g. flasher/build/solax_flasher.bin")
    ap.add_argument("dst", help="output image to push with push_ota.py")
    a = ap.parse_args()

    tab = load_table()
    self_test(tab)
    data = pathlib.Path(a.src).read_bytes()
    if data[:1] != b"\xe9":
        sys.exit(f"{a.src}: not an ESP-IDF app image (first byte != 0xE9). "
                 "Use the app .bin, not factory.bin / bootloader / partition table.")
    if crc16(data, tab) == 0:
        print(f"note: {a.src} already CRC-16s to zero (a genuine SolaX image?); appending a trailer anyway")
    c = crc16(data, tab)
    out = data + bytes([c >> 8, c & 0xFF])
    if crc16(out, tab) != 0:
        sys.exit("trailer self-check failed")
    pathlib.Path(a.dst).write_bytes(out)
    print(f"{a.src} {len(data)} B -> {a.dst} {len(out)} B (trailer {c:#06x}); whole-image crc16 = 0 OK")


if __name__ == "__main__":
    main()
