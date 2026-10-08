"""Tests for egmtrans.io."""

import os

import numpy as np
import pytest
from osgeo import gdal, osr

from egmtrans.io import (
    apply_scale_factor,
    restore_nodata,
    round_half_away,
    write_array_to_geotiff,
    write_points_to_geojson,
)


class TestRoundHalfAway:
    def test_matches_gdal_int16_write(self):
        """The explicit rounding must agree with what GDAL does on a band write,
        including halves, which GDAL rounds away from zero."""
        values = np.array([[0.5, 1.5, 2.5, -0.5, -1.5, 0.49999, 173.5, 173.49999, -7.0, 12.0]])
        ds = gdal.GetDriverByName("MEM").Create("", values.shape[1], 1, 1, gdal.GDT_Int16)
        ds.GetRasterBand(1).WriteArray(values)
        stored = ds.GetRasterBand(1).ReadAsArray()
        np.testing.assert_array_equal(round_half_away(values), stored)

    def test_nan_passes_through(self):
        out = round_half_away(np.array([np.nan, 2.4]))
        assert np.isnan(out[0]) and out[1] == 2.0


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


class TestWriteDted:
    """A DTED file written from scratch: rounded, verified, published by rename."""

    PROFILE = os.path.join(os.path.dirname(__file__), 'data', 'dted_profile.toml')

    def _metadata(self, level=2):
        from egmtrans.dted.profile import load_profile
        from egmtrans.dted.writer import DtedMetadata

        profile = load_profile(self.PROFILE)
        profile.product['dted_level'] = level
        return DtedMetadata(None, [], profile)

    def test_writes_a_verified_file(self, tmp_dir, log_lines):
        import hashlib

        from egmtrans.dted.header import CellGeometry, read_header
        from egmtrans.dted.records import read_records
        from egmtrans.dted.validate import validate_file
        from egmtrans.io import write_dted

        cell = CellGeometry(2, 0, 85)  # zone V: 601 lines of 3601 posts
        rng = np.random.default_rng(4)
        heights = rng.uniform(-50, 2000, size=(cell.lat_points, cell.lon_lines))
        heights[0, 0] = 0.5          # halves away from zero
        heights[0, 1] = -0.5
        heights[0, 2] = 173.49999
        heights[10:13, :300] = np.nan  # a void strip
        out = os.path.join(tmp_dir, 'N85E000.dt2')

        digest = write_dted(out, cell, heights, 'EGM96', 14, tmp_dir, metadata=self._metadata())

        with open(out, 'rb') as handle:
            content = handle.read()
        assert hashlib.sha256(content).hexdigest() == digest
        assert any(f'SHA-256 {digest}' in line and f'{len(content):,} bytes' in line for line in log_lines)
        assert not [name for name in os.listdir(tmp_dir) if name.endswith('.part')]

        header = read_header(out)
        assert header['dsi.vertical_datum'] == 'E96' and header['dsi.series'] == 'DTED2'
        assert header['uhl.lon_lines'] == '0601' and header['dsi.partial_cell'] == '99'
        assert header['acc.abs_horiz_acc'] == '0012' and header['dsi.producer_code'] == 'USCNIMA '
        _header, issues = validate_file(out, check_data=True)
        assert not [issue for issue in issues if issue.severity == 'error']

        values = read_records(out).values
        assert values[0, 0] == 1 and values[0, 1] == -1 and values[0, 2] == 173
        assert np.all(values[10:13, :300] == -32767) and values[13, 0] != -32767
        expected = round_half_away(heights)
        valid = ~np.isnan(heights)
        assert np.array_equal(values[valid], expected[valid])
        with gdal.Open(out) as ds:
            assert ds.GetGeoTransform() == cell.geotransform

    def test_a_failure_leaves_no_file(self, tmp_dir):
        from egmtrans.dted.header import CellGeometry
        from egmtrans.dted.records import RecordError
        from egmtrans.dted.writer import HeaderAssemblyError
        from egmtrans.io import new_dted_header, write_dted

        cell = CellGeometry(0, 126, 6)
        heights = np.full((121, 121), 100.0)
        out = os.path.join(tmp_dir, 'N06E126.dt0')
        with pytest.raises(HeaderAssemblyError, match='security_code is required'):
            write_dted(out, cell, heights, 'EGM96', temp_dir=tmp_dir)
        with pytest.raises(HeaderAssemblyError, match='profile is for DTED level 2'):
            write_dted(out, cell, heights, 'EGM96', temp_dir=tmp_dir, metadata=self._metadata(level=2))
        for target in ('WGS84', 'EGM2008'):
            with pytest.raises(ValueError, match='EGM96 only'):
                write_dted(out, cell, heights, target, temp_dir=tmp_dir, metadata=self._metadata(level=0))
            with pytest.raises(ValueError, match='EGM96 only'):
                new_dted_header(cell, target, metadata=self._metadata(level=0))
        heights[5, 5] = 9001.0
        with pytest.raises(RecordError, match='outside'):
            write_dted(out, cell, heights, 'EGM96', temp_dir=tmp_dir, metadata=self._metadata(level=0))
        assert os.listdir(tmp_dir) == []

        # The dry run and the write build the same header.
        heights[5, 5] = 100.0
        header, sources = new_dted_header(cell, 'EGM96', 7, metadata=self._metadata(level=0))
        assert header['dsi.vertical_datum'] == 'E96' and header['acc.abs_horiz_acc'] == '0012'
        assert sources['acc.abs_horiz_acc'] == 'profile'
        assert header['dsi.unique_ref'] == '0' * 15, 'zero filled when nothing supplies it (3.13.4.1 c)'
        write_dted(out, cell, heights, 'EGM96', 7, tmp_dir, metadata=self._metadata(level=0))
        from egmtrans.dted.header import read_header

        assert read_header(out) == header


class TestLevelAgainstName:
    def test_a_level_2_cell_written_to_a_dt1_name_is_refused(self, tmp_dir):
        from egmtrans.dted.header import CellGeometry
        from egmtrans.dted.profile import load_profile
        from egmtrans.dted.writer import DtedMetadata
        from egmtrans.io import write_dted

        profile = load_profile(TestWriteDted.PROFILE)
        profile.product['dted_level'] = 0
        cell = CellGeometry(0, 126, 6)
        heights = np.full((121, 121), 100.0)
        out = os.path.join(tmp_dir, 'N06E126.dt1')
        with pytest.raises(RuntimeError, match='ambiguous'):
            write_dted(out, cell, heights, 'EGM96', temp_dir=tmp_dir, metadata=DtedMetadata(None, [], profile))
        assert os.listdir(tmp_dir) == []
