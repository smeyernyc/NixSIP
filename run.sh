#!/bin/bash
# Run SIP client, using local pjproject build if pjsua2 isn't installed

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD="$SCRIPT_DIR/pjproject_build"
# Use build-tree libs so the Python .so finds the same libpjsua2 etc. it was built with
if [ -d "$BUILD/pjsip/lib" ]; then
    export LD_LIBRARY_PATH="$BUILD/pjlib/lib:$BUILD/pjlib-util/lib:$BUILD/pjmedia/lib:$BUILD/pjnath/lib:$BUILD/pjsip/lib:$BUILD/third_party/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
# Prefer local Python bindings from build
SWIG_PY="$BUILD/pjsip-apps/src/swig/python"
if [ -d "$SWIG_PY/build" ]; then
    for lib in "$SWIG_PY/build"/lib.*; do
        [ -d "$lib" ] && export PYTHONPATH="$lib${PYTHONPATH:+:$PYTHONPATH}" && break
    done
    export PYTHONPATH="$SWIG_PY${PYTHONPATH:+:$PYTHONPATH}"
fi

# Workaround for servers (e.g. FreeSWITCH) that send Contact with sip: instead of sips:
# PJSIP then ends the call with 480 SIPS Required. Setting this allows the dialog to continue.
export PJSIP_DISABLE_SECURE_DLG_CHECK=1

exec python3 main.py "$@"
