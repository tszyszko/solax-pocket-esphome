# `solax_aa55` ESPHome component

One non-blocking component that owns the dongle UART, speaks SolaX's AA55
"Pocket USB" protocol to the inverter and serves Modbus TCP by tunnelling each
request in an AA55 ctrl-0x14 frame, the way stock Pocket WiFi 3.010.16 does.
Protocol details: [../docs/protocol.md](../docs/protocol.md).

Tested on an X1-Fit G4 (inverter type 15, ARM 1.42) with
`homeassistant-solax-modbus` (plugin `solax`, Modbus address 1): 540/540 reads on
two concurrent clients with Home Assistant polling alongside, no exceptions, zero
link drops, median about 210 ms (about 105 ms single-client) (stock firmware:
over 1 s and flaky). Other inverter families use the same AA55 link layer, but
only the G4 has been tried.

## Configuration

```yaml
external_components:
  - source: { type: local, path: components }

uart:
  id: inv_uart
  tx_pin: GPIO17
  rx_pin: GPIO18
  baud_rate: 9600          # the component switches to 115200 itself
  rx_buffer_size: 2048

solax_aa55:
  id: solax
  uart_id: inv_uart
  dongle_serial: SXXXXXXXXX
```

| Option | Default | Meaning |
|---|---|---|
| `dongle_serial` | required | The stick's own 10-character serial (registration no.). Sent in the `02 01` register frame. Must be exactly 10 ASCII characters. |
| `modbus_port` | `502` | Modbus TCP listen port. |
| `response_timeout` | `1000ms` | Wait for the inverter's tunnelled reply (stock: 1 s). One silent retry, then Modbus exception 0x0B to the client. |
| `frame_gap` | `100ms` | Minimum spacing between UART frames at 115200 (stock value). |
| `frame_gap_9600` | `650ms` | The same at 9600 (stock value). |
| `allow_115200` | `true` | Accept the inverter's request to move to 115200. |
| `poll_interval` | `5s` | Realtime query `01 10` while online, like stock. Needed: without a ctrl-01 query for ~30 s the inverter falls back to 9600, and tunnelled Modbus frames do not count. Takes priority over queued Modbus requests. |
| `keepalive_interval` | `10s` | Accepted for compatibility; no longer used (the realtime poll replaced the `01 05` keepalive). |
| `silence_timeout` | `30s` | No UART traffic for this long: re-handshake. |
| `max_clients` | `4` | Concurrent Modbus TCP clients (1-8). When full, the least recently active client is evicted. |

Fixed internals: request queue of 8, one request in flight, transaction IDs
matched, queued requests older than 5 s dropped, idle clients closed after 90 s,
five consecutive timeouts force a re-handshake.

## State for template entities

| Method | Returns |
|---|---|
| `linked()` | `true` once the handshake is done and the link is online |
| `registered()` | the inverter has echoed our register frame |
| `inverter_serial()` | inverter serial from the `01 85` reply |
| `inverter_type()` | model type byte (15 = X1-Hybrid/Fit G4 family) |
| `model_code()` | model code, e.g. 0x0E60 = 3680 W |
| `link_baud()` | 9600 or 115200 |
| `modbus_ok_count()`, `modbus_timeout_count()`, `modbus_retry_count()` | counters |

## Notes

* Only one component may own the UART. ESPHome's `serial_proxy` (raw UART over
  the native API for `aiosolax-uart`) is an alternative, not an addition.
* ESPHome IDF `uart::write_array(ptr, len)`: the vector overload does not compile
  on this target; the component uses the pointer form.
* Updates after the first flash: normal ESPHome OTA (port 3232) or the dashboard.
