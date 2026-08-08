# sound.py – Non-blocking UI tick for M5Dial piezo buzzer (board.SPEAKER / GPIO3)
#
# The M5Dial exposes board.SPEAKER as a single GPIO driving a piezo element.
# CircuitPython's audiopwmio is not available on ESP32-S3, so we use pwmio
# with variable_frequency to generate a short, sharp chirp.
#
# Each click is a short descending burst -- a few milliseconds per phase,
# dropping in both frequency and duty cycle so it decays instead of ringing.
# Tuned by ear on the real piezo; the numbers in click()/click_heavy() below
# are the tuning, so change them there rather than describing them twice.
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
    """Crisp tap sound – mute toggle, menu navigation, menu selection."""
    _chirp([          # (frequency_hz, duration_ms, duty_cycle)
        (1800, 3, 10000),
        ( 800, 2, 16000),
        ( 400, 2,  6000),
    ])


def click_heavy():
    """Slightly heavier tap – power on / power off long-press confirmation."""
    _chirp([
        (1800, 4, 24000),
        (1200, 3, 12000),
        ( 800, 2,  6000),
    ])
