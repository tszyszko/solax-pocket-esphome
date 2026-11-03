# SolaX's MQTT OTA (dongle self-update)

Reverse-engineered from Pocket WiFi 3.001.02 (ESP32-S2, Ghidra Xtensa), then
used successfully: stock 3.003.02 → 3.010.16 with SolaX's own image, and a
custom IDF 4.4.4 app pushed and booted. Under the hood it is plain ESP-IDF OTA
(`esp_ota_begin` / `esp_ota_write` / `esp_ota_end` / `esp_ota_set_boot_partition`).
SolaX does not verify firmware authenticity (CVE-2025-15575).

## Getting the dongle onto your own broker

The dongle connects to `mqtt001.solaxcloud.com:8883` over TLS. It does **not**
validate the server certificate (CVE-2025-15573), so any broker with any
certificate works. Username = the 10-character dongle serial; the password is
derived from it (CVE-2025-15574). The simplest broker setup is anonymous access
on a network you control. Client ids seen: the serial, or `ESP32_<last 3 MAC bytes>`.

1. **DNS redirect.** Answer `mqtt001.solaxcloud.com` with the IP of a local
   mosquitto (Pi-hole / router static DNS), then power-cycle the dongle. A
   minimal throwaway broker:

   ```conf
   # mosquitto.conf
   listener 1883                 # plain, for push_ota.py
   listener 8883                 # TLS, for the dongle
   certfile /mosquitto/certs/server.crt
   keyfile  /mosquitto/certs/server.key
   allow_anonymous true
   log_type all                  # shows every SUBSCRIBE: the best OTA debugger
   ```

   A self-signed certificate is fine (`openssl req -x509 -newkey rsa:2048 -nodes
   -days 30 -subj /CN=mqtt001.solaxcloud.com -keyout server.key -out server.crt`).
   With `log_type all` you see the dongle pull every chunk.

2. **Repoint between your brokers** (once it is on one you control): publish to
   `base/down/<SN>`

   ```json
   {"type":2,"wifiSN":"<SN>","domain":"<host or IP>","port":8883}
   ```

   The firmware always uses `mqtts://`, so the target must speak TLS on that
   port. To go back: `"domain":"mqtt001.solaxcloud.com","port":8883`.

While on your broker the dongle no longer reports to SolaX Cloud.

## Topics

On connect the dongle subscribes to `respsynctime/<SN>`, `set/<SN>`,
`<SN>/init`, **`update/version/<SN>`**, **`base/down/<SN>`**, `base/bdown/<SN>`.

## Wire sequence

1. **Chunks**, published by us **retained** on `update/package/<taskId>/<n>`,
   n = 1..N (1-based):

   ```
   payload = image[(n-1)*1024 : n*1024] + CRC16(slice) as 2 bytes BIG-endian
   ```

   The broker never tells us when the dongle subscribes, so retained delivery is
   how a chunk arrives the instant the dongle asks for it. Without the per-chunk
   CRC the transfer fails.

2. **Command** on `update/version/<SN>`:

   ```json
   {"version":"V9.000.10","taskId":"<32 chars>","devicetype":2,"scope":8,
    "pagesize":1024,"packagesize":<total image bytes>}
   ```

   * `devicetype` 2 + `scope` 8 = update the dongle itself. Other values relay
     an update to the inverter: do not use them.
   * `pagesize` ≤ 1024 (the `esp_ota_write` ping-pong buffer is 0x400). Use 1024.
   * `packagesize` = total image bytes (despite the name); N = ceil(total / 1024).
     It must fit a stock app slot (1 MB).
   * `taskId`: topics are formatted with `%32.32s`, so use exactly 32
     characters (32 hex) or the topics get padded with spaces.
   * **`version` must start with `V`** and look like SolaX's (`V3.010.16`). The
     success/commit block is gated on `strchr(version, 'V')` followed by two
     dots. Without the `V` the dongle downloads every chunk, skips the commit and
     reboots into the old firmware with no final ack (our first attempts
     "stalled" at 920/924 chunks for exactly this reason).

3. The dongle runs `esp_ota_begin`, then subscribes to each chunk topic in turn
   and writes it. It may also ask for chunk N+1: harmless, do not serve it.

4. **Acks** on `update/ack/<taskId>/<SN>/` (note the trailing slash):

   ```json
   {"wifiSn":"<SN>","successFlag":2,"wifiVersion":"...","taskId":"...","msg":"0","progress":"<chunks so far>"}
   ```

   `successFlag` 2 = in progress (every 10 chunks), **1 = committed** (followed by
   a reboot into the new image), 0 = failed.

5. **Final checks** after the last chunk: the dongle reads the partition back
   and runs a CRC-16 over all `packagesize` bytes, which must be **0**, and
   `esp_ota_end` validates the ESP-IDF image (magic 0xE9 + SHA-256).
   SolaX's `.usb` images already CRC to zero. For any other app image append a
   2-byte big-endian CRC-16 trailer (`tools/make_ota_image.py`); `esp_ota_end`
   ignores the extra bytes.

6. Clean up: publish zero-length retained messages to every chunk topic
   (`push_ota.py` does this after a final ack, or with `--arm --clear --taskid`).

## CRC-16

SolaX's table (`tools/crctab.txt`, lifted from the 3.x firmware) is
CRC-16/UMTS (a.k.a. BUYPASS): poly 0x8005, init 0, MSB first, no reflection,
no final xor; check value `"123456789"` → 0xFEE8.

```
crc = tbl[((crc >> 8) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFF)
```

## What this can and cannot deliver

| Image | Result |
|---|---|
| SolaX 3.010.16 `.usb` | committed, boots (tested) |
| Custom ESP-IDF **4.4.4** app + CRC trailer | committed, **boots and sticks** (tested with the probe) |
| ESPHome / any ESP-IDF **5.x** app + CRC trailer | committed, but the stock IDF 4.x bootloader refuses it and boots the other slot. Looks like a rollback; it is not one. |

The stock bootloader has **no app rollback**: whatever IDF 4.4 app boots stays,
across power cycles, with no way back except serial or the app's own update
path. Never push an app without one. The flasher in `../flasher` is that app.

## Tool

```sh
python3 -I tools/push_ota.py --image build/flasher.ota.bin --sn <SN> --version V9.000.10 --broker <broker>        # dry run
python3 -I tools/push_ota.py --image build/flasher.ota.bin --sn <SN> --version V9.000.10 --broker <broker> --arm  # real push
```
