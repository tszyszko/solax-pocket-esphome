# Serial flashing (path B, and recovery for path A)

This is the route that has actually put ESPHome on a dongle. It is also the
only way back once a custom app has booted.

## Pads

On the tested "PW2.0"-labelled board (3.x firmware, ESP32-S2):

| Pad | Signal |
|---|---|
| TP3 | 3V3 (do not power the board from here, see below) |
| TP4 | RX (ESP32-S2 UART0 RX, GPIO44) |
| TP5 | TX (ESP32-S2 UART0 TX, GPIO43) |
| TP6, TP8 | GND |
| TP7 | IO0 (strap: low at power-on = ROM download mode) |

The widely shared PW3.0 map (Svenson84/Solax-Pocket-Wifi-V3.0-Hack) has **TP2**
as GND. On our board TP2 was not ground and the first attempts got no reply.
Check your board with a multimeter before wiring.

## Power and wiring

* Power the dongle from its normal supply (the inverter port). **Do not** power
  it from a USB-UART adapter's 3V3 pin: it browns out and boot-loops during
  Wi-Fi bursts. Do not power it from a PC's USB port either (Svenson84's advice).
* A common ground between adapter and dongle (TP6) is mandatory.
* Check the adapter first: loop its TX to RX and type. One CP2102 stick had a
  dead RX (zero bytes back on macOS and Linux).
* An ESP32 devkit (NodeMCU-32S) works as a USB-UART bridge: tie its **EN to
  GND** (holds its own ESP32 in reset), then wire **straight**: devkit RX0 →
  TP4, devkit TX0 → TP5, GND → TP6. With the devkit's chip in reset, its
  pins RX0/TX0 carry the on-board USB-UART's TX/RX.

## Download mode

1. Short TP7 (IO0) to TP6 (GND).
2. Apply power to the dongle; keep TP7 low through power-on, then release.
3. At 115200 the ROM prints `waiting for download` on TX. If you see
   application output instead, the app is running: redo the power-on.

## esptool (v5 command names; v4 uses `read_flash` / `write_flash`)

Stay at **115200**. Through a bridge, 460800 kills the esptool stub. The
dongle has no auto-reset circuit, so always use `--before no-reset --after no-reset`.

```sh
ARGS="--chip esp32s2 --port /dev/ttyUSB0 --baud 115200 --before no-reset --after no-reset"

# 1. ALWAYS first: full 4 MB backup (4194304 bytes). Keep two copies, one off this machine.
esptool $ARGS read-flash 0 0x400000 stock-backup.bin
md5sum stock-backup.bin

# 2. ESPHome (factory image, written at 0x0)
esptool $ARGS write-flash 0x0 build/factory.bin

# Restore stock at any time (the backup is a faithful image incl. cfg_data with the serial)
esptool $ARGS write-flash 0x0 stock-backup.bin

# Restore from a path A (over-the-air) backup: use the -restorable twin, whose
# otadata boots stock instead of the flasher (tools/make_restorable.py makes it)
esptool $ARGS write-flash 0x0 stock-backup-restorable.bin
```

The same as Makefile targets: `make serial-backup`, `make serial-flash`,
`make serial-restore BACKUP=...` (with `PORT=`).

After `write-flash`, power-cycle the dongle without TP7 shorted. ESPHome comes
up on your Wi-Fi; all later updates go over ESPHome OTA, so the case stays shut.

## Symptoms

| Symptom | Meaning |
|---|---|
| `No serial data received` | adapter RX dead, TX/RX swapped, wrong GND pad, or no common ground |
| `Invalid head of packet` | the application is running, not the ROM: redo the TP7 power-on |
| reboot loop / brown-out messages while flashing | powered from the adapter's 3V3: use the normal supply |
| stub dies mid-transfer | baud above 115200 through a bridge |
