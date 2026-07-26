# deloop device configuration
# Values come from /settings.toml on the device (CircuitPython reads it at
# boot and exposes values via os.getenv). Defaults here are safe fallbacks.
# On the host Mac, os.getenv reads the shell environment; probe tools pass
# --host directly so they never rely on these values.

import os

# WiFi -- set in settings.toml
WIFI_SSID = os.getenv("WIFI_SSID", "")
WIFI_PASS = os.getenv("WIFI_PASS", "")

# AVR network settings
# AVR-X4800H: control API is on port 8080, plain HTTP.
# Port 80 redirects to HTTPS; port 443 HTTPS returns 403 for /goform/ paths.
AVR_HOST   = os.getenv("AVR_HOST", "192.168.1.100")
AVR_PORT   = int(os.getenv("AVR_PORT", "8080"))
AVR_PORT_UI = 11080  # web UI port (speaker preset, Dirac Live)
AVR_SCHEME = "http"
AVR_TIMEOUT_MS = 1000  # 1 s -- keep low so a slow AVR does not block the UI

# Polling interval -- display state is truth; poll is error correction only
POLL_INTERVAL_S  = float(os.getenv("POLL_INTERVAL", "30.0"))
POLL_INTERVAL_MS = int(POLL_INTERVAL_S * 1000)

# Volume limits and acceleration
VOLUME_MIN       = -80.0
VOLUME_MAX       = 18.0
VOLUME_STEP      = float(os.getenv("VOLUME_STEP", "0.5"))      # dB/tick normal
VOLUME_STEP_FAST = float(os.getenv("VOLUME_STEP_FAST", "2.0")) # dB/tick fast spin
# Inter-tick interval (ms) below which fast mode kicks in.
# 50ms = ~20 ticks/sec = ~1.3 revolutions/sec -- feels like a deliberate quick sweep.
ACCEL_THRESHOLD_MS = int(os.getenv("ACCEL_THRESHOLD", "100"))
# When spinning fast and increasing volume, stop here. Prevents accidental blasting.
# Slow down to push past it intentionally.
ACCEL_SAFETY_CAP = float(os.getenv("ACCEL_SAFETY_CAP", "-15.0"))

# Speaker preset display names (the AVR has no names; set these in settings.toml)
SPEAKER_PRESET_1 = os.getenv("SPEAKER_PRESET_1", "Preset 1")
SPEAKER_PRESET_2 = os.getenv("SPEAKER_PRESET_2", "Preset 2")
