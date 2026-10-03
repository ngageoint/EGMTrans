"""DTED made from a GeoTIFF on the whole-degree lattice: the end-to-end
conversion, its refusals, the lattice geoid and what a failure leaves behind.

The cells sit in zone V (80 to 90 degrees), where a level-2 cell is 3601 rows
by 601 columns, so the work grid stays small; the source tiles are 300 posts
per degree, built from integer formulas, so source post (r, c) is cell post
(12 r, 2 c).
"""

import math
import os
from fractions import Fraction

import numpy as np
import pytest
from osgeo import gdal, osr

from egmtrans import _state, transform
from egmtrans.cli import process_file
from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.dted.header import CellGeometry, read_header
from egmtrans.dted.profile import HarvestConfig, Profile
from egmtrans.dted.records import read_records
from egmtrans.dted.schema import HEADER_LENGTH, NULL_ELEVATION
from egmtrans.dted.validate import validate_file
from egmtrans.dted.writer import DtedMetadataSource
from egmtrans.io import round_half_away
from egmtrans.transform import geoid_on_cell, load_input, source_grid, transform_vertical_datum
from tests.conftest import lattice_geotransform, read_band, synthetic_cell, write_geotiff

PER_DEGREE = 300
LON0, LAT0 = 30, 85   # zone V


def _grids_available() -> bool:
    return all(
        os.path.isfile(os.path.join(BASE_PATH, 'datums', DATUM_MAPPING[datum]['grid']))
        for datum in ('EGM96', 'EGM2008')
    )


requires_grids = pytest.mark.skipif(not _grids_available(), reason='Geoid grid files not present.')

PRODUCT = {
    'security_code': 'U', 'data_edition': 1, 'match_merge_version': 'A', 'producer_code': 'USNGA',
    'compilation_date': '2026-01', 'abs_horiz_acc': 10, 'abs_vert_acc': 5, 'rel_horiz_acc': 'NA',
    'rel_vert_acc': 3, 'digitizing_system': 'TEST',
}


def metadata(level):
    product = dict(PRODUCT, dted_level=level)
    return DtedMetadataSource(None, Profile(path='<test>', product=product, harvest=HarvestConfig()))


def feature_tile():
    """A tile of terrain at 400 to 430 m with a lake, a low spot behind a
    one-post dam, an outlet to a lower lake, a hilltop, ocean, posts a
    centimeter either side of it, a pond of the minimum size and a void block."""
    heights = synthetic_cell(PER_DEGREE, base_cm=40000)
    cm = np.rint(heights.astype(np.float64) * 100).astype(np.int64)
    cm[100:200, 120:220] = 35000      # the lake
    cm[200, 170] = 34960              # a low post behind ...
    cm[201, 170] = 35050              # ... a one-post dam
    cm[250:290, 60:100] = 20000       # a lower lake
    cm[np.arange(200, 250), np.arange(120, 70, -1)] = 34900   # a channel from the lake's corner to it
    cm[10:35, 250:275] = 90000        # a flat hilltop
    cm[:, 0] = 0                      # ocean along the west edge
    cm[:, 1] = -1                     # a centimeter under it: ocean
    cm[:, 2] = 1                      # a centimeter over it: ocean
    cm[150, 3] = -5                   # a post under the sea, enclosed by the land east of it
    cm[50:70, 50:70] = 12345          # a pond of exactly the minimum patch size
    heights = (cm / 100).astype(np.float32)
    heights[270:280, 250:260] = np.nan
    return heights


def write_tile(path, heights, lon0=LON0, lat0=LAT0, per_degree=PER_DEGREE, nodata=-32767.0):
    return write_geotiff(path, heights, lattice_geotransform(lon0, lat0, per_degree, per_degree), nodata=nodata)


def convert(src, out, level, *, src_datum='EGM2008', tgt_datum='EGM2008', mask=False, patch=400):
    transform_vertical_datum(
        src, out, src_datum, tgt_datum, True, mask, patch, 'bilinear', None, False,
        dted_metadata=metadata(level), cell=CellGeometry(level, LON0, LAT0),
    )
    return out


