"""Shared test fixtures."""

import os
import shutil
import sys
import tempfile

import numpy as np
import pytest

# Ensure the src directory is on the path for test imports
_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Detach handlers between tests.

    The ``egmtrans`` logger and ``_state``'s log-file path are process-global, so
    a FileHandler opened by one test otherwise survives into the next — pointing
    at a tmp_dir that has already been removed.
    """
    import logging

    from egmtrans import _state

    yield
    logger = logging.getLogger("egmtrans")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.addHandler(logging.NullHandler())
    _state.set_log_file_path(None)


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after each test."""
    d = tempfile.mkdtemp(prefix="egmtrans_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def log_lines():
    """Collect the messages the ``egmtrans`` logger emits during a test.

    ``setup_logger`` sets ``propagate = False`` and that survives between tests,
    so ``caplog`` cannot be relied on; a handler on the logger itself can.
    """
    import logging

    class ListHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    logger = logging.getLogger("egmtrans")
    logger.setLevel(logging.DEBUG)
    handler = ListHandler()
    logger.addHandler(handler)
    yield handler.lines
    logger.removeHandler(handler)


def write_geotiff(path, array, geotransform, nodata=None, gdal_type=None, point=True):
    """Write a single-band GeoTIFF in EPSG:4326 with the given geotransform."""
    from osgeo import gdal, osr

    array = np.asarray(array)
    if gdal_type is None:
        gdal_type = gdal.GDT_Int16 if np.issubdtype(array.dtype, np.integer) else gdal.GDT_Float32
    rows, cols = array.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1, gdal_type)
    ds.SetGeoTransform(geotransform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if point:
        ds.SetMetadataItem("AREA_OR_POINT", "Point")
    band = ds.GetRasterBand(1)
    band.WriteArray(array)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.FlushCache()
    band = None
    ds = None
    return path


def point_geotransform(lon0, lat0, posts, extent=1.0):
    """The geotransform of a point-registered tile whose posts run from
    (lon0, lat0 + extent) south and east over *posts* x *posts* posts, the first
    and last on the integer degree like DTED."""
    step = extent / (posts - 1)
    return (lon0 - step / 2, step, 0.0, lat0 + extent + step / 2, 0.0, -step)


def lattice_geotransform(lon0, lat0, per_degree_x, per_degree_y, extent_x=1.0, extent_y=1.0):
    """A pixel-is-point geotransform on the whole-degree lattice: *per_degree_x*
    posts per degree of longitude from *lon0*, *per_degree_y* per degree of
    latitude down from ``lat0 + extent_y``; posts on every whole degree."""
    px, py = 1.0 / per_degree_x, 1.0 / per_degree_y
    return (lon0 - px / 2, px, 0.0, lat0 + extent_y + py / 2, 0.0, -py)


def synthetic_cell(per_degree, seed_offset=0, base_cm=30000):
    """Float32 heights of a one-degree tile at *per_degree* posts per degree
    from an integer formula: whole centimeters, no two 4-neighbors alike."""
    n = per_degree + 1
    i, j = np.mgrid[0:n, 0:n]
    cm = base_cm + ((i * 7 + j * 13 + seed_offset) % 997) * 3
    return (cm / 100).astype(np.float32)


def write_dted(path, array, lon0, lat0, datum_code=None):
    """Write a DTED tile with the posts of *array* on lon0..lon0+1, lat0..lat0+1.

    121 x 121 posts make a DTED0 tile (30 arc seconds); the GDAL driver accepts
    that size everywhere, whereas an odd-sized DTED2 crop is refused. GDAL
    writes MSL as the vertical datum; *datum_code* (E08, E96) replaces it.
    """
    from osgeo import gdal

    array = np.asarray(array, dtype=np.int16)
    scratch = path + ".src.tif"
    write_geotiff(scratch, array, point_geotransform(lon0, lat0, array.shape[0]), nodata=-32767)
    gdal.Translate(path, scratch, format="DTED")
    os.remove(scratch)
    if datum_code:
        from egmtrans.dted.header import read_header, write_header

        header = read_header(path)
        header.set_raw("dsi.vertical_datum", datum_code)
        write_header(path, header)
    return path


def read_band(path):
    """The first band of a raster as an array, with the dataset closed."""
    from osgeo import gdal

    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    array = band.ReadAsArray()
    band = None
    ds = None
    return array


@pytest.fixture
def synthetic_geotiff(tmp_dir):
    """Create a minimal single-band GeoTIFF for testing."""
    from osgeo import gdal, osr

    filepath = os.path.join(tmp_dir, "test_dem.tif")
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(filepath, 10, 10, 1, gdal.GDT_Float32)

    # Simple geotransform: origin at (0, 10), 1-degree pixels
    ds.SetGeoTransform((0.0, 1.0, 0.0, 10.0, 0.0, -1.0))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())

    band = ds.GetRasterBand(1)
    data = np.arange(100, dtype=np.float32).reshape(10, 10)
    band.WriteArray(data)
    band.SetNoDataValue(-9999.0)
    band.FlushCache()
    ds = None

    return filepath


@pytest.fixture
def multiband_tiff(tmp_dir):
    """Create a multi-band GeoTIFF that should be rejected as a DEM."""
    from osgeo import gdal, osr

    filepath = os.path.join(tmp_dir, "multiband.tif")
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(filepath, 5, 5, 3, gdal.GDT_Byte)
    ds.SetGeoTransform((0.0, 1.0, 0.0, 5.0, 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for i in range(1, 4):
        ds.GetRasterBand(i).WriteArray(np.zeros((5, 5), dtype=np.uint8))
    ds = None

    return filepath
