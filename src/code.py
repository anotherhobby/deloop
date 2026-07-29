# code.py -- deloop entry point.
#
# CircuitPython requires this exact file (uncompiled source) as the boot
# entry point -- it cannot be a precompiled .mpy. Everything else lives in
# app.py, which can be, and (see Makefile's `mpy` target) is.

import app

app.main()
