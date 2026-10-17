# sound.py – Non-blocking UI tick for M5Dial piezo buzzer (board.SPEAKER / GPIO3)
#
# The M5Dial exposes board.SPEAKER as a single GPIO driving a piezo element.
# CircuitPython's audiopwmio is not available on ESP32-S3, so we use pwmio
# with variable_frequency to generate a short, sharp click.
#
# Each click is a single short pulse, only a cycle or two at its frequency
# -- short enough that it reads as a mechanical tick rather than a tone.
# click_heavy is the same pitch and length as click, just a heavier duty
# cycle so it hits harder. Tuned by ear on the real piezo; the numbers in
# click()/click_heavy() below are the tuning, so change them there rather
# than describing them twice.
#
# Usage:
#   import sound
#   sound.click()          # main tap feedback
#   sound.click_heavy()    # power on/off (slightly lower, longer)

import time
import board
import pwmio

# Initialise once; duty_cycle 0 = silent
try:
    _spk = pwmio.PWMOut(board.SPEAKER, variable_frequency=True, duty_cycle=0)
    _OK  = True
except Exception as e:
    print("sound init:", e)
    _OK  = False

enabled = True   # toggled by menu; persisted to NVM via app.py


def _chirp(phases):
    """Play a sequence of (frequency_hz, duration_ms, duty_cycle) phases."""
    if not _OK or not enabled:
        return
    for freq, dur_ms, duty in phases:
        _spk.frequency   = freq
        _spk.duty_cycle  = duty
        time.sleep(dur_ms / 1000)
    _spk.duty_cycle = 0   # silence


def click():
    """Crisp tap sound – mute toggle, menu navigation, menu selection.

    Single short low tick, not a tone.
    """
    _chirp([          # (frequency_hz, duration_ms, duty_cycle)
        (500, 2, 1500),
    ])


def click_heavy():
    """Slightly heavier tap – power on / power off long-press confirmation.

    Single short low tick, not a tone. Heavier duty cycle.
    """
    _chirp([
        (500, 2, 30000),
    ])