@pytest.fixture(scope='module')
def outputs(tmp_path_factory):
    """The feature tile converted to all three levels, with masks."""
    folder = str(tmp_path_factory.mktemp('convert'))
    src = write_tile(os.path.join(folder, 'tile.tif'), feature_tile())
    files = {}
    for level in (2, 1, 0):
        files[level] = convert(src, os.path.join(folder, f'N85E030_{level}.dt{level}'), level, mask=True)
    return folder, src, files


class TestSameDatumConversion:
    """Resampling, water, low spots, rounding and the header, with no geoid grid."""

    def test_header_and_records_validate(self, outputs):
        folder, src, files = outputs
        for level, path in files.items():
            header, issues = validate_file(path, check_data=True)
            assert [str(i) for i in issues if i.severity == 'error'] == []
            assert header['dsi.series'] == f'DTED{level}' and header['dsi.vertical_datum'] == 'E08'
            assert header['uhl.lon_interval'] == {2: '0060', 1: '0180', 0: '1800'}[level]
            assert header['dsi.producer_code'] == 'USNGA   ' and header['acc.rel_horiz_acc'] == 'NA  '
            assert header['dsi.partial_cell'] == '99'
            with open(path, 'rb') as handle:
                raw = handle.read(HEADER_LENGTH)
            assert raw[:3] == b'UHL' and raw[80:83] == b'DSI' and raw[728:731] == b'ACC'

    def test_values_are_the_rounded_resampled_heights(self, outputs):
        folder, src, files = outputs
        values = read_records(files[2]).values
        with gdal.Open(files[2]) as ds:
            assert np.array_equal(ds.GetRasterBand(1).ReadAsArray(), values)
            assert ds.GetGeoTransform() == CellGeometry(2, LON0, LAT0).geotransform
        # Every twelfth row and every second column is a source node.
        heights = feature_tile()
        nodes = values[::12, ::2]
        expected = round_half_away(heights)
        land = ~np.isnan(heights) & (heights > 1)
        land[100:200, 120:220] = False
        land[200, 170] = False
        land[50:70, 50:70] = False
        assert np.array_equal(nodes[land], expected[land].astype(np.int32)), 'a land node was not rounded in place'
        assert np.all(nodes[np.isnan(heights)] == NULL_ELEVATION)

    def test_water_low_spots_and_outlet(self, outputs):
        folder, src, files = outputs
        values = read_records(files[2]).values
        lake = values[1200:2389, 240:439]          # the lake's interior on the cell grid
        assert np.unique(lake).size == 1 and lake[0, 0] == 350
        assert values[2400, 340] == 350, 'the low post behind the dam was not raised'
        assert values[2412, 340] == 351, 'the dam post moved'
        channel = values[np.arange(2400, 3000, 12), np.arange(240, 140, -2)]
        assert np.all(channel == 349), 'the outlet channel was raised'
        assert np.all(values[:, 0] == 0) and np.all(values[:, 2] == 0) and np.all(values[:, 4] == 0)
        assert values[1800, 6] == 0, 'the post under the sea was not raised to the ocean'
        pond = values[600:829, 100:139]
        assert np.unique(pond).size == 1 and pond[0, 0] == 123, 'the pond of the minimum size is not flat'
        hill = values[120:409, 500:549]
        assert np.unique(hill).size == 1 and hill[0, 0] == 900, 'the hilltop is a plateau whatever its verdict'
        mask = read_band(os.path.join(folder, 'N85E030_2_mask.tif'))
        assert mask.shape == values.shape
        assert mask[1800, 340] > 1 and mask[2400, 340] == mask[1800, 340] and mask[1800, 0] == 1
        assert mask[2700, 190] == 0 and mask[200, 520] == 0, "the outlet or the hilltop is in the mask"

    def test_lower_levels_are_the_thinned_dted2(self, outputs):
        folder, src, files = outputs
        v2 = read_records(files[2]).values
        assert np.array_equal(read_records(files[1]).values, v2[::3, ::3])
        assert np.array_equal(read_records(files[0]).values, v2[::30, ::30])
        mask2 = read_band(os.path.join(folder, 'N85E030_2_mask.tif'))
        assert np.array_equal(read_band(os.path.join(folder, 'N85E030_1_mask.tif')), mask2[::3, ::3])
        assert np.array_equal(read_band(os.path.join(folder, 'N85E030_0_mask.tif')), mask2[::30, ::30])
        with gdal.Open(os.path.join(folder, 'N85E030_0_mask.tif')) as ds:
            assert ds.GetGeoTransform() == CellGeometry(0, LON0, LAT0).geotransform

    def test_halves_round_away_from_zero(self, tmp_path):
        heights = synthetic_cell(PER_DEGREE)
        heights[:] = 0.5
        heights[0, 0] = -0.5
        heights[0, 1] = 2.5
        heights[0, 2] = -2.5
        src = write_tile(str(tmp_path / 'halves.tif'), heights)
        out = convert(src, str(tmp_path / 'N85E030.dt2'), 2, patch=10 ** 9)
        values = read_records(out).values
        assert values[0, 2] == 3 and values[0, 4] == -3 and values[0, 0] == -1
        assert values[12, 12] == 1


