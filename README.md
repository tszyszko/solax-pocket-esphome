# SolaX Pocket WiFi → ESPHome

Replace the cloud firmware on a SolaX **Pocket WiFi** dongle (ESP32-S2, stock
ESP-IDF 4.4 firmware 3.x) with ESPHome. The result is a local, cloud-free Modbus
TCP server on port 502 for
[homeassistant-solax-modbus](https://github.com/wills106/homeassistant-solax-modbus),
faster and more reliable than SolaX's own (median ~210 ms per read on an
X1-Fit G4, stock: over 1 s and flaky), plus normal ESPHome entities and OTA.

This repository contains:

* the ESPHome component `solax_aa55` and an example config ([esphome/](esphome/));
* tools for SolaX's own MQTT OTA channel ([tools/](tools/));
* an in-app "factory flasher" that, delivered over that channel, rewrites the
  dongle to ESPHome without opening the case ([flasher/](flasher/));
* the reverse-engineering notes ([docs/](docs/)).

## SAFETY FIRST

Read this before touching a dongle.

* **The stock bootloader has no rollback.** If an app you push over SolaX's OTA
  boots, the dongle stays on it, across power cycles. There is no way back
  except that app's own update path or serial.
* **ESP-IDF 5.x images do not boot under the stock IDF 4.x bootloader.**
  ESPHome is IDF 5.x, so it cannot simply be pushed over SolaX's OTA; the
  bootloader skips it and boots the other slot. That is why the flasher (IDF
  4.4.4) exists.
