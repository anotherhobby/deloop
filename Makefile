PYTHON   := ./.venv/bin/python
CIRCUITPY := /Volumes/CIRCUITPY
LIB_DIR  := $(CIRCUITPY)/lib
CIRCUP   := ./.venv/bin/circup

# Inter-4.1 is a local, gitignored download (not shipped) -- grab v4.1 from
# https://github.com/rsms/inter/releases and extract it to local/Inter-4.1.
FONT_TTF  := local/Inter-4.1/extras/ttf/Inter-Medium.ttf
# Exactly the three sizes dial_ui.py loads (_F_LG/_F_MD/_F_SM).
FONT_SIZES := 20 24 36

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
	# ota.py's sha256 verification -- this board's native hashlib has no
	# sha256 at all (confirmed live, 2026-07-31); adafruit_hashlib falls
	# back to a pure-Python implementation, confirmed byte-identical to
	# CPython's hashlib.sha256 including its incremental .update() path.
	$(CIRCUP) install adafruit_hashlib

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
# run yet, and a device left running plain source sits in exactly the
# low-headroom state that caused a real WiFi-boot failure investigation
# (see "CircuitPython heap/boot-memory guardrails" in CLAUDE.md).
#
# Note the naming trap: `deploy` is the .mpy one and `deploy-src` is the
# plain-.py one, which is the opposite of what "src" suggests to most
# people. Requires $(MPY_CROSS) -- see its comment below if missing;
# compiling locally is milliseconds per file, so this is not the slow path.

# Every copy target needs the drive mounted. Without this the first cp just
# fails with a bare "No such file or directory", which is genuinely confusing
# when the usual cause is that you ran `make usb-drive-off` -- the one thing
# the README now tells every new user to do (see "Last step" there).
_require-drive:
	@test -d $(CIRCUITPY) || { \
	  echo ""; \
	  echo "  $(CIRCUITPY) is not mounted."; \
	  echo ""; \
	  echo "  If you have run 'make usb-drive-off' (recommended, so OTA can"; \
	  echo "  remount the filesystem), the drive is hidden on purpose. Run:"; \
	  echo ""; \
	  echo "      make usb-drive-on     # resets the device, drive returns in ~5s"; \
	  echo ""; \
	  echo "  then re-run this target. 'make usb-drive-off' puts it back."; \
	  echo "  Otherwise: check the USB cable and that the device is powered."; \
	  echo ""; \
	  exit 1; }

deploy: _require-drive _copy-files-mpy

# deploy-code: same .mpy compile as `deploy`, but skips splash_logo.bmp and
# the fonts/ directory entirely -- those rarely change and each cp to the
# mounted CIRCUITPY drive pays a real per-file flash-write cost (see the
# comment on _copy-files-mpy below), which adds up during rapid iteration
# on code alone. Not what a fresh device/full-deploy needs (use `deploy`
# for that) -- this is purely a fast path for "I only changed a .py file."
deploy-code: _require-drive _copy-code-mpy

# deploy-src: plain, uncompiled .py -- NOT what the device should run day
# to day (see `deploy` above). Real uses: no `local/mpy-cross` available
# yet, or actively chasing a traceback where uncompiled source gives a
# clearer on-device error than .mpy does. Requires no extra tooling.
deploy-src: _require-drive _copy-files

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
# code.py and boot.py, which CircuitPython requires as uncompiled source
# (see each file's own header comment). `.mpy` is more compact than what
# the runtime compiler produces from `.py` source AND skips paying that
# compile-time cost at all -- see CLAUDE.md's "CircuitPython heap/boot-
# memory guardrails" for why that's not just a flash-space nicety.
#
# This list is one of three that must agree: the other two are
# tools/build_release_manifest.py's _MPY_MODULES and the compile step in
# .github/workflows/release.yml. A module here but missing there still
# deploys fine over USB and simply stops being OTA-updatable, with no
# error anywhere -- see release.yml's header for what that cost once.
MPY_MODULES := config driver denon minidsp camilladsp ha ha_ui wiim wiim_ui state dial_ui sound app ota ota_boot version

_copy-code-mpy:
	cp src/settings.toml $(CIRCUITPY)/settings.toml
	cp src/code.py     $(CIRCUITPY)/code.py
	cp src/boot.py     $(CIRCUITPY)/boot.py
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

