# -*- coding: utf-8 -*-
"""run_hidden.py - run a bat/command with a hidden console window, return its exit code.

Used by Task Scheduler via pythonw.exe so no console window pops up on the desktop.
Unlike wscript.exe (which hangs under Task Scheduler), this wrapper waits on the
child process handle directly with CREATE_NO_WINDOW - the child gets a real
(hidden) console, so stdout/stderr/print() keep working.

usage: pythonw.exe run_hidden.py <exe-or-bat> [arg...]
"""
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000  # no console window for the child

args = sys.argv[1:]
if not args:
    sys.exit(2)

try:
    r = subprocess.run(args, creationflags=CREATE_NO_WINDOW)
    sys.exit(r.returncode)
except Exception as e:  # pragma: no cover - defensive, keep task non-zero
    sys.exit(1)
