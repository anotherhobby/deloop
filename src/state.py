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

        # Input list -- loaded from AVR at boot via denon.get_inputs().
        # input_names: list of (index, friendly_name)
        # input_index: current Zone 1 source index string
        self.input_names    = []
        self.input_index    = ""

        # Rotary encoder increments accumulated since the last volume command.
        self._pending_ticks = 0

    # ------------------------------------------------------------------
    # State update from a successful poll
    # ------------------------------------------------------------------

    def apply_status(self, status):
        """Update state from a dict returned by denon.get_status().

        Returns True if any visible field changed.
        """
        changed = (
            status["volume_db"] != self.volume_db
            or status["muted"] != self.muted
            or status["power"] != self.power
            or status["input"] != self.input
        )
        self.volume_db = status["volume_db"]
        self.muted = status["muted"]
        self.power = status["power"]
        self.input = status["input"]
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

