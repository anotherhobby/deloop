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
# Requires $(MPY_CROSS) -- see its comment below if you don't have it yet.
full-deploy: install-libs deploy

# deploy: what the device should actually run, day to day -- every module
# except code.py precompiled to .mpy first (code.py must stay uncompiled
# source; see its own header comment). Not just a flash-space optimization:
# CircuitPython compiles a module's entire bytecode before running any of
# it, so plain .py costs real heap at boot whether or not most of it has
# run yet -- this used to be two separate targets (`deploy` for fast plain-
# .py iteration, `deploy-mpy` for the real thing) until plain `deploy` got
# run out of habit, silently regressing the device back into exactly the
# low-headroom state that caused a real WiFi-boot failure investigation
# (see "CircuitPython heap/boot-memory guardrails" in CLAUDE.md). Requires
# $(MPY_CROSS) -- see its comment below if missing; compiling locally is
# fast (milliseconds/file) so this isn't meaningfully slower than the old
# plain-.py path was.
deploy: _copy-files-mpy

# deploy-src: plain, uncompiled .py -- NOT what the device should run day
# to day (see `deploy` above). Real uses: no `local/mpy-cross` available
# yet, or actively chasing a traceback where uncompiled source gives a
# clearer on-device error than .mpy does. Requires no extra tooling.
deploy-src: _copy-files

# mpy-cross: path to the CircuitPython-matched mpy-cross binary -- NOT the
# generic MicroPython one from pip (`pip install mpy-cross`); that targets
# vanilla MicroPython and produces .mpy files this device's CircuitPython
# will refuse to load. Check your device's exact version first:
#   cat /Volumes/CIRCUITPY/boot_out.txt
# then download the matching build from Adafruit's official bucket (linked
# from https://learn.adafruit.com/welcome-to-circuitpython/library-file-types-and-frozen-libraries
# -> "Creating an .mpy File"), e.g. for CircuitPython 10.2.1 on macOS arm64:
#   curl -sL "https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/macos/mpy-cross-macos-10.2.1-arm64" -o local/mpy-cross
#   chmod +x local/mpy-cross
# local/ is gitignored -- this binary is platform- and version-specific,
# never commit it.
MPY_CROSS := local/mpy-cross

# Every module precompiled to .mpy by `deploy` -- everything except
# code.py, which CircuitPython requires as uncompiled source (it's the boot
# entry point, see code.py's own header comment). `.mpy` is more compact
# than what the runtime compiler produces from `.py` source AND skips
# paying that compile-time cost at all -- see CLAUDE.md's "CircuitPython
# heap/boot-memory guardrails" for why that's not just a flash-space nicety.
MPY_MODULES := config driver denon minidsp ha ha_ui state dial_ui sound app

_copy-files-mpy:
	cp src/settings.toml $(CIRCUITPY)/settings.toml
	cp src/code.py     $(CIRCUITPY)/code.py
	@rm -f $(addsuffix .py,$(addprefix $(CIRCUITPY)/,$(MPY_MODULES)))
	@mkdir -p local/build
	@for m in $(MPY_MODULES); do \
	  $(MPY_CROSS) src/$$m.py -o local/build/$$m.mpy && \
	  echo "  compiled $$m.mpy"; \
	done
	@# Compile to a local staging dir first, then cp to CIRCUITPY -- writing
	@# mpy-cross's output directly to the mounted USB drive was observed to
	@# take 100+ seconds per file (likely the mass-storage driver's flash
	@# erase/write cycle); compiling locally (<10ms/file) then copying is
	@# both far faster and consistent with how every other file in this
	@# Makefile reaches the device.
	@for m in $(MPY_MODULES); do \
	  cp local/build/$$m.mpy $(CIRCUITPY)/$$m.mpy; \
	done
	cp src/splash_logo.bmp $(CIRCUITPY)/splash_logo.bmp
	mkdir -p $(CIRCUITPY)/fonts
	cp src/fonts/FreeMonoBold_36.pcf $(CIRCUITPY)/fonts/FreeMonoBold_36.pcf
	@for s in $(FONT_SIZES); do \
	  cp src/fonts/Inter_Medium_$${s}.pcf $(CIRCUITPY)/fonts/ && \
	  echo "  copied Inter_Medium_$${s}.pcf"; \
	done

