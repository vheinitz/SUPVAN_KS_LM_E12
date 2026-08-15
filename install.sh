#!/usr/bin/env bash
# Install dependencies for the Katasymbol E12 label printer tools.
# No root required — installs into the user's site-packages.
set -euo pipefail

echo "==> Installing Python dependencies (user site)..."
pip3 install --user --upgrade \
    bleak \
    pillow \
    python-barcode \
    qrcode \
    PyQt6

echo
echo "Done. Verify your printer is discoverable:"
echo "    python3 katasymbol_e12.py scan"
echo
echo "Then print a barcode series, e.g.:"
echo "    python3 barcode_label.py A4:93:40:02:F3:F5 'S{id:04d}' --from 1 --to 10"
