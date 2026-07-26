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

        # Speaker preset and Dirac Live -- loaded from AVR at boot via denon.
        # speaker_preset: '1' or '2'
        # dirac_filter:   '0'=Off, '1'=first filter, '2'=second filter
        # dirac_names:    list of (value, name) from get_dirac_filters()
        self.speaker_preset = "1"
        self.dirac_filter   = "1"   # "1" = Off; real value fetched at boot
        self.dirac_names    = [("2", "Filter 1"), ("1", "Off")]  # placeholder

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

