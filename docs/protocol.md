# The dongle ↔ inverter protocol (AA55 "Pocket USB")

The inverter's dongle port does **not** speak Modbus RTU. It speaks SolaX's
"Pocket USB" AA55 protocol. Everything below was reverse-engineered from stock
Pocket WiFi firmware 3.010.16 (Ghidra, Xtensa) and from the X1-Fit G4 inverter
firmware (ARM 1.42), and the link-layer parts were confirmed on the wire.
Byte maps of the large realtime/settings replies are still open.

## Physical layer

* Dongle UART1: **TX GPIO17, RX GPIO18**, 8N1 (`uart_set_pin(1, 17, 18, -1)`).
* Starts at **9600**; moves to **115200** after the handshake if the inverter
  asks for it (the X1-Fit G4 does). Stock never uses any other rate.
* Once switched, the inverter **stays** at 115200, even across a dongle reboot,
  so a handshake must try both rates.
* UART0 (the TP4/TP5 pads) is the separate programming/console port.

## Frame layout

```
AA 55 LEN CTRL FUNC payload... SUMlo SUMhi
```

* `LEN` counts the whole frame (`7 + payload`).
* `SUM` = 16-bit additive sum of all preceding bytes, little-endian.
* `CTRL` bits 7:6 extend `LEN` bits 9:8: `len |= (ctrl & 0xC0) << 2; ctrl &= 0x3F`.
  Used by the 407-byte realtime/settings replies (`LEN` 0x97 + one extension bit).
* Stock also parses long frames `BB 55 LENlo LENhi FUNC data SUM16`.
* A reply usually has `FUNC | 0x80`; a NAK has `FUNC | 0x40` and no payload.

## Link bring-up (stock behaviour, copied by `solax_aa55`)

| Step | Frame | Notes |
|---|---|---|
| Handshake / serial query | `AA 55 07 01 05 0C 01` | Once per second until the inverter answers `01 85`. |
| Reply `01 85` | 42-byte payload | inverter SN [14 B ASCII] + 14 spaces + model code u16 LE (0x0E60 = 3680 W) + dongle serial [8 B] + model **type** at raw[0x2B] (15 = X1-Hybrid/Fit G4 family) + `00 01`. If `LEN ≥ 0x30`, NewFunctionCode = raw[0x2D]; **raw[0x2E] == 1 → the inverter wants 115200**. |
| Register | `AA 55 11 02 01 <10-char dongle SN> SUMlo SUMhi` | Once per boot; the inverter echoes it verbatim. Re-register only when the inverter sends `02 41`. |
| ACK + baud change | `AA 55 07 01 85 8C 01`, wait 1 s, switch to 115200 | Only when raw[0x2E] == 1. |
| Keepalive | `01 10` realtime query | Stock: every 5 s (plus `01 05` every ~295 s); `solax_aa55`: realtime `01 10` every `poll_interval` (5 s). Without a ctrl-01 query the inverter drops back to 9600 after ~30 s; tunnelled Modbus does not count. |
| Silence | — | 30 s without traffic: toggle 9600/115200 and redo `01 05`. |

The dongle serial is the first 10 bytes of the stock `cfg_data` partition
(0x210000); the u16 at offset 14 is the byte sum of bytes 0..13.

Stock pacing: 650 ms between frames at 9600, 100 ms at 115200, 1 s reply
timeout. That spacing is most of why stock Modbus TCP is slow.

After the link is up, stock 3.010.16 sends a settings query `01 <S4>`, a
version frame `AA 55 0F 17 00 "3.010.16" AC 02`, realtime `01 <S3>` every 5 s,
`01 05` every ~295 s, settings every ~305 s and `01 04` when energy changes.

## The Modbus tunnel: CTRL 0x14

This is how stock "ModbusTCPService" works and what `solax_aa55` does:

```
request:  AA 55 (n+9) 14 <slot> <MBAP(6) + unit + PDU  = n bytes> <Modbus CRC16 lo hi> <SUM16 lo hi>
reply:    same wrapping
```

* `slot` is stock's TCP client slot (1 or 2); the inverter echoes it. It sits
  in the FUNC position, so in the reply the **MBAP starts at raw[5]**.
