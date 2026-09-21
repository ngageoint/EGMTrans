"""Tests for egmtrans.transform."""

import os

import numpy as np
import pytest

from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.transform import (
    create_datum_array,
    create_gdal_warp_array,
    create_interp_array,
    transform_vertical_datum,
)


def _grids_available() -> bool:
    return all(
        os.path.isfile(os.path.join(BASE_PATH, "datums", DATUM_MAPPING[datum]["grid"]))
        for datum in ("EGM96", "EGM2008")
    )


requires_grids = pytest.mark.skipif(
    not _grids_available(),
    reason="Geoid grid files not present; run 'python download_grids.py' to fetch.",
)


def test_transform_functions_exist():
    """Verify all expected transform functions are importable."""
    assert callable(create_gdal_warp_array)
    assert callable(create_interp_array)
    assert callable(create_datum_array)
    assert callable(transform_vertical_datum)


@requires_grids
def test_flatten_keeps_voids_ocean_and_flat_patches(tmp_dir):
    """Voids stay voids, ocean stays 0 m, and a lake stays one flat surface.

    Placed off Mindanao, where EGM2008 - EGM96 reaches several meters, so a
    lake that was not flattened as one patch could not pass by accident.
    Before the fastmath fix every void came out at 0 m.
    """
    from osgeo import gdal, osr

    rows = cols = 40
    nodata = -9999.0
    i, j = np.mgrid[0:rows, 0:cols]
    dem = (10.0 + i + 0.37 * j).astype(np.float32)  # no two terrain pixels share a value
    dem[:, :10] = 0.0                                 # ocean
    dem[20:30, 20:30] = 150.0                         # lake
    voids = [(0, 0), (5, 3), (25, 25), (12, 33)]      # in the ocean, the lake, and on land
    for r, c in voids:
        dem[r, c] = nodata

    src = os.path.join(tmp_dir, "voids_dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.01, 0.0, 7.90, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds = None

    out = os.path.join(tmp_dir, "voids_dem_egm96.tif")
    transform_vertical_datum(
        input_file=src,
        output_file=out,
        src_datum="EGM2008",
        tgt_datum="EGM96",
        flatten=True,
        create_mask=False,
        min_patch_size=16,
        algorithm="bilinear",
        save_log=False,
    )

    out_ds = gdal.Open(out)
    result = out_ds.GetRasterBand(1).ReadAsArray()
    out_ds = None

    void = dem == nodata
    assert np.all(np.isnan(result[void])), "a void was written as a height"
    assert np.all(np.isfinite(result[~void]))

    ocean = (dem == 0.0) & ~void
    assert np.all(result[ocean] == 0.0)

    lake = np.zeros_like(void)
    lake[20:30, 20:30] = True
    lake &= ~void
    assert np.unique(result[lake]).size == 1, "the lake surface is no longer flat"
    assert abs(float(result[lake][0]) - 150.0) > 0.5, "the lake was not shifted with the datum"


@requires_grids
def test_all_ocean_rows_stay_at_zero(tmp_dir):
    """Whole rows of ocean must come out at 0 m, not as voids.

    The output was written before its NoData value (NaN) was set, so GDAL skipped
    each all-zero row as an empty block and filled it with NaN on close. A tile
    whose southern rows are open sea lost them all. 4000 columns make every row
    its own strip, as in a real 1-arc-second tile.
    """
    from osgeo import gdal, osr

    rows, cols = 48, 4000
    dem = np.tile(np.linspace(5.0, 400.0, cols, dtype=np.float32), (rows, 1))
    dem[:, :50] = 0.0     # a strip of ocean along the west edge
    dem[30:, :] = 0.0     # and open sea across the southern rows

    src = os.path.join(tmp_dir, "coastal.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.0001, 0.0, 7.90, 0.0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds = None

    out = os.path.join(tmp_dir, "coastal_egm96.tif")
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)

    out_ds = gdal.Open(out)
    result = out_ds.GetRasterBand(1).ReadAsArray()
    out_ds = None

    assert not np.isnan(result).any(), f"{int(np.isnan(result).sum())} pixels came out as voids"
    assert np.all(result[dem == 0.0] == 0.0)
