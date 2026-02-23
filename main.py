#!/usr/bin/env python3
"""Compact SIP client for Linux: multi-account, dialpad, mute, end, answer, TLS (no SRTP)."""

import sys
import os

# If pjsua2 isn't installed, try loading from local pjproject build
def _add_pjsua2_path():
    if "pjsua2" in sys.modules:
        return
    root = os.path.dirname(os.path.abspath(__file__))
    for subdir in ("pjproject_build/pjsip-apps/src/swig/python", "pjsip-apps/src/swig/python"):
        base = os.path.join(root, subdir)
        # Prefer build/lib... so we get the compiled .so
        build = os.path.join(base, "build")
        if os.path.isdir(build):
            for name in os.listdir(build):
                if name.startswith("lib."):
                    lib = os.path.join(build, name)
                    if lib not in sys.path:
                        sys.path.insert(0, lib)
                    break
        if os.path.isfile(os.path.join(base, "pjsua2.py")):
            if base not in sys.path:
                sys.path.insert(0, base)
            break

_add_pjsua2_path()

def main():
    try:
        from gui import main as gui_main
        gui_main()
    except ImportError as e:
        if "gi" in str(e) or "Gtk" in str(e):
            sys.stderr.write("GTK not found. Install: python3-gi python3-gi-cairo gir1.2-gtk-3.0\n")
        else:
            sys.stderr.write("Import error: %s\n" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