@requires_grids
class TestWithDatumShift:
    def test_lattice_geoid_equals_exact_arithmetic_and_the_nodes(self):
        cell = CellGeometry(2, LON0, LAT0)
        corrections = geoid_on_cell('EGM96', cell)
        path = os.path.join(BASE_PATH, 'datums', DATUM_MAPPING['EGM96']['grid'])
        with gdal.Open(path) as ds:
            nodes = ds.GetRasterBand(1).ReadAsArray((LON0 + 180) * 60, (90 - LAT0 - 1) * 60, 61, 61)
        assert corrections.shape == (3601, 601)
        assert np.array_equal(corrections[::60, ::10], nodes.astype(np.float64)), 'a node did not keep its value'
        rng = np.random.default_rng(1)
        for r, c in zip(rng.integers(0, 3601, 40), rng.integers(0, 601, 40)):
            py, px = Fraction(int(r), 60), Fraction(int(c), 10)
            y0, x0 = math.floor(py), math.floor(px)
            fy, fx = py - y0, px - x0
            total = Fraction(0)
            for yy, wy in ((y0, 1 - fy), (y0 + 1, fy)):
                for xx, wx in ((x0, 1 - fx), (x0 + 1, fx)):
                    if wy * wx:
                        total += wy * wx * Fraction(float(nodes[yy, xx]))
            assert corrections[r, c] == float(total)

    def test_lattice_and_existing_geoid_agree(self, tmp_path):
        from egmtrans.transform import create_datum_array

        src = write_tile(str(tmp_path / 'tile.tif'), synthetic_cell(PER_DEGREE))
        existing = create_datum_array(src, 'EGM2008', 'bilinear', str(tmp_path), str(tmp_path))
        lattice = geoid_on_cell('EGM2008', CellGeometry(2, LON0, LAT0))
        assert np.abs(existing - lattice[::12, ::2]).max() < 0.0002

    def test_lake_pond_and_steps_after_the_shift(self, tmp_path, log_lines):
        heights = feature_tile()
        heights[40:50, 100:140] = 349.99   # 1 cm under the lake's height, apart from it: its own body
        src = write_tile(str(tmp_path / 'tile.tif'), heights)
        transform._verified_grids.clear()
        out = convert(src, str(tmp_path / 'N85E030.dt2'), 2, tgt_datum='EGM96')
        values = read_records(out).values
        lake = values[1200:2389, 240:439]
        assert np.unique(lake).size == 1
        assert values[2400, 340] == lake[0, 0], 'the raised post differs from the lake'
        assert np.unique(values[480:589, 200:279]).size == 1, 'the body a centimeter under the lake is not flat'
        assert np.unique(values[600:829, 100:139]).size == 1, 'the pond is not flat'
        assert np.all(values[:, 0] == 0) and np.all(values[:, 2] == 0) and np.all(values[:, 4] == 0)
        text = '\n'.join(log_lines)
        assert 'Evaluated the geoid correction of cell N85E030 on the arc-minute lattice' in text
        assert 'Geoid grid us_nga_egm96_1.tif: SHA-256' in text
        assert 'Raised' in text and 'enclosed low spot' in text and '1 left open on the source grid' in text


