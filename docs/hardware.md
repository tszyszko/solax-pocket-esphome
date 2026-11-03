# Hardware

## Supported (or expected to be)

| Stick | Firmware | Status |
|---|---|---|
| "Pocket WiFi 2.0"-labelled board with 3.x firmware (MAC prefix 7C:DF:A1) | 3.001.02 / 3.003.02 / 3.010.16 seen | **tested**: MQTT OTA, serial flash, ESPHome running |
| Pocket WiFi 3.0 (part no. 618.00415.00) | 3.x | same ESP32-S2 platform and firmware line; pads documented by Svenson84 differ in GND (TP2); not tried here |

How to tell: the stock web UI / `optType=ReadRealTimeData` API reports a 3.x
dongle version, and the image SolaX ships for it is an ESP-IDF image (first byte
0xE9). If in doubt, read the flash over serial first.

## Not supported

* Pocket WiFi **V2** with 2.x firmware (part no. 618.00122.00): STM32F2 +
  MXCHIP EMW3162 on MiCO OS. Different hardware entirely; the "UNLOCKED"
  2.033.20 / 2.034.06 images on the internet are for that.
* Pocket WiFi **4.0**: did not link with the X1-Fit G4 on the bench and was not
  investigated further.

## The ESP32-S2 dongle

* ESP32-S2, 4 MB flash, **no secure boot, no flash encryption**
  (SEC Consult SA-20260212-0).
* Inverter link: UART1, TX GPIO17, RX GPIO18, 9600/115200 8N1 (AA55 protocol,
  see [protocol.md](protocol.md)).
* Programming: UART0 on test pads, IO0 strap on TP7 (see [serial.md](serial.md)).
* Stock bootloader at 0x1000 is ESP-IDF 4.x (header `e9 04 02 2f`), no app
  rollback, refuses IDF 5.x app images.

### Stock partition table

| Name | Type | Offset | Size |
|---|---|---|---|
| nvs | data/nvs | 0x9000 | 16 K |
| otadata | data/ota | 0xD000 | 8 K |
| phy_init | data/phy | 0xF000 | 4 K |
| ota_0 | app | 0x10000 | 1 M |
| ota_1 | app | 0x110000 | 1 M |
| cfg_data | 0x40 | 0x210000 | 64 K (dongle serial in bytes 0..9) |
| offline_data | 0x41 | 0x220000 | ~1.9 M |

### After conversion (ESPHome default ESP-IDF 4 MB layout)

| Name | Offset | Size |
|---|---|---|
| otadata | 0x9000 | 0x2000 |
| phy_init | 0xB000 | 0x1000 |
| app0 | 0x10000 | 0x1C0000 |
| app1 | 0x1D0000 | 0x1C0000 |
| nvs | 0x390000 | 0x70000 |

## Stock firmware notes

* Web UI: HTTP Basic auth `admin` / `<dongle serial>`; curl needs `--http0.9`.
  The old form API answers `optType=ReadRealTimeData&pwd=<SN>`.
* Local firmware upload (`POST /update.htm`, multipart `imagefile`) rejected
  both SolaX's encrypted 3.015.02 and plain 3.010.16 images; the MQTT OTA did not.
* 3.010.16 has a local Modbus TCP server on 502 (the AA55 0x14 tunnel, slow);
  later dongle firmware reportedly drops it in favour of a reverse connection.

## Tested inverter

SolaX X1-Fit G4 3.68 kW (type 15, X1-Hybrid G4 firmware family, ARM 1.42 /
DSP 1.45). Other SolaX inverters with an AA55 dongle port probably work for the
link layer; whether they answer the 0x14 Modbus tunnel is unknown.
