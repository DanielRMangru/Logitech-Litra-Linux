#!/bin/bash
set -e

echo "=== Logitech Litra Controller Build Script ==="

# Check if uv is available
if command -v uv &> /dev/null; then
    echo "Using uv (system python) to compile standalone binary..."
    # Force uv to use the system python to avoid Tkinter binary version mismatch
    uv run --python /usr/bin/python3 --with pyinstaller --with pyusb pyinstaller --onefile --name litra-control app.py
else
    echo "uv not found. Creating a local Python virtual environment..."
    # Standard venv fallback
    python3 -m venv .venv
    source .venv/bin/activate
    echo "Installing dependencies..."
    pip install -r requirements.txt
    echo "Compiling standalone binary..."
    pyinstaller --onefile --name litra-control app.py
    deactivate
fi

echo "=== Build Complete ==="
echo "The standalone binary has been created at: dist/litra-control"
echo "To test the binary, run:"
echo "  ./dist/litra-control --scan"