class TestRefusals:
    def _run(self, src, out, **kwargs):
        options = dict(flatten=True, create_mask=False, min_patch_size=16, algorithm='bilinear', save_log=False,
                       assume_yes=True, dted_metadata=metadata(2))
        options.update(kwargs)
        return process_file(src, out, 'EGM2008', 'EGM2008', **options)

    def test_rasters_that_cannot_become_dted(self, tmp_path, log_lines):
        heights = synthetic_cell(PER_DEGREE)
        rotated = list(lattice_geotransform(LON0, LAT0, PER_DEGREE, PER_DEGREE))
        rotated[2] = 1e-5
        src = write_geotiff(str(tmp_path / 'rotated.tif'), heights, tuple(rotated))
        with pytest.raises(ValueError, match='rotated'):
            source_grid(src)
        off = list(lattice_geotransform(LON0, LAT0, PER_DEGREE, PER_DEGREE))
        off[0] += 0.01 / PER_DEGREE
        src = write_geotiff(str(tmp_path / 'off.tif'), heights, tuple(off))
        with pytest.raises(ValueError, match='off the lattice'):
            source_grid(src)
        src = write_geotiff(str(tmp_path / 'spacing.tif'), heights, (LON0, 0.0031, 0.0, LAT0 + 1, 0.0, -0.0031))
        with pytest.raises(ValueError, match='not a whole number of posts per degree'):
            source_grid(src)
        src = write_geotiff(str(tmp_path / 'scaled.tif'), heights, lattice_geotransform(LON0, LAT0, 300, 300))
        with gdal.Open(src, gdal.GA_Update) as ds:
            ds.GetRasterBand(1).SetScale(0.1)
        with pytest.raises(ValueError, match='scale or offset'):
            source_grid(src)
        projected = str(tmp_path / 'utm.tif')
        ds = gdal.GetDriverByName('GTiff').Create(projected, 50, 50, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000, 30, 0, 9000000, 0, -30))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32633)
        ds.SetProjection(srs.ExportToWkt())
        ds = None
        with pytest.raises(ValueError, match='not in a geographic coordinate system'):
            source_grid(projected)
        assert self._run(projected, str(tmp_path / 'N85E030.dt2')) is False
        assert any('cannot become DTED' in line or 'not in a geographic' in line for line in log_lines)

    def test_no_whole_cell_and_several_cells(self, tmp_path, log_lines):
        heights = synthetic_cell(PER_DEGREE)[:-1, :-1]   # the south row and east column are missing
        src = write_geotiff(str(tmp_path / 'short.tif'), heights, lattice_geotransform(LON0, LAT0, 300, 300))
        assert self._run(src, str(tmp_path / 'N85E030.dt2')) is False
        assert any('no whole one-degree cell' in line for line in log_lines)
        wide = np.concatenate([synthetic_cell(PER_DEGREE), synthetic_cell(PER_DEGREE)[:, 1:]], axis=1)
        src = write_geotiff(str(tmp_path / 'wide.tif'), wide, lattice_geotransform(LON0, LAT0, 300, 300, extent_x=2))
        assert self._run(src, str(tmp_path / 'N85E030.dt2')) is False
        assert any('covers 2 whole cells' in line for line in log_lines)

    def test_level_and_datum_refusals(self, tmp_path, log_lines):
        from tests.conftest import write_dted

        dted = write_dted(str(tmp_path / 'n06e126.dt0'), np.full((121, 121), 5, dtype=np.int16), 126, 6)
        assert self._run(dted, str(tmp_path / 'n06e126.dt2')) is False
        assert any('keeps its level' in line for line in log_lines)
        src = write_tile(str(tmp_path / 'tile.tif'), synthetic_cell(PER_DEGREE))
        assert process_file(src, str(tmp_path / 'N85E030.dt2'), 'EGM2008', 'WGS84', True, False, 16, 'bilinear',
                            save_log=False, dted_metadata=metadata(2)) is False
        assert any('not WGS84' in line for line in log_lines)
        assert self._run(src, str(tmp_path / 'N85E030.dt2'), algorithm='spline') is False
        assert any('requires the bilinear algorithm' in line for line in log_lines)
        assert self._run(src, str(tmp_path / 'N85E030.dt1')) is False, 'the profile is for level 2'
        assert self._run(src, str(tmp_path / 'N85E030.dt2'), dted_metadata=None) is False
        assert any('needs --dted-profile and/or --dted-index' in line for line in log_lines)
        # A cell can only be named for a conversion.
        assert self._run(src, str(tmp_path / 'copy.tif'), dted_cell=CellGeometry(2, LON0, LAT0)) is False
        assert not any(name.endswith(('.dt2', '.dt1')) for name in os.listdir(tmp_path))

    def test_cell_is_found_from_the_raster_or_named(self, tmp_path):
        src = write_tile(str(tmp_path / 'tile.tif'), synthetic_cell(PER_DEGREE))
        out = str(tmp_path / 'N85E030.dt2')
        assert self._run(src, out) is True
        assert read_header(out).cell_id == 'N85E030'
        wrong = CellGeometry(2, LON0 + 1, LAT0)
        assert self._run(src, str(tmp_path / 'other.dt2'), dted_cell=wrong) is False, 'the raster lacks that cell'