_copy-files-mpy: _copy-code-mpy
	cp src/splash_logo.bmp $(CIRCUITPY)/splash_logo.bmp
	mkdir -p $(CIRCUITPY)/fonts
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
# full incident writeup. The play/pause indicator sidesteps the question
# entirely -- it is drawn as vectorio shapes, with no glyph involved (see
# dial_ui.py's _build_icon). If a Unicode icon glyph is ever truly
# necessary, use otf2bdf's `-m mapfile` to re-encode it onto a codepoint
# adjacent to the existing range instead of just adding its real (possibly
# far-away) codepoint to `-l`.

# splash: (re)generate the splash screen BMP from ui/hobbysprawl.png.
# Requires: pip install pillow
splash:
	$(PYTHON) tools/make_splash.py

# renders: render dial_ui.py's actual screens to PNGs (local/renders/) without
# needing the device -- see tools/dial_sim.py for what's simulated.
renders:
	$(PYTHON) tools/dial_sim.py

# ui-renders: regenerate the polished per-backend screenshots in ui/ that
# feed README.md (ui-denon.png, ui-minidsp.png, ui-wiim.png,
# ui-camilladsp.png, ui-homeassistant.png, ui-muted.png, ui-standby.png),
# then compose them into the single ui-devices-grid.png README actually
# embeds -- see tools/render_ui_grid.py's module docstring for why README
# embeds one composed image rather than a table of five separate <img>
# tags. One subprocess per backend -- DEVICE_DRIVER binds at import time,
# so a single process can't switch mid-run; see
# tools/render_ui_screenshots.py's module docstring.
# Run this after any dial_ui.py change that affects layout/colors/text.
# MINIDSP_HOST/CAMILLADSP_HOST etc. are never touched -- rendering only
# imports config.py/dial_ui.py, no network calls, no real device needed.
# CAMILLADSP_PRESETS/CAMILLADSP_QUICK_PRESETS below are NOT optional --
# camilladsp.py's CAPS["preset_quickbuttons"] is derived from
# len(config.CAMILLADSP_QUICK_PRESETS) at import time, so leaving it unset
# renders a real-looking but buttonless screenshot even though
# render_ui_screenshots.py's fixture sets state.preset_quick_names -- CAPS
# gates whether dial_ui.py draws the row at all, regardless of what's in
# state. Confirmed live: this was exactly the first bug in this target.
ui-renders:
	DEVICE_DRIVER=denon \
	  $(PYTHON) tools/render_ui_screenshots.py --backend denon
	DEVICE_DRIVER=minidsp VOLUME_MIN=-50.0 VOLUME_MAX=0.0 \
	  $(PYTHON) tools/render_ui_screenshots.py --backend minidsp
	DEVICE_DRIVER=wiim WIIM_PRESET_NAMES="Bass +8,Night,Vocal" \
	  $(PYTHON) tools/render_ui_screenshots.py --backend wiim
	DEVICE_DRIVER=camilladsp \
	  CAMILLADSP_PRESETS="Flat:/path/to/flat.yml,Quiet:/path/to/quiet.yml,Muffled:/path/to/muffled.yml" \
	  CAMILLADSP_QUICK_PRESETS="Flat,Quiet,Muffled" \
	  $(PYTHON) tools/render_ui_screenshots.py --backend camilladsp
	DEVICE_DRIVER=ha VOLUME_MIN=0.0 VOLUME_MAX=100.0 \
	  $(PYTHON) tools/render_ui_screenshots.py --backend ha
	$(PYTHON) tools/render_ui_grid.py

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
	cp src/ota.py      $(CIRCUITPY)/ota.py
	cp src/ota_boot.py $(CIRCUITPY)/ota_boot.py
	cp src/version.py  $(CIRCUITPY)/version.py
	cp src/driver.py   $(CIRCUITPY)/driver.py
	cp src/denon.py    $(CIRCUITPY)/denon.py
	cp src/minidsp.py  $(CIRCUITPY)/minidsp.py
	cp src/camilladsp.py $(CIRCUITPY)/camilladsp.py
	cp src/ha.py       $(CIRCUITPY)/ha.py
	cp src/ha_ui.py    $(CIRCUITPY)/ha_ui.py
	cp src/wiim.py     $(CIRCUITPY)/wiim.py
	cp src/wiim_ui.py  $(CIRCUITPY)/wiim_ui.py
	cp src/state.py    $(CIRCUITPY)/state.py
	cp src/dial_ui.py  $(CIRCUITPY)/dial_ui.py
	cp src/sound.py    $(CIRCUITPY)/sound.py
	cp src/app.py      $(CIRCUITPY)/app.py
	cp src/code.py     $(CIRCUITPY)/code.py
	cp src/boot.py     $(CIRCUITPY)/boot.py
	cp src/splash_logo.bmp $(CIRCUITPY)/splash_logo.bmp
	mkdir -p $(CIRCUITPY)/fonts
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

