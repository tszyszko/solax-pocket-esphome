# SolaX Pocket WiFi -> ESPHome. `make help` for the targets.
# Path A (MQTT OTA):  make esphome && make flasher && make convert
# Path B (serial):    make esphome && make serial-backup && make serial-flash

ESPHOME_IMAGE ?= ghcr.io/esphome/esphome:2026.10.2
# Override with a local install: make esphome ESPHOME=esphome
ESPHOME       ?= docker run --rm -v $(CURDIR)/esphome:/config $(ESPHOME_IMAGE)
YAML          ?= solax-dongle.yaml
PYTHON        ?= python3 -I
ESPTOOL       ?= esptool
PORT          ?= /dev/ttyUSB0
# Through a USB-UART bridge keep 115200; 460800 kills the esptool stub.
BAUD          ?= 115200
ESPTOOL_ARGS   = --chip esp32s2 --port $(PORT) --baud $(BAUD) --before no-reset --after no-reset

# make convert SN=... BROKER=...
SN            ?=
BROKER        ?=
BROKER_PORT   ?= 1883

.PHONY: help esphome flasher check convert resume serial-backup serial-flash serial-restore pycheck clean

help:
	@echo "make esphome                 build esphome/$(YAML) -> build/factory.bin"
	@echo "make check                   sanity-check build/factory.bin for the flasher"
	@echo "make flasher                 SSID=.. PSK=.. REPORT_URL=http://<host>:<port> -> build/flasher.ota.bin"
	@echo "make convert SN=.. BROKER=.. path A: push flasher over MQTT, gate on backup + GO"
	@echo "make resume SN=..            path A: flasher already on the dongle, serve + gate again"
	@echo "make serial-backup PORT=..   path B: read the full 4 MB flash -> backups/"
	@echo "make serial-flash PORT=..    path B: write build/factory.bin at 0x0 (needs a backup)"
	@echo "make serial-restore PORT=.. BACKUP=backups/<file>.bin   write a 4 MB backup back"
	@echo "make pycheck                 byte-compile all Python"

build:
	mkdir -p build

esphome: | build
	@test -f esphome/secrets.yaml || { echo "esphome/secrets.yaml missing: copy esphome/secrets.yaml.example"; exit 1; }
	cd esphome && $(ESPHOME) compile $(YAML)
	@f=$$(find esphome/.esphome/build -name firmware.factory.bin -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2); \
	  test -n "$$f" || { echo "firmware.factory.bin not found under esphome/.esphome/build"; exit 1; }; \
	  cp "$$f" build/factory.bin && echo "build/factory.bin <- $$f"
	$(PYTHON) tools/check_factory.py build/factory.bin

check:
	$(PYTHON) tools/check_factory.py build/factory.bin

flasher: | build
	flasher/build.sh build/factory.bin build/flasher.ota.bin

convert:
	@test -n "$(SN)" -a -n "$(BROKER)" || { echo "usage: make convert SN=<dongle serial> BROKER=<host> [BROKER_PORT=1883]"; exit 1; }
	$(PYTHON) convert.py --sn $(SN) --broker $(BROKER) --broker-port $(BROKER_PORT)

resume:
	@test -n "$(SN)" || { echo "usage: make resume SN=<dongle serial>"; exit 1; }
	$(PYTHON) convert.py --sn $(SN) --resume

serial-backup:
	mkdir -p backups
	@f=backups/stock-backup-$$(date +%Y%m%d-%H%M%S).bin; \
	  $(ESPTOOL) $(ESPTOOL_ARGS) read-flash 0 0x400000 $$f && \
	  test $$(stat -c %s $$f) -eq 4194304 && md5sum $$f && \
	  echo "Copy $$f somewhere safe (off this machine too) before flashing."

serial-flash:
	@ls backups/*.bin >/dev/null 2>&1 || { echo "no backup in backups/: run make serial-backup first"; exit 1; }
	$(PYTHON) tools/check_factory.py build/factory.bin --serial
	$(ESPTOOL) $(ESPTOOL_ARGS) write-flash 0x0 build/factory.bin

serial-restore:
	@test -n "$(BACKUP)" || { echo "usage: make serial-restore BACKUP=backups/<file>.bin"; exit 1; }
	@test $$(stat -c %s "$(BACKUP)") -eq 4194304 || { echo "$(BACKUP) is not a 4 MB image"; exit 1; }
	$(ESPTOOL) $(ESPTOOL_ARGS) write-flash 0x0 "$(BACKUP)"

pycheck:
	$(PYTHON) -m py_compile convert.py tools/*.py esphome/components/solax_aa55/__init__.py

clean:
	rm -rf build flasher/build flasher/sdkconfig flasher/sdkconfig.old flasher/main/factory_expect.h
