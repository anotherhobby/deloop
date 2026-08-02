# code.py -- deloop entry point.
#
# CircuitPython requires this exact file (uncompiled source) as the boot
# entry point -- it cannot be a precompiled .mpy. Everything else lives in
# app.py, which can be, and (see Makefile's `mpy` target) is.
#
# The OTA check below (reading one nvm byte) must happen BEFORE `import
# app` -- not inside app.py's main() -- because `import app` alone
# unconditionally pulls in driver.py/dial_ui.py/the active backend module,
# regardless of which branch main() takes afterward. Confirmed live
# (2026-08-01): that transitive cost left too little free memory for a
# reliable TLS handshake during Check Now/Install Update. See
# ota_boot.py's module docstring and docs/ota.md for the full story.

import microcontroller
import config

try:
    _pending_ota = microcontroller.nvm[config.NVM_OTA_ACTION] != 0
except Exception:
    _pending_ota = False   # never let a boot-critical nvm read crash the boot

if _pending_ota:
    import ota_boot
    ota_boot.run()
else:
    import app
    app.main()