* The Modbus CRC16 (poly 0xA001, init 0xFFFF) covers `unit + PDU` only.
* Stock copies `L - 9` bytes back to the client and sets MBAP length = `L - 15`.
* Stock: at most 2 TCP clients, 30 s idle close, 1 s reply timeout.
* On the inverter this lands in the full register engine: the whole holding
  and input map, including the VPP / remote-control block 0x7C–0x8A (FC16;
  watchdog-refreshed; needs the inverter in running state). Both unit 1 and
  unit 2 answer on the G4.

## Function codes by inverter type (stock code table)

| Type | realtime | settings | settings-write CTRL |
|---|---|---|---|
| 1, 2 | `01 01` | `01 06` | 0x03 |
| 3 | `01 0B` | `01 15` | 0x04 |
| 4 | `01 0C` | `01 16` | 0x05 |
| 5 | `01 0D` | `01 17` | 0x06 |
| 6 | `01 0E` | `01 18` | 0x07 |
| 7 | `01 0F` | `01 19` | 0x08 |
| other, incl. **15** (X1-Hybrid/Fit G4) | `01 10` → `01 90` (407 B) | `01 1A` → `01 9A` (407 B) | **0x09** |

NewFunctionCode 0xCC / 0xDD selects 0x13 / 0x1C instead. On the G4, `01 0C`
and `01 16` are NAKed (`01 4C` / `01 56`). Errors: `01 04` → `01 84` (44 B).

## What the inverter accepts (X1-Fit G4, ARM 1.42; handler FUN_08045ca8)

* **CTRL 1** queries: 0x10 realtime → 0x90 (mirrors the Modbus input registers:
  grid V/I/P, PV1/PV2 V/I, energies), 0x1A settings → 0x9A (mirrors the
  writable settings), 0x03, 0x04 errors, 0x05 serial, 0x07, 0x13, 0x14 → 0x94,
  0x1B (sub-page in frame[5]) → 0x9B, 0x1C, 0x1E self-test → 0x9E, 0x20;
  0x85 = ACK flag.
* **CTRL 2**: 01 register serial (stored in EEPROM), **02 time sync**
  `02 02 Y M D h m s` sets the inverter RTC, 06 network info.
* **CTRL 9: settings write** `AA 55 09 09 <func> <u16 LE> SUMlo SUMhi`,
  range-checked, **no PIN gate at protocol level**: 0x00 grid code, 0x24 work
  mode (0 self-use / 1 feed-in / 2 backup), 0x2F inverter on/off, 0x8A–0x92
  charge/discharge power (< 8000 W), SoC limits (< 101), TOU; funcs 0x00–0x7E
  mirror the LCD/EEPROM settings. Stock treats any CTRL 3..9 / 0x11 / 0x12
  frame as the ack (func ≠ 0 OK, func = 0 rejected; 150 ms timeout).
  `solax_aa55` does not send settings writes.
* CTRL 0x10 inverter firmware update, 0x12 self-test / IV curve, 0x13 restart,
  **0x14 Modbus tunnel**.
* Other stock frames seen: `02 03` net info, `17 00` version, `12` raw
  passthrough, `10 01`/`10 02` inverter update, `01 1B/14/1F/1E`, replies
  0x81, 0x8B–0x90, 0x93 realtime, 0xA1 battery.

## What it cannot do: meter injection

No AA55 frame and no Modbus register carries CT / grid-meter values. The
inverter's only meter bypass is the COM485 "DataHub" mode (Function Select =
DataHub), which stops Meter485 polling but is an export-limit aggregator on a
different UART, not a self-use grid feed. The dongle is for monitoring, VPP and
settings, not for replacing the meter.

## Open questions

* Byte maps of `01 90`, `01 9A`, `01 9B`, `01 9E` (inverter builders
  FUN_080399a0, FUN_0803b4f8; stock dongle handlers for 0x8B–0x90 / 0x93).
* Whether other inverter families tunnel CTRL 0x14 the same way.

## References

* jesserockz/aiosolax-uart (`const.py`: CTRL query 01 / register 02 / setting
  write 05 / setting response 03; func 05 serial, 0C data, 04 errors, 16 settings)
* xdubx/Solax-Pocket-USB-reverse-engineering (`05 00` PIN, `05 42` power limit,
  `05 60` limit on/off on older types)
