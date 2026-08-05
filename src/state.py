# state.py -- Local model of AVR state
#
# Holds the last-known AVR state and tracks pending changes from the
# rotary encoder so rapid spinning batches into fewer HTTP calls.

import config


class AVRState:
    """Holds the current known state of the AVR and pending control changes."""

    def __init__(self):
        self.volume_db = -40.0
        self.muted = False
        self.power = "STANDBY"
        self.input = ""

        # Have we actually heard the device state this, or is "STANDBY" just
        # the initial value above? Without this they are indistinguishable,
        # and dial_ui.draw_main() renders the standby power ring for both --
        # so a device we cannot reach at all looked exactly like one that is
        # switched off (confirmed 2026-08-04: it fooled both of us during a
        # debugging session). Set True by apply_status(); app.py clears it
        # again when a run of failed polls means we have lost contact, so
        # the UI falls back to "lost connection" rather than claiming the
        # device is off.
        self.power_known = False

        # Raw playback-state string from backends that track it (currently
        # only ha.py -- "playing"/"paused"/"on"/"idle"/etc, or "" for
        # backends that never report this). dial_ui.py shows a play/pause
        # status line + code.py offers a matching tap target only when this
        # is exactly "playing" or "paused" -- see driver.py's contract note.
        self.media_state = ""

        # Input channel count, from backends that can determine it (currently
        # only camilladsp.py). None for backends that never report this --
        # deliberately None, not 0, so "no data" and "confirmed silent/zero
        # channels" (not a real scenario, but keep the distinction) aren't
        # conflated. Not shown anywhere yet -- see driver.py's contract note.
        self.channels = None

        # Presets -- a device-specific "pick one of a few stored configs"
        # menu (Dirac Live filters on Denon, DSP config slots on MiniDSP;
        # see driver.py's CAPS["presets"]). Loaded at boot via
        # driver.get_presets(); empty for drivers that don't support it.
        # preset:       currently selected slot's value, as a string
        # preset_names: list of (value, name) from get_presets() -- real
        #               slots only, never a synthetic "off" entry
        # preset_enabled: whether that slot is actually engaged, independent
        #               of which one is selected (see CAPS["preset_enable"]
        #               and driver.get_preset_enabled()/set_preset_enabled())
        self.preset         = ""
        self.preset_names   = []
        self.preset_enabled = True

        # Main-screen quick-select button subset -- usually identical to
        # preset_names (see driver.py's get_quick_presets() contract note),
        # but can be a smaller, separately-configured list for a backend
        # whose full preset_names can outgrow the button row (currently only
        # camilladsp.py). Loaded at boot via driver.get_quick_presets();
        # dial_ui.py's button row and its tap-rect math both read this, not
        # preset_names.
        self.preset_quick_names = []

        # Input list -- loaded from AVR at boot via denon.get_inputs().
        # input_names: list of (index, friendly_name)
        # input_index: current Zone 1 source index string
        self.input_names    = []
        self.input_index    = ""

        # Media player list -- backends that can target more than one
        # device discovered at runtime (see driver.py's CAPS["player_select"];
        # currently only ha.py, one entry per Home Assistant media_player
        # entity). Loaded at boot via driver.get_players(); empty for
        # drivers that don't support it. Same (id, name) shape as
        # input_names/preset_names above.
        self.player_id    = ""
        self.player_names = []

        # Rotary encoder increments accumulated since the last volume command.
        self._pending_ticks = 0

    # ------------------------------------------------------------------
    # State update from a successful poll
    # ------------------------------------------------------------------

    def apply_status(self, status):
        """Update state from a dict returned by denon.get_status().

        Returns True if any visible field changed.
        """
        media_state = status.get("media_state", "")
        channels = status.get("channels")
        changed = (
            status["volume_db"] != self.volume_db
            or status["muted"] != self.muted
            or status["power"] != self.power
            or status["input"] != self.input
            or media_state != self.media_state
            or channels != self.channels
        )
        self.volume_db = status["volume_db"]
        self.muted = status["muted"]
        self.power = status["power"]
        self.power_known = True    # heard it from the device, not a default
        self.input = status["input"]
        self.media_state = media_state
        self.channels = channels
        return changed

    # ------------------------------------------------------------------
    # Rotary encoder
    # ------------------------------------------------------------------

    def add_ticks(self, ticks):
        """Accumulate rotary encoder ticks. Positive = clockwise = louder."""
        self._pending_ticks += ticks

    def take_pending_ticks(self):
        """Return accumulated tick count and reset to zero."""
        ticks = self._pending_ticks
        self._pending_ticks = 0
        return ticks

    def apply_volume_delta(self, ticks, step=None):
        """Update local volume immediately (optimistic) without waiting for a poll.

        Args:
            ticks: Number of ticks (positive = up, negative = down).
            step:  dB per tick override. Defaults to config.VOLUME_STEP.
        """
        if step is None:
            step = config.VOLUME_STEP
        delta_db = ticks * step
        self.volume_db = max(
            config.VOLUME_MIN,
            min(config.VOLUME_MAX, self.volume_db + delta_db),
        )

    # ------------------------------------------------------------------

