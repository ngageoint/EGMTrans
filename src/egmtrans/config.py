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


def verify_grids(src_datum: str, tgt_datum: str) -> None:
    """Check that the geoid grid files needed for a transformation exist.

    Raises:
        FileNotFoundError: If a required grid file is missing, with
            instructions on how to download it.
    """
    for datum in (src_datum, tgt_datum):
        grid = DATUM_MAPPING.get(datum, {}).get('grid')
        if grid is None:
            continue
        path = os.path.join(get_datums_dir(), grid)
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Required geoid grid file not found: datums/{grid}\n\n"
                f"Download the grid files by running:  python download_grids.py\n"
                f"Or download manually from: "
                f"https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1"
            )


def _bundled_proj_db_is_compatible() -> bool:
    """Return True iff the bundled ``crs/proj.db`` is usable by the installed PROJ.

    The bundled database has a ``DATABASE.LAYOUT.VERSION`` that was current at
    the time it was generated (PROJ 9.6.2, layout 1.5). If the installed PROJ
    library has moved past that layout version, any EPSG lookup using the
    bundled db will raise ``RuntimeError: DATABASE.LAYOUT.VERSION.MINOR = N
    whereas a number >= M is expected``. We compare the bundled and installed
    PROJ major.minor versions and only consider the bundled db usable when it
    is at least as new as the installed library.
    """
    import sqlite3

    from osgeo import osr

    db_path = os.path.join(get_crs_dir(), 'proj.db')
    if not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'PROJ.VERSION'"
        ).fetchone()
        conn.close()
        if not row:
            return False
        bundled = tuple(int(p) for p in row[0].split('.')[:2])
    except (sqlite3.Error, ValueError):
        return False

    installed = (osr.GetPROJVersionMajor(), osr.GetPROJVersionMinor())
    return bundled >= installed


def configure_gdal() -> None:
    """Set GDAL/PROJ configuration options. Must run before any GDAL operations.

    - Forces PROJ to work offline with the local datum grids (``PROJ_NETWORK=OFF``).
    - Points ``PROJ_DATA`` at the project's ``crs/`` directory so the bundled
      ``proj.db`` is used instead of the system default — but only when the
      bundled database is compatible with the installed PROJ library. On
      newer systems the bundled db is silently skipped and the system
      ``proj.db`` is used instead.
    - Sets ``GDAL_CACHEMAX`` to 512 MB for faster I/O with temporary files.
    - Suppresses GDAL FutureWarnings and enables GDAL exceptions.
    - Prevents ArcPy from creating ``.aux.xml`` sidecar files.
    """
    from osgeo import gdal

    gdal.SetConfigOption('PROJ_NETWORK', 'OFF')
    if _bundled_proj_db_is_compatible():
        gdal.SetConfigOption('PROJ_DATA', get_crs_dir())
    gdal.SetConfigOption('GDAL_CACHEMAX', '512')

    warnings.filterwarnings("ignore", category=FutureWarning, module="osgeo.gdal")
    gdal.UseExceptions()

    os.environ["ARCPY_NO_AUX_XML"] = "TRUE"
