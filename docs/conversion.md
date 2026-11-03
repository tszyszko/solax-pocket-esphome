# Conversion runbook

Read the safety section of the [README](../README.md) first. Both paths end
with the same ESPHome firmware; after that every update is a normal ESPHome OTA.

## 0. Common preparation

1. Note the dongle serial (10 characters, on the label; also the stock web UI
   password and the MQTT username).
2. Configure ESPHome:
   ```sh
   cp esphome/secrets.yaml.example esphome/secrets.yaml   # fill in Wi-Fi, API key, OTA password
   $EDITOR esphome/solax-dongle.yaml                       # set dongle_serial
   ```
3. Build: `make esphome` → `build/factory.bin` (ESPHome in Docker; use
   `make esphome ESPHOME=esphome` for a local install). The build also runs
   `tools/check_factory.py`.
4. Install the Python tools: `python3 -m pip install -r tools/requirements.txt`.

## Path A: over SolaX's MQTT OTA (no case opening; flasher untested on hardware)

**Before you start:** the flasher's write path has not run on real hardware
yet. Have the serial rig from path B ready anyway. Once the flasher is pushed,
the dongle runs it on every boot until it completes.

1. **Broker.** Get the dongle onto a mosquitto you control
   ([mqtt-ota.md](mqtt-ota.md#getting-the-dongle-onto-your-own-broker)): DNS
   redirect `mqtt001.solaxcloud.com` to it, TLS on 8883 (any cert), plain 1883
   for the tools. Confirm with `mosquitto_sub -v -t '#'` / the broker log that
   the dongle is connected.

2. **Build the flasher** against the exact factory.bin from step 0.3. The
   dongle needs your Wi-Fi and the URL where the OTA server will listen (this
   machine, reachable from the dongle's Wi-Fi):
   ```sh
   SSID='your-ssid' PSK='your-psk' REPORT_URL=http://<this-host>:8080 make flasher
   ```
   Output: `build/flasher.ota.bin` (+ `.json`). It contains your PSK.

3. **Optional dry run** of the push (connects to nothing):
   ```sh
   python3 -I tools/push_ota.py --image build/flasher.ota.bin --sn <SN> --version V9.000.10 --broker <broker>
   ```

4. **Convert:**
   ```sh
   make convert SN=<SN> BROKER=<broker>
   ```
   `convert.py` then:
   1. checks the flasher was built for this factory.bin;
   2. asks you to type the serial, then starts `tools/ota_server.py` on the
      port in REPORT_URL and pushes the flasher (`push_ota.py --arm`). Expect
      upload → `dongle flashing` progress → final ack `successFlag: 1`;
   3. the dongle reboots into the flasher: `hello slot phys=0x11....`
      (running from stock ota_1 → ESPHome goes to app0);
   4. the 4 MB backup arrives: `backup 1/16` … `16/16`, then
      `build/ota-server/stock-backup.bin` with its MD5. **Copy it somewhere safe.**
      The server also writes `stock-backup-restorable.bin` (the image to
      restore, otadata pointing at stock instead of the flasher); copy it too.
   5. the flasher downloads factory.bin, checks length + MD5 and posts `ready`;
   6. you type `GO`. The flasher first pulls factory.bin 0x0..0x10000 into
      RAM and checks its MD5 (a failure halts with nothing written). Then:
      app (streamed) → partition table → otadata → bootloader (all three from
      RAM, no network), each sector read back. Do not power-cycle;
   7. `done … VERIFIED … REBOOTING into ESPHome`.

5. **Check:** the device appears in the ESPHome dashboard / Home Assistant as
   `solax-dongle`. `Inverter link` should go on within a minute;
   `python3 -I tools/mbverify.py --host <ip>` should read the inverter serial.

### If something goes wrong (path A)

| Situation | What to do |
|---|---|
| `push_ota.py` reports FAILED | Dongle stays on stock. Check the version starts with V, the image CRC, the broker. |
| push times out | See the broker log: did it subscribe to every chunk? Clear retained chunks with `push_ota.py --arm --clear --taskid <id>`. |
| no `hello` after a successful push | The flasher booted but cannot reach Wi-Fi or REPORT_URL (wrong PSK, firewall, wrong host). Without Wi-Fi it retries forever; with Wi-Fi but no server it halts at the backup step, silently. Nothing has been written: fix the network side and power-cycle the dongle. If the Wi-Fi details compiled into it are wrong, only serial helps. |
| `HALT BACKUP_FAIL` / `DOWNLOAD_BAD` | Nothing written. Fix the server side, power-cycle the dongle, run `make resume SN=<SN>`. |
| you answered anything but GO | Nothing written; the flasher keeps polling `/go` and does not post `ready` again. Run `make resume SN=<SN>`, then **power-cycle the dongle** so the flasher starts over (hello → backup → ready). |
| `HALT HEAD_…` / `RANGE_BAD` right after GO, before `app written` | Nothing written (the 64 KB head download or its MD5 failed, or the server ignored `Range`). Fix the server side, `make resume SN=<SN>`, power-cycle the dongle. |
| `HALT` during the app write (`WRITE_FAIL`, `SHORT_READ`, `RANGE_BAD`) | Bootloader and partition table are still stock, but an app slot is partly overwritten. From `ota_0` the flasher restarts after a power cycle; from `ota_1` the stock app is damaged: path B with the restorable backup. |
| `HALT` after `app written` (`WRITE_FAIL`, `VERIFY_FAIL`, …) | Flash is partly rewritten (these writes need no network, so this is a flash error). Do not power-cycle in hope; go to path B with the backup. |
| no ESPHome after `done` | Serial: check the boot log on TP5 at 115200, or reflash `build/factory.bin`. |

## Path B: serial (tested)

See [serial.md](serial.md) for pads, wiring and symptoms.

1. Wire the adapter (TX/RX/GND only), dongle powered from its normal supply.
2. Download mode: TP7 → GND while powering on; expect `waiting for download`.
3. Backup, always first:
   ```sh
   make serial-backup PORT=/dev/ttyUSB0
   ```
   Check the file is 4194304 bytes and keep two copies.
4. Flash (download mode again if the dongle was power-cycled):
   ```sh
   make serial-flash PORT=/dev/ttyUSB0
   ```
5. Power-cycle without TP7. Continue with step 5 of path A.

To go back to stock: `make serial-restore PORT=/dev/ttyUSB0 BACKUP=backups/<file>.bin`.

From a path A backup, restore **`stock-backup-restorable.bin`**, not
`stock-backup.bin`: the latter was taken while the flasher ran, so its otadata
boots the flasher. The restorable twin is the same image with otadata pointing
at the stock slot:

```sh
esptool --chip esp32s2 --port /dev/ttyUSB0 --baud 115200 --before no-reset --after no-reset \
  write-flash 0x0 build/ota-server/stock-backup-restorable.bin
# or: make serial-restore PORT=/dev/ttyUSB0 BACKUP=build/ota-server/stock-backup-restorable.bin
```

If the server did not write it (it logs `restorable: NOT written`), make it with
`python3 -I tools/make_restorable.py build/ota-server/stock-backup.bin
[--flasher-slot ota_0|ota_1]`. Details:
[flasher/README.md](../flasher/README.md#backup-and-restore). Untested on hardware.

## Home Assistant

* ESPHome integration: adopt the device (use the API key from `secrets.yaml`).
* Modbus: `homeassistant-solax-modbus` with TCP to `<dongle-ip>:502`, Modbus
  address 1, the plugin for your inverter (`solax` for the X1-Fit G4). A 15 s
  scan interval worked well.
