#!/usr/bin/env python3
"""
render_ui_screenshots.py -- regenerate the polished per-backend screenshots
in ui/ that README.md embeds: ui-denon.png, ui-minidsp.png, ui-wiim.png,
ui-camilladsp.png, ui-homeassistant.png, plus the backend-agnostic
ui-muted.png/ui-standby.png.

Why this exists instead of just using tools/dial_sim.py directly: dial_sim's
own scenario set (make renders, into local/renders/) always uses one
Denon-shaped fixture (_base_state()) regardless of which DEVICE_DRIVER is
active. That's fine for dial_sim's own purpose (exercising dial_ui.py's
drawing code generically), but reused as-is for a *different* backend it
produces real nonsense -- confirmed 2026-07-30: minidsp.py's friendly_input()
mangling the Denon-shaped raw input "SAT/CBL" into "Sat/cbl", and wiim.py
showing "Unknown"/a blank "--" volume because -20.5 isn't a valid value on
its 0-100 range. Each backend needs its own realistic fixture (real source
names, a volume level sane for its own VOLUME_MIN/MAX, real preset shapes)
-- that's what the _fixture_* functions below are.

One process = one backend, always: config.py/driver.py/dial_ui.py all bind
to DEVICE_DRIVER (and any VOLUME_MIN/MAX override) at first import, and
Python caches imported modules -- so a single run of this script can only
ever render the one backend whose env the process started with. --backend
is required and cross-checked against DEVICE_DRIVER as a safety net against
silently rendering the wrong backend into the wrong filename. `make
ui-renders` is what actually loops over every backend, one subprocess each.

Usage (single backend, e.g. while iterating on one backend's UI):
    DEVICE_DRIVER=minidsp VOLUME_MIN=-50 VOLUME_MAX=0 \\
        python tools/render_ui_screenshots.py --backend minidsp

Usage (everything, matching what README embeds):
    make ui-renders
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dial_sim  # noqa: E402 -- installs CircuitPython shims, imports
                  # dial_ui/config/driver bound to whatever DEVICE_DRIVER
                  # was in the environment at this import -- see docstring.

OUT_DIR = ROOT / "ui"


def _fixture_denon():
    state = dial_sim.AVRState()
    state.power, state.brightness = "ON", 1.0
    state.input = "SAT/CBL"
    state.preset = "2"
    state.preset_names = [("2", "Movie"), ("3", "Music"), ("4", "Night")]
    state.preset_quick_names = list(state.preset_names)
    state.preset_enabled = True
    state.volume_db = -20.5
    return state


def _fixture_minidsp():
    state = dial_sim.AVRState()
    state.power, state.brightness = "ON", 1.0
    state.input = "Toslink"   # a real minidsp.py source name, not a Denon one
    state.preset = "0"
    state.preset_names = [("0", "Movie"), ("1", "Music"), ("2", "Night"), ("3", "Flat")]
    state.preset_quick_names = list(state.preset_names)
    state.preset_enabled = True
    state.volume_db = -25.0   # mid-range on the -50..0 illustrative override
    return state           # (config.py's real default is -127..0 -- the full
                            #  native range -- unchanged; see make ui-renders)


def _fixture_wiim():
    import config   # already imported by dial_sim; re-import just binds the name
    state = dial_sim.AVRState()
    state.power, state.brightness = "ON", 1.0
    state.input = "1"           # raw WiiM mode code -> friendly_input() = "AirPlay"
                                 # (mode "10" is also real -- confirmed live, see
                                 # wiim.py's _MODE_NAME -- and literally means "WiiM",
                                 # its own network/multiroom idle state, but that reads
                                 # oddly as a screenshot's "current input"; AirPlay is
                                 # more recognizable and equally confirmed-live)
    state.media_state = "playing"
    # WiiM can never report which Favorite (if any) is currently active --
    # state.preset stays "" always (see wiim.py's get_presets()) -- so the
    # realistic fixture is CAPS["presets"] True with nothing selected,
    # rendering wiim_ui.py's literal "(Set Preset)" placeholder, not a named
    # slot. Needs WIIM_PRESET_NAMES/WIIM_PRESET_BUTTONS actually set in the
    # environment (see the Makefile line) for CAPS["presets"] to be True at
    # all -- same "CAPS is config-derived at import time, state alone can't
    # fake it" gotcha as camilladsp's quick-button row, see CLAUDE.md.
    #
    # Taken from the driver rather than rebuilt here, because the quick list
    # is a configured *subset* of the full one (WIIM_PRESET_BUTTONS) -- a
    # hand-assembled fixture that set both to the same list would render a
    # button row this backend never actually draws.
    import driver as _driver
    _, state.preset_names = _driver.get_presets()
    state.preset_quick_names = _driver.get_quick_presets()
    state.preset_enabled = True
    state.volume_db = 65        # native 0-100 percent range
    return state


def _fixture_camilladsp():
    import config   # already imported by dial_sim; re-import just binds the name
    state = dial_sim.AVRState()
    state.power, state.brightness = "ON", 1.0
    state.input = "96khz"   # what camilladsp.py shows while GetState == "Running"
    presets = config.CAMILLADSP_PRESETS or [
        ("/path/to/flat.yml", "Flat"),
        ("/path/to/quiet.yml", "Quiet"),
        ("/path/to/muffled.yml", "Muffled"),
    ]
    quick = config.CAMILLADSP_QUICK_PRESETS or presets
    state.preset_names, state.preset_quick_names = presets, quick
    state.preset = presets[0][0]
    state.preset_enabled = True
    state.volume_db = -25.0   # mid-range on the -50..0 default (config.py's real default)
    return state


def _fixture_ha():
    state = dial_sim.AVRState()
    state.power, state.brightness = "ON", 1.0
    state.input = "AirPlay"   # a real HA source name, not a raw entity id
    # HA has no generic media_player equivalent of Dirac Live/config slots
    # -- CAPS["presets"] is always False (see ha.py) -- so preset_names/
    # preset_quick_names stay empty, same as every other HA render.
    state.preset_names = state.preset_quick_names = []
    state.preset_enabled = False
    # CAPS["player_select"] is unconditionally True for ha.py (import-time
    # constant, not a live-query result -- see ha.py's CAPS literal), so
    # this is realistic even though the fixture never calls driver.get_players().
    # Mirrors the real discovered entity set confirmed live against the
    # user's house (see CLAUDE.md's "Backend #3: HA media_player" section)
    # -- "Dining Room" is the currently targeted device, matching one of
    # the two real entities that share that friendly name there.
    state.player_id = "media_player.dining_room"
    state.player_names = [
        ("media_player.office", "Office"),
        ("media_player.dining_room", "Dining Room"),
        ("media_player.wiim_pro_38da", "Dining Room"),
        ("media_player.shield", "Shield"),
        ("media_player.cabin", "Cabin"),
    ]
    state.media_state = "playing"   # media controls (play/pause + skip) active
    state.volume_db = 60            # native 0-100 percent range
    return state


_FIXTURES = {
    "denon": _fixture_denon,
    "minidsp": _fixture_minidsp,
    "wiim": _fixture_wiim,
    "camilladsp": _fixture_camilladsp,
    "ha": _fixture_ha,
}


def _save(ui, name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.png"
    dial_sim.frame(dial_sim.render(ui)).save(path)
    print(f"  {path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=sorted(_FIXTURES))
    args = parser.parse_args()

    active = os.environ.get("DEVICE_DRIVER", "denon")
    if active != args.backend:
        sys.exit(
            "DEVICE_DRIVER={!r} in the environment does not match --backend {!r} "
            "-- config.py/driver.py bind to DEVICE_DRIVER at import time, so these "
            "must match in a single process. Use `make ui-renders` to do all "
            "backends correctly, or set DEVICE_DRIVER yourself to match --backend."
            .format(active, args.backend)
        )

    state = _FIXTURES[args.backend]()
    # Set here rather than in each _fixture_*, so a new fixture can't forget
    # it. draw_main() refuses to render device state it hasn't actually heard
    # from the device and falls back to draw_reconnecting() (see
    # state.AVRState.power_known); the fixtures build AVRState() directly
    # instead of going through dial_sim._base_state(), which is where that
    # flag normally gets set. Missing this silently rendered "lost connection"
    # for EVERY backend from 2026-08-05 (when power_known landed) until
    # 2026-08-06 -- the failure looks like a plausible screen, not a crash,
    # and nothing here re-runs on its own, so it sat in ui/ unnoticed.
    state.power_known = True
    ui = dial_sim.dial_ui.init()
    dial_sim.dial_ui.draw_main(ui, state)
    # README's filename is "ui-homeassistant.png", not "ui-ha.png" -- the
    # --backend/DEVICE_DRIVER value stays the short "ha" (matching
    # config.py/driver.py's DEVICE_DRIVER check), only the output filename
    # spells it out.
    filename = "ui-homeassistant" if args.backend == "ha" else "ui-" + args.backend
    _save(ui, filename)

    # Backend-agnostic extras (mute/standby styling doesn't meaningfully
    # differ by backend beyond CAPS["power"], and Denon has every capability
    # this project models) -- generated only on the "denon" run so `make
    # ui-renders` doesn't write them 4 times.
    if args.backend == "denon":
        state.muted = True
        dial_sim.dial_ui.draw_main(ui, state)
        _save(ui, "ui-muted")
        state.muted = False

        state.power = "STANDBY"
        dial_sim.dial_ui.draw_main(ui, state)
        _save(ui, "ui-standby")


if __name__ == "__main__":
    main()
