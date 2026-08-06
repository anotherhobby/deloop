# boot.py -- USB mass-storage toggle. Hiding the drive is the RECOMMENDED
# end state for any device that will use OTA, not just a dev convenience:
# see README's "Last step: turn off the USB drive". It began as the latter,
# hence the testing-focused rationale below, which still applies.
#
# storage.remount("/", readonly=False) (used by ota_boot.py's Install
# Update path) fails with "Cannot remount path when visible via USB"
# whenever CIRCUITPY is mounted as a drive on the host -- normally that
# means physically ejecting the drive (but keeping the USB cable/serial
# console connected) before every single test, which gets old fast.
#
# storage.disable_usb_drive() hides just the mass-storage interface while
# leaving the USB CDC serial console (REPL, print() output) fully
# available -- exactly what's needed to watch Install Update's real
# behavior live instead of testing blind on a bare power source. It only
# works from boot.py: CircuitPython raises if it's called after USB has
# already enumerated, which happens well before code.py ever runs. boot.py
# itself only runs on a genuine hard reset (never Ctrl-D, never
# supervisor.reload()) -- toggling this requires one hard reset to take
# effect, via `make usb-drive-off` / `make usb-drive-on` (see Makefile).
#
# That one hard reset is harmless to OTA testing despite this project's
# "hard reset breaks TLS for the rest of that power cycle" finding (see
# docs/ota.md) -- confirmed live (2026-08-01) that finding is specifically
# about a TLS attempt made immediately on the SAME boot a hard reset
# produced; Check Now succeeded fine that same session after a much
# earlier hard reset, once reached via a normal soft-reloaded menu tap.
# As long as Install Update is triggered normally through the menu (which
# always goes through supervisor.reload() first), this toggle's own reset
# doesn't taint that later attempt.
#
# Zero effect on production/normal use: this file only ever calls
# disable_usb_drive() when config.NVM_USB_DRIVE_DISABLED reads exactly 1 --
# never set by anything except the two Makefile targets above. Leave it
# alone (or run `make usb-drive-on`) to get CIRCUITPY mounting normally
# again for day-to-day `make deploy`.

import microcontroller
import storage
import config

try:
    if microcontroller.nvm[config.NVM_USB_DRIVE_DISABLED] == 1:
        storage.disable_usb_drive()
except Exception as e:
    print("boot.py: usb drive toggle check failed:", e)
