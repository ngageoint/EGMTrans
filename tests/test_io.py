"""Tests for egmtrans.io."""

import os

import numpy as np
from osgeo import gdal, osr

from egmtrans.io import apply_scale_factor, write_array_to_geotiff, write_points_to_geojson


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