* **Always take the full 4 MB backup first.** It is the only way back to
  stock (it holds `cfg_data` with the dongle serial). Path A takes it over
  Wi-Fi before writing anything; path B with esptool. Keep two copies.
  **What the path A backup is:** the 4 MB flash as the flasher read it, so one
  OTA slot holds the flasher and otadata selects it; `stock-backup.bin` restored
  as is boots the flasher again. The OTA server therefore also writes
  `stock-backup-restorable.bin` (otadata rewritten to boot the stock slot;
  `tools/make_restorable.py` does the same for an existing backup).
  **Restore:** `esptool write-flash 0x0 stock-backup-restorable.bin` by serial
  ([flasher/README.md](flasher/README.md#backup-and-restore); not yet tested on hardware).
* **Have a serial rig ready** (USB-UART adapter, wires to the test pads,
  [docs/serial.md](docs/serial.md)) before trying path A.
* **Only for your own dongle,** on your own network. Converting it ends SolaX
  Cloud reporting and may affect warranty or support.

## Supported hardware

* SolaX Pocket WiFi sticks built on the **ESP32-S2** with 3.x firmware: the
  "PW2.0"-labelled 3.x boards (tested) and Pocket WiFi 3.0 (same platform,
  untested here). Not the STM32-based Pocket WiFi V2 (2.x firmware), not Pocket
  WiFi 4.0. Details: [docs/hardware.md](docs/hardware.md).
* Inverter tested: X1-Fit G4 (type 15, X1-Hybrid G4 firmware family).

## Two ways to convert

**Path A: over SolaX's MQTT OTA** (no soldering). Point the dongle at your own
MQTT broker, push the flasher, which backs up the whole flash to your machine,
downloads and verifies ESPHome's `factory.bin`, waits for your explicit GO, pulls
the first 64 KB (bootloader, partition table) into RAM, then writes app →
partition table → otadata → bootloader (the last three from RAM, no network)
and reboots into ESPHome.

```sh
make esphome                                            # build/factory.bin
SSID=.. PSK=.. REPORT_URL=http://<this-host>:8080 make flasher
make convert SN=<dongle serial> BROKER=<your broker>
```

**Path B: serial** (open the case, three wires). Back up with esptool, write
`factory.bin` at 0x0.

```sh
make esphome
make serial-backup PORT=/dev/ttyUSB0
make serial-flash  PORT=/dev/ttyUSB0
```

Step-by-step runbook for both: [docs/conversion.md](docs/conversion.md).

## What has been tested on hardware

| Piece | Status |
|---|---|
| MQTT OTA of SolaX's own image (3.003.02 → 3.010.16) | **tested** |
| MQTT OTA delivery **and boot** of a custom ESP-IDF 4.4.4 app (`examples/probe2`) | **tested** (and it stuck, as described above) |
| MQTT OTA of an ESP-IDF 5.x image (ESPHome app) | delivered and committed, **does not boot** (stock bootloader refuses it) |
| ESPHome with `solax_aa55`, flashed by serial | **tested**: link at 115200 held for the whole soak (realtime poll every 5 s), 540/540 reads on 2 concurrent clients plus HA polling, zero exceptions, Home Assistant integration working |
| Serial backup / flash / restore recipe | **tested** |
| `flasher/` (in-app factory flasher) | **BUILT BUT NOT YET EXERCISED ON HARDWARE.** Its write path has never run on a dongle. |
| `convert.py`, `Makefile`, `flasher/build.sh`, `tools/ota_server.py` backup reassembly | written for this repository, not yet run end to end |
| `tools/make_restorable.py` (path A backup → boots stock) | otadata CRC checked against a real dongle's entries; restore **not tested on hardware** |

## How the stock firmware really works

Summary; the details are in [docs/protocol.md](docs/protocol.md) and
[docs/mqtt-ota.md](docs/mqtt-ota.md).

* **Inverter side: not Modbus RTU.** The dongle talks SolaX's AA55
  "Pocket USB" protocol on UART1 (TX GPIO17, RX GPIO18), 9600 8N1, frames
  `AA 55 LEN CTRL FUNC payload SUM16`. It handshakes with `AA 55 07 01 05 0C 01`
  until the inverter answers `01 85` (inverter serial, model, type), registers
  its own serial once (`02 01 <SN>`), and if the inverter asks, ACKs with
  `01 85` and moves to 115200.
* **Modbus TCP is a tunnel.** The stock "ModbusTCPService" wraps each Modbus TCP
  request (MBAP + unit + PDU + Modbus CRC) in an AA55 frame with CTRL **0x14**;
  the inverter's own register engine answers in the same wrapping. That reaches
  the full holding/input map, VPP remote-control registers included. Stock
  spaces frames 650 ms apart at 9600 and 100 ms at 115200 and serves at most two
  clients, hence its slowness. `solax_aa55` does the same tunnel, non-blocking,
  with a queue and up to four clients.
* **Also possible over AA55**: settings writes (CTRL 9, no PIN), time sync.
  **Not possible:** feeding grid-meter readings to the inverter through the dongle.
* **Cloud side: MQTT.** The dongle talks to `mqtt001.solaxcloud.com:8883` over
  TLS without validating the certificate. Its self-update is plain ESP-IDF OTA
  driven by MQTT: a JSON command on `update/version/<SN>`, 1024-byte chunks
  (+ CRC-16) pulled from retained `update/package/<taskId>/<n>` topics, acks on
  `update/ack/...`, a whole-image CRC-16 that must come out 0, and a version
  string that must start with `V`.

## Troubleshooting (things we hit)

| Symptom | Cause / fix |
|---|---|
| esptool: `No serial data received`, adapter TX LED blinks | USB-UART adapter with a dead RX (test with a TX↔RX loopback), or wrong GND pad: on our board GND is **TP6/TP8**, not TP2 as in the PW3.0 map |
| Dongle reboot-loops while connected to the adapter | Brown-out: it was powered from the adapter's 3V3. Power it from its normal supply, share only GND |
| esptool: `Invalid head of packet` | The application is running, not the ROM loader. Hold TP7 to GND through power-on again |
| Transfer dies at higher speed | 460800 baud kills the esptool stub through a USB-UART bridge. Stay at 115200 |
| MQTT OTA downloads every chunk, then reverts with no final ack | Version string without a leading `V` |
| MQTT OTA succeeds but the dongle comes back on stock | You pushed an IDF 5.x image (e.g. ESPHome directly). Use the flasher or serial |
| ESPHome Modbus bridge gets silence from the inverter | The port speaks AA55, not Modbus RTU. Use `solax_aa55` |
| `solax_aa55` handshakes at 9600 but never links after a dongle reboot | The inverter stays at 115200 once switched; the component toggles rates every 5 s until it answers |

## Repository layout

```
README.md            this file
Makefile             build + path A/B entry points (`make help`)
convert.py           path A orchestration (server, push, backup gate, GO prompt)
esphome/             solax_aa55 component, example YAML, secrets.yaml.example
tools/               push_ota.py, make_ota_image.py (+crctab.txt), ota_server.py,
                     make_restorable.py, check_factory.py, mbverify.py, mblat.py,
                     requirements.txt
flasher/             IDF 4.4.4 in-app factory flasher + build.sh
examples/probe2/     minimal IDF 4.4.4 boot probe (sticks once pushed: read its header);
                     build: main/secrets.h from the .example, `idf.py build` in
                     espressif/idf:v4.4.4, then tools/make_ota_image.py
docs/                protocol, MQTT OTA, serial, hardware, conversion runbook
```

Run Python with `python3 -I`. Secrets (`esphome/secrets.yaml`,
`flasher/main/secrets.h`), firmware images and flash backups are git-ignored and
must never be committed: the flasher image contains your Wi-Fi PSK, a backup
contains your dongle's serial and, in its NVS, most likely your Wi-Fi credentials.

## References

* SEC Consult SA-20260212-0: Pocket WiFi 3.0, CVE-2025-15573 (MQTT cert not
  validated), CVE-2025-15574 (MQTT credentials derived from the serial),
  CVE-2025-15575 (OTA not authenticated)
* Svenson84/Solax-Pocket-Wifi-V3.0-Hack (PW3.0 pads, esptool recipe)
* jesserockz/aiosolax-uart, xdubx/Solax-Pocket-USB-reverse-engineering (AA55)
* wills106/homeassistant-solax-modbus

## License

MIT. See [LICENSE](LICENSE).
