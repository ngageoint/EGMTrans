"""Tests for egmtrans.io."""

import os

import numpy as np
from osgeo import gdal, osr

from egmtrans.io import (
    apply_scale_factor,
    restore_nodata,
    write_array_to_geotiff,
    write_points_to_geojson,
)


class TestWriteArrayToGeotiff:
    def test_creates_file(self, tmp_dir):
        arr = np.arange(25, dtype=np.float32).reshape(5, 5)
        outpath = os.path.join(tmp_dir, "out.tif")
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        gt = (0.0, 1.0, 0.0, 5.0, 0.0, -1.0)

        write_array_to_geotiff(arr, outpath, srs.ExportToWkt(), gt)

        assert os.path.isfile(outpath)
        ds = gdal.Open(outpath)
        assert ds is not None
        assert ds.RasterXSize == 5
        assert ds.RasterYSize == 5
        result = ds.GetRasterBand(1).ReadAsArray()
        np.testing.assert_array_almost_equal(result, arr)
        ds = None


class TestApplyScaleFactor:
    def test_applies_scale_and_offset(self, tmp_dir):
        # Create a source raster with known values
        src_path = os.path.join(tmp_dir, "src.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(src_path, 3, 3, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 1.0, 0.0, 3.0, 0.0, -1.0))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        band = ds.GetRasterBand(1)
        data = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]], dtype=np.float32)
        band.WriteArray(data)
        band.SetNoDataValue(-9999.0)
        ds = None

        scaled_path = os.path.join(tmp_dir, "scaled.tif")
        result_path = apply_scale_factor(src_path, scaled_path, scale=2.0, offset=10.0, nodata_value=-9999.0)

        assert os.path.isfile(result_path)
        ds = gdal.Open(result_path)
        result = ds.GetRasterBand(1).ReadAsArray()
        expected = data * 2.0 + 10.0
        np.testing.assert_array_almost_equal(result, expected)
        ds = None


class TestWritePointsToGeojson:
    def test_creates_geojson(self, tmp_dir):
        points = {
            'x': np.array([1.0, 2.0, 3.0]),
            'y': np.array([4.0, 5.0, 6.0]),
            'z': np.array([10.0, 20.0, 30.0]),
        }
        write_points_to_geojson(points, "EGM96", tmp_dir)
        outpath = os.path.join(tmp_dir, "EGM96_points.geojson")
        assert os.path.isfile(outpath)

        import json
        with open(outpath) as f:
            data = json.load(f)
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 3


class TestRestoreNodata:
    """GDAL writes NaN as 0 into an integer band, which turned DTED voids
    (-32767) into sea level. restore_nodata puts the void value back first."""

    def test_replaces_nan_with_nodata(self):
        arr = np.array([[1.5, np.nan], [np.nan, -3.25]])
        out = restore_nodata(arr, -32767)
        assert out[0, 1] == -32767
        assert out[1, 0] == -32767

    def test_preserves_finite_values(self):
        arr = np.array([[1.5, np.nan], [0.0, -3.25]])
        out = restore_nodata(arr, -32767)
        assert out[0, 0] == 1.5
        assert out[1, 0] == 0.0
        assert out[1, 1] == -3.25

    def test_integer_array_passes_through_untouched(self):
        arr = np.array([[1, -32767], [3, 4]], dtype=np.int16)
        out = restore_nodata(arr, -32767)
        assert out is arr

    def test_survives_the_round_trip_through_an_int16_band(self, tmp_dir):
        """The end-to-end proof: without restore_nodata the void reads back as 0."""
        path = os.path.join(tmp_dir, "voids.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(path, 3, 1, 1, gdal.GDT_Int16)
        arr = np.array([[10.4, np.nan, -5.6]])
        ds.GetRasterBand(1).WriteArray(restore_nodata(arr, -32767))
        ds.FlushCache()
        stored = ds.GetRasterBand(1).ReadAsArray()
        ds = None

        assert stored[0, 1] == -32767, "void was flattened to sea level"
        assert stored[0, 0] == 10 and stored[0, 2] == -6, "GDAL rounds float to int"