# fonts: (re)generate Inter PCF bitmaps from the TTF source.
# Requires: brew install otf2bdf bdftopcf
# Character set: printable ASCII 32-126 (A-Z a-z 0-9 punctuation) ONLY.
#
# Do not add a codepoint outside this contiguous range without reading
# this note first. U+25B6 (BLACK RIGHT-POINTING TRIANGLE, for an HA
# "Playing" glyph) was added here briefly and caused a real production
# bug: PCF's encoding table apparently has to span from the lowest to the
# highest included codepoint, so one glyph ~9500 codepoints above the
# ASCII range ballooned every generated .pcf from ~8KB to ~20KB (confirmed
# by isolating it: 32_126 alone -> 8184 bytes; +9654 -> 19564 bytes; +127
# (adjacent to the existing range) -> 8184 bytes, no change). That's real
# heap eaten at font-load time on every boot, before WiFi even connects --
# it took down WiFi entirely on real hardware ("Unknown failure 2" /
# WIFI_REASON_AUTH_EXPIRE, misleading since the real cause was upstream
# memory pressure, not anything WiFi-specific) and cost a long real
# debugging session to trace back to this. See project-context.md for the
# full incident writeup. Both play and pause glyphs are plain ASCII now
# ("|>" and "II") specifically to never hit this again -- if a Unicode
# icon glyph is ever truly necessary, use otf2bdf's `-m mapfile` to
# re-encode it onto a codepoint adjacent to the existing range instead of
# just adding its real (possibly far-away) codepoint to `-l`.

# splash: (re)generate the splash screen BMP from ui/hobbysprawl.png.
# Requires: pip install pillow
splash:
	$(PYTHON) tools/make_splash.py

# renders: render dial_ui.py's actual screens to PNGs (local/renders/) without
# needing the device -- see tools/dial_sim.py for what's simulated.
renders:
	$(PYTHON) tools/dial_sim.py

fonts:
	@mkdir -p src/fonts
	@for s in $(FONT_SIZES); do \
	  otf2bdf -p $$s -l "32_126" -r 72 $(FONT_TTF) \
	    | bdftopcf -o src/fonts/Inter_Medium_$${s}.pcf && \
	  echo "  Inter_Medium_$${s}.pcf  $$(ls -lh src/fonts/Inter_Medium_$${s}.pcf | awk '{print $$5}')"; \
	done

# File copy step for deploy-src. Also strips any stale .mpy shadow of these
# same modules left behind by a prior `deploy` -- CircuitPython's import
# would otherwise have both a fresh .py and a stale .mpy for the same
# module on the drive at once.
_copy-files:
	cp src/settings.toml $(CIRCUITPY)/settings.toml
	@rm -f $(addsuffix .mpy,$(addprefix $(CIRCUITPY)/,$(MPY_MODULES)))
	cp src/config.py   $(CIRCUITPY)/config.py
	cp src/driver.py   $(CIRCUITPY)/driver.py
	cp src/denon.py    $(CIRCUITPY)/denon.py
	cp src/minidsp.py  $(CIRCUITPY)/minidsp.py
	cp src/ha.py       $(CIRCUITPY)/ha.py
	cp src/ha_ui.py    $(CIRCUITPY)/ha_ui.py
	cp src/state.py    $(CIRCUITPY)/state.py
	cp src/dial_ui.py  $(CIRCUITPY)/dial_ui.py
	cp src/sound.py    $(CIRCUITPY)/sound.py
	cp src/app.py      $(CIRCUITPY)/app.py
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

.PHONY: bootstrap install-libs full-deploy deploy deploy-src _copy-files _copy-files-mpy ls shell probe dump-avr probe-minidsp probe-ha renders
