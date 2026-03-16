"""Constants, datum mapping, path helpers, and GDAL configuration.

Centralises all compile-time constants (supported extensions, datum EPSG codes
and grid filenames, invalid filename patterns) and the one-time GDAL/PROJ
configuration that must run before any geospatial operation.
"""

from __future__ import annotations

import os
import warnings
from typing import TypedDict

SUPPORTED_EXTENSIONS = ('.tif', '.tiff', '.dt0', '.dt1', '.dt2')
DTED_EXTENSIONS = ('.dt0', '.dt1', '.dt2')
INVALID_CHARACTERS = '<>:"/\\|?*'
INVALID_FILENAMES = (
    'ortho',
    'image',
    'mask',   # Generic mask file, like EGMTrans flat mask
    'AMP',    # TanDEM-X Amplitude Mosaic
    'EDM',    # TanDEM-X Edit Data Mask
    'HEM',    # TanDEM-X Height Error Map
    'SDM',    # TanDEM-X Source Data Mask
    'WBM',    # TanDEM-X Water Body Mask
    'SPM',    # DGED Source Processing Mask
    'SLM',    # DGED Source Lineage Mask
)


class DatumDetails(TypedDict):
    epsg: int
    grid: str | None
    dted_code: str | None


DATUM_MAPPING: dict[str, DatumDetails] = {
    'WGS84': {
        'epsg': 4979,
        'grid': None,
        'dted_code': None,
    },
    'EGM96': {
        'epsg': 5773,
        'grid': 'us_nga_egm96_1.tif',
        'dted_code': 'E96',
    },
    'EGM2008': {
        'epsg': 3855,
        'grid': 'us_nga_egm08_1.tif',
        'dted_code': 'E08',
    },
}

# Resolve project root: env-var override, or walk up from this file.
# This file lives at src/egmtrans/config.py — three levels up is the project root.
BASE_PATH = os.environ.get(
    'EGMTRANS_BASE_PATH',
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def get_datums_dir() -> str:
    """Return the path to the ``datums/`` directory containing geoid grid TIFFs."""
    return os.path.join(BASE_PATH, 'datums')


def get_crs_dir() -> str:
    """Return the path to the ``crs/`` directory containing the local PROJ database."""
    return os.path.join(BASE_PATH, 'crs')


def configure_gdal() -> None:
    """Set GDAL/PROJ configuration options. Must run before any GDAL operations.

    - Forces PROJ to work offline with the local datum grids (``PROJ_NETWORK=OFF``).
    - Points ``PROJ_DATA`` at the project's ``crs/`` directory so the bundled
      ``proj.db`` is used instead of the system default.
    - Sets ``GDAL_CACHEMAX`` to 512 MB for faster I/O with temporary files.
    - Suppresses GDAL FutureWarnings and enables GDAL exceptions.
    - Prevents ArcPy from creating ``.aux.xml`` sidecar files.
    """
    from osgeo import gdal

    gdal.SetConfigOption('PROJ_NETWORK', 'OFF')
    gdal.SetConfigOption('PROJ_DATA', get_crs_dir())
    gdal.SetConfigOption('GDAL_CACHEMAX', '512')

    warnings.filterwarnings("ignore", category=FutureWarning, module="osgeo.gdal")
    gdal.UseExceptions()

    os.environ["ARCPY_NO_AUX_XML"] = "TRUE"