# Toggle CIRCUITPY's USB mass-storage drive via boot.py's nvm-byte check
# (src/boot.py) -- lets Install Update's storage.remount() succeed without
# physically ejecting/reconnecting, while keeping the serial console
# (REPL/prints) available. Hiding the drive is the RECOMMENDED end state for
# any device using OTA, not just a dev convenience -- see the README's "Last
# step". Requires a hard reset to take effect (boot.py only runs then), which
# these trigger directly; reconnect with `make shell` afterward.
# Run `make usb-drive-on` before `make deploy` -- CIRCUITPY must be mounted
# for the file copy to have anywhere to write to.
#
# Both targets fought the same thing twice, in opposite directions: a hard
# reset takes the USB device away, and neither mpremote nor the host copes
# with that gracefully.
#
# 1. The nvm write and the reset are deliberately SEPARATE invocations.
#    microcontroller.reset() drops the USB CDC port mid-session, so mpremote's
#    own cleanup (transport.close() setting RTS on a vanished fd) raises
#    "OSError: [Errno 6] Device not configured" and the target exits non-zero
#    -- even though both the write and the reset landed. That made a working
#    target look broken and would break any chain depending on it. Only the
#    reset ignores errors; combining them would have swallowed a genuine
#    failure to write the byte too.
# 2. The nvm write RETRIES, because the port takes seconds to re-enumerate
#    after a reset. Running these back to back (or straight after any other
#    reset) otherwise fails instantly with "mpremote: no device found" --
#    found live 2026-08-06 running off/on/off in sequence.
#
# Neither exit code is trusted regardless: each target then waits on the mount
# point itself, which is the outcome actually being asked for.
usb-drive-off:
	@i=0; until $(PYTHON) -m mpremote connect auto exec \
	    "import gc, microcontroller; gc.collect(); microcontroller.nvm[5] = 1" >/dev/null 2>&1; do \
	  i=$$((i+1)); \
	  if [ $$i -gt 20 ]; then \
	    echo "  ERROR: no device on the serial port after 20s."; exit 1; \
	  fi; \
	  sleep 1; \
	done
	@$(PYTHON) -m mpremote connect auto exec "import microcontroller; microcontroller.reset()" >/dev/null 2>&1 || true
	@printf "  hiding the USB drive"
	@i=0; while [ -d "$(CIRCUITPY)" ]; do \
	  i=$$((i+1)); \
	  if [ $$i -gt 30 ]; then \
	    echo ""; echo "  ERROR: $(CIRCUITPY) still mounted after 30s."; exit 1; \
	  fi; \
	  printf "."; sleep 1; \
	done; echo " done."

usb-drive-on:
	@i=0; until $(PYTHON) -m mpremote connect auto exec \
	    "import gc, microcontroller; gc.collect(); microcontroller.nvm[5] = 0" >/dev/null 2>&1; do \
	  i=$$((i+1)); \
	  if [ $$i -gt 20 ]; then \
	    echo "  ERROR: no device on the serial port after 20s."; exit 1; \
	  fi; \
	  sleep 1; \
	done
	@$(PYTHON) -m mpremote connect auto exec "import microcontroller; microcontroller.reset()" >/dev/null 2>&1 || true
	@printf "  restoring the USB drive"
	@i=0; while [ ! -d "$(CIRCUITPY)" ]; do \
	  i=$$((i+1)); \
	  if [ $$i -gt 30 ]; then \
	    echo ""; echo "  ERROR: $(CIRCUITPY) did not appear within 30s."; exit 1; \
	  fi; \
	  printf "."; sleep 1; \
	done; echo " done."

