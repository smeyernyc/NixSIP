#!/bin/bash
# Install pjsua2 Python bindings from pjproject source
# Some steps require sudo for system libraries

set -e

echo "=== Installing pjsua2 Python bindings ==="
echo ""

# Check if we're in the right directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/pjproject_build"

# Install build dependencies (requires sudo)
echo "Step 1: Installing build dependencies..."
echo "You may be prompted for your password."
sudo apt-get update
sudo apt-get install -y \
    build-essential \
    libssl-dev \
    python3-dev \
    swig \
    libasound2-dev \
    libportaudio2 \
    portaudio19-dev \
    git

# Download pjproject
echo ""
echo "Step 2: Downloading pjproject..."
if [ ! -d "$BUILD_DIR" ]; then
    git clone https://github.com/pjsip/pjproject.git "$BUILD_DIR"
else
    echo "pjproject directory exists, updating..."
    cd "$BUILD_DIR"
    git pull
    cd "$SCRIPT_DIR"
fi

# Build pjproject
echo ""
echo "Step 3: Building pjproject..."
cd "$BUILD_DIR"
./configure --enable-shared CFLAGS='-fPIC'
make dep
make

# Install C libraries to /usr/local (so Python .so finds them at runtime)
echo ""
echo "Step 4: Installing pjproject libraries to /usr/local..."
sudo make install

# Build Python bindings
echo ""
echo "Step 5: Building Python bindings..."
cd pjsip-apps/src/swig/python
make

# Install Python bindings to YOUR account (do NOT use sudo)
echo ""
echo "Step 6: Installing Python bindings to ~/.local..."
make install

echo ""
echo "=== Installation complete! ==="
echo "Run the app with:  python3 main.py"
echo "Or use the launcher:  ./run.sh"
