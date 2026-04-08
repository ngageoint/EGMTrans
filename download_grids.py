#!/usr/bin/env python3
"""Download EGMTrans geoid grid files from GitHub Releases.

Run this script after cloning or downloading EGMTrans to fetch the geoid
grid GeoTIFFs that are required for vertical datum transformation.  The
files are placed in the ``datums/`` directory.

Usage::

    python download_grids.py
"""

import os
import sys

# Make the egmtrans package importable even when not installed via pip.
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from egmtrans.download import ensure_grids

if __name__ == "__main__":
    ensure_grids()