class TestFailures:
    def test_a_failed_write_leaves_nothing(self, tmp_path, monkeypatch):
        from egmtrans import io as egm_io

        src = write_tile(str(tmp_path / 'tile.tif'), synthetic_cell(PER_DEGREE))
        out = str(tmp_path / 'N85E030.dt2')

        def broken(path, cell, posts):
            raise RuntimeError('verification failed on purpose')

        monkeypatch.setattr(egm_io, '_verify_dted', broken)
        with pytest.raises(RuntimeError, match='on purpose'):
            convert(src, out, 2, mask=True)
        assert sorted(os.listdir(tmp_path)) == ['tile.tif'], 'the output, a mask or a temp folder was left'

    def test_a_header_that_cannot_be_completed_leaves_nothing(self, tmp_path):
        src = write_tile(str(tmp_path / 'tile.tif'), synthetic_cell(PER_DEGREE))
        out = str(tmp_path / 'N85E030.dt2')
        with pytest.raises(ValueError, match='security_code is required'):
            transform_vertical_datum(src, out, 'EGM2008', 'EGM2008', True, True, 16, 'bilinear', None, False,
                                     dted_metadata=None, cell=CellGeometry(2, LON0, LAT0))
        assert sorted(os.listdir(tmp_path)) == ['tile.tif']

    def test_a_cell_with_no_valid_post_is_an_error(self, tmp_path):
        heights = np.full((PER_DEGREE + 1, PER_DEGREE + 1), np.nan, dtype=np.float32)
        src = write_tile(str(tmp_path / 'void.tif'), heights)
        with pytest.raises(ValueError, match='no valid post'):
            load_input(src, str(tmp_path), cell=CellGeometry(2, LON0, LAT0))

    def test_arc_mode_never_asks_arcpy_for_geometry(self, tmp_path):
        class Stub:
            def Raster(self, *args):
                raise AssertionError('arcpy.Raster was called for a conversion')

            def __getattr__(self, name):
                raise AssertionError(f'arcpy.{name} was used for a conversion')

        src = write_tile(str(tmp_path / 'tile.tif'), feature_tile())
        out = str(tmp_path / 'N85E030.dt2')
        _state.set_arc_mode(True)
        _state.set_arcpy(Stub())
        try:
            convert(src, out, 2)
        finally:
            _state.set_arc_mode(False)
            _state.set_arcpy(None)
        assert not [i for i in validate_file(out, check_data=True)[1] if i.severity == 'error']
        assert read_records(out).values[1800, 340] == 350
