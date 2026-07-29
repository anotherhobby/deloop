PYTHON   := ./.venv/bin/python
CIRCUITPY := /Volumes/CIRCUITPY
LIB_DIR  := $(CIRCUITPY)/lib
CIRCUP   := ./.venv/bin/circup

# Inter-4.1 is a local, gitignored download (not shipped) -- grab v4.1 from
# https://github.com/rsms/inter/releases and extract it to local/Inter-4.1.
FONT_TTF  := local/Inter-4.1/extras/ttf/Inter-Medium.ttf
FONT_SIZES := 20 24 28 32 36 40

# Bootstrap: set up the host venv with all dev tools (run once after clone).
# Requires Python 3.11+ and an existing .venv (python -m venv .venv).
bootstrap:
	$(PYTHON) -m pip install -e '.[dev]'

# Install all required CircuitPython libraries onto the mounted CIRCUITPY drive.
# Re-run this after flashing fresh firmware or if a library goes missing.
install-libs:
	$(CIRCUP) install adafruit_requests
	$(CIRCUP) install adafruit_display_text
	$(CIRCUP) install adafruit_bitmap_font
	$(CIRCUP) install adafruit_focaltouch
	$(CIRCUP) install adafruit_imageload

# full-deploy: everything from scratch -- libs, settings, code, fonts.
# Run this after a fresh CircuitPython flash or when onboarding a new device.
# Prerequisite: python -m venv .venv && make bootstrap (host tools, once per machine)
full-deploy: install-libs _copy-files

# deploy: fast iteration -- settings, code, fonts. No lib reinstall.
deploy: _copy-files

# fonts: (re)generate Inter PCF bitmaps from the TTF source.
# Requires: brew install otf2bdf bdftopcf
# Character set: printable ASCII 32-126 (A-Z a-z 0-9 punctuation).

# splash: (re)generate the splash screen BMP from ui/hobbysprawl.png.
# Requires: pip install pillow
splash:
	$(PYTHON) tools/make_splash.py

# renders: render dial_ui.py's actual screens to PNGs (local/renders/) without
# needing the device -- see tools/dial_sim.py for what's simulated.
renders:
	$(PYTHON) tools/dial_sim.py

fonts:
# Requires: brew install otf2bdf bdftopcf
# Character set: printable ASCII 32-126 (A-Z a-z 0-9 punctuation).
fonts:
	@mkdir -p src/fonts
	@for s in $(FONT_SIZES); do \
	  otf2bdf -p $$s -l "32_126" -r 72 $(FONT_TTF) \
	    | bdftopcf -o src/fonts/Inter_Medium_$${s}.pcf && \
	  echo "  Inter_Medium_$${s}.pcf  $$(ls -lh src/fonts/Inter_Medium_$${s}.pcf | awk '{print $$5}')"; \
	done

# Shared file copy step used by both targets above.
_copy-files:
	cp src/settings.toml $(CIRCUITPY)/settings.toml
	cp src/config.py   $(CIRCUITPY)/config.py
	cp src/driver.py   $(CIRCUITPY)/driver.py
	cp src/denon.py    $(CIRCUITPY)/denon.py
	cp src/minidsp.py  $(CIRCUITPY)/minidsp.py
	cp src/ha.py       $(CIRCUITPY)/ha.py
	cp src/state.py    $(CIRCUITPY)/state.py
	cp src/dial_ui.py  $(CIRCUITPY)/dial_ui.py
	cp src/sound.py    $(CIRCUITPY)/sound.py
	cp src/code.py     $(CIRCUITPY)/code.py
	cp src/splash_logo.bmp $(CIRCUITPY)/splash_logo.bmp
	mkdir -p $(CIRCUITPY)/fonts
	cp src/fonts/FreeMonoBold_36.pcf $(CIRCUITPY)/fonts/FreeMonoBold_36.pcf
	@for s in $(FONT_SIZES); do \
	  cp src/fonts/Inter_Medium_$${s}.pcf $(CIRCUITPY)/fonts/ && \
	  echo "  copied Inter_Medium_$${s}.pcf"; \
	done

# List files on device
ls:
	ls -la $(CIRCUITPY)/

# Open an interactive REPL over USB serial (CircuitPython still has one)
shell:
	$(PYTHON) -m mpremote connect auto

# Run the host-side Denon HTTP probe (requires AVR on network).
# Usage: make probe
#        make probe PROBE_ARGS=--skip-write
#        make probe PROBE_ARGS=--diagnose
AVR_HOST   ?= $(shell grep '^AVR_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
AVR_PORT   ?= $(shell grep '^AVR_PORT' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' | grep -o '[0-9]*')
PROBE_ARGS ?=
probe:
	$(PYTHON) tools/probe_denon.py --host $(AVR_HOST) --port $(AVR_PORT) $(PROBE_ARGS)

# Dump full AVR state using python-denonavr (same lib as Home Assistant).
# Run once to discover API version, endpoint, and working command format.
# Requires: ./.venv/bin/pip install denonavr
dump-avr:
	$(PYTHON) tools/dump_denonavr.py --host $(AVR_HOST)

# Run the host-side minidsp-rs HTTP probe (requires the daemon reachable
# on the network -- see settings.toml.template's minidsp section).
# Usage: make probe-minidsp
#        make probe-minidsp PROBE_ARGS=--skip-write
MINIDSP_HOST ?= $(shell grep '^MINIDSP_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
MINIDSP_PORT ?= $(shell grep '^MINIDSP_PORT' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' | grep -o '[0-9]*')
probe-minidsp:
	$(PYTHON) tools/probe_minidsp.py --host $(or $(MINIDSP_HOST),127.0.0.1) --port $(or $(MINIDSP_PORT),5380) $(PROBE_ARGS)

# Run the host-side Home Assistant REST probe (requires HA reachable on
# the network -- see settings.toml.template's HA section).
# Usage: make probe-ha
#        make probe-ha PROBE_ARGS=--skip-write
HA_HOST     ?= $(shell grep '^HA_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
HA_PORT     ?= $(shell grep '^HA_PORT' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' | grep -o '[0-9]*')
HA_TOKEN    ?= $(shell grep '^HA_TOKEN' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
HA_ENTITY_ID ?= $(shell grep '^HA_ENTITY_ID' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
probe-ha:
	$(PYTHON) tools/probe_ha.py --host $(HA_HOST) --port $(or $(HA_PORT),8123) \
	  --token "$(HA_TOKEN)" --entity $(or $(HA_ENTITY_ID),media_player.office) $(PROBE_ARGS)

.PHONY: bootstrap install-libs full-deploy deploy _copy-files ls shell probe dump-avr probe-minidsp probe-ha renders
