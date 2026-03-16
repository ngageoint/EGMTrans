"""Shared test fixtures."""

import os
import sys
import tempfile
import shutil

import numpy as np
import pytest

# Ensure the src directory is on the path for test imports
_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after each test."""
    d = tempfile.mkdtemp(prefix="egmtrans_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


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