# Run the host-side Denon HTTP probe (requires AVR on network).
# Usage: make probe
#        make probe PROBE_ARGS=--skip-write
#        make probe PROBE_ARGS=--diagnose
AVR_HOST   ?= $(shell grep '^AVR_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
AVR_PORT   ?= $(shell grep '^AVR_PORT' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' | grep -o '[0-9]*')
PROBE_ARGS ?=
probe:
	$(PYTHON) tools/probe_denon.py --host $(AVR_HOST) --port $(AVR_PORT) $(PROBE_ARGS)

# Host-side AVR latency/reliability check -- independent of the M5 Dial
# entirely, so a bad run here is real evidence the AVR itself is the
# problem rather than deloop/ESP32-side flakiness. See tools/avr_health_check.py.
# Usage: make avr-health
#        make avr-health PROBE_ARGS="--count 30 --timeout 2"
avr-health:
	$(PYTHON) tools/avr_health_check.py --host $(AVR_HOST) $(PROBE_ARGS)

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

# Run the host-side WiiM/LinkPlay HTTPS probe (requires the streamer
# reachable on the network -- see settings.toml.template's WiiM section).
# Usage: make probe-wiim
#        make probe-wiim PROBE_ARGS=--skip-write
WIIM_HOST ?= $(shell grep '^WIIM_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
probe-wiim:
	$(PYTHON) tools/probe_wiim.py --host $(WIIM_HOST) $(PROBE_ARGS)

# Run the host-side CamillaDSP websocket probe (requires the process
# reachable on the network -- see settings.toml.template's camilladsp
# section). Uses the `websocket-client` package (a known-good WebSocket
# implementation) rather than src/camilladsp.py's own hand-rolled client --
# a failure here means "check CAMILLADSP_HOST/PORT", not "check the driver".
# Usage: make probe-camilladsp
#        make probe-camilladsp PROBE_ARGS=--skip-write
CAMILLADSP_HOST ?= $(shell grep '^CAMILLADSP_HOST' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
CAMILLADSP_PORT ?= $(shell grep '^CAMILLADSP_PORT' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' | grep -o '[0-9]*')
probe-camilladsp:
	$(PYTHON) tools/probe_camilladsp.py --host $(CAMILLADSP_HOST) --port $(or $(CAMILLADSP_PORT),1234) $(PROBE_ARGS)

# Run the host-side GitHub Release sanity check (no device/daemon needed --
# just network access to github.com). Confirms the changelog/history
# release release.yml published has the expected release/manifest/asset
# shapes -- the device's own update path is S3, see src/ota.py.
# Usage: make probe-ota
OTA_REPO ?= $(shell grep '^OTA_REPO' src/settings.toml 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
probe-ota:
	$(PYTHON) tools/probe_ota.py --repo $(or $(OTA_REPO),anotherhobby/deloop) $(PROBE_ARGS)

# Build a release manifest.json from whatever's already staged in a
# directory (by default, local/build/ -- whatever `make deploy` last
# compiled there). Sanity-check locally before this same script runs in CI
# (.github/workflows/release.yml). OTA_VERSION is just a placeholder int
# for local testing; the real workflow auto-computes it (latest tag + 1).
build-manifest:
	$(PYTHON) tools/build_release_manifest.py --dir local/build --out local/build/manifest.json --version $(or $(OTA_VERSION),0)

# ON-DEVICE regression check for ota.py's real install sequence (release
# fetch -> minimal metadata extraction -> manifest -> every real asset,
# resolved+downloaded+verified) -- unlike probe-ota above, this runs the
# actual deployed ota.py/ota_boot.py on real hardware, over real Wi-Fi.
# Stops short of the final rename-into-place commit, so a pass never
# overwrites the files currently running on the device. Run after any
# change to ota.py's Fetcher/session/apply() internals. See
# tools/ota_regression_check.py's module docstring for why this exists.
probe-ota-regression:
	$(PYTHON) -m mpremote connect auto run tools/ota_regression_check.py

# ON-DEVICE network-only stress test -- no dial_ui.py/rendering at all, just
# continuous status polling + periodic commands over the real denon.py async
# engine, with plain-text live stats on screen (no gauge bitmap). Isolates
# networking behavior from rendering-caused memory pressure. Runs forever;
# Ctrl-C or power cycle to stop. WARNING: toggles AVR mute periodically --
# see tools/network_stress_check.py's docstring.
network-stress:
	$(PYTHON) -m mpremote connect auto run tools/network_stress_check.py

# ON-DEVICE combined stress test -- real dial_ui.py rendering AND the real
# denon.py networking engine running together, plus synthetic random-sized
# bytearray churn to actively fragment the heap. Deliberately worse than the
# real app (fixed-cadence redraws regardless of change, continuous alloc
# noise) -- built to try to reproduce the rendering/networking interaction
# instability that network-stress and avr-health each failed to reproduce in
# isolation. Runs forever; Ctrl-C or power cycle to stop. WARNING: toggles
# AVR mute periodically -- see tools/chaos_stress_check.py's docstring.
chaos-stress:
	$(PYTHON) -m mpremote connect auto run tools/chaos_stress_check.py

.PHONY: _require-drive usb-drive-on usb-drive-off bootstrap install-libs full-deploy deploy deploy-code deploy-src fonts splash _copy-code-mpy _copy-files _copy-files-mpy ls shell probe avr-health dump-avr probe-minidsp probe-ha probe-wiim probe-camilladsp probe-ota build-manifest probe-ota-regression network-stress chaos-stress renders ui-renders
