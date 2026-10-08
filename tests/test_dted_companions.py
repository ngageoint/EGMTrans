"""The DTED0 companion files of MIL-PRF-89020B 3.9.3 (.avg, .min, .max):
their statistics against NumPy, and their writing beside a DTED0 cell."""

import os

import numpy as np
import pytest
from osgeo import gdal

from egmtrans.dted.companions import COMPANION_EXTENSIONS, HALF_WINDOW, STEP, dted0_companions
from egmtrans.dted.header import CellGeometry, read_header
from egmtrans.dted.records import read_records
from egmtrans.dted.schema import NULL_ELEVATION
from egmtrans.transform import transform_vertical_datum
from tests.test_dted_create import LAT0, LON0, feature_tile, metadata, write_tile


def _round_half_away(value):
    return int(np.floor(value + 0.5)) if value >= 0 else int(np.ceil(value - 0.5))


def _window_stats(level1, i, j):
    rows1, cols1 = level1.shape
    r0, r1 = max(STEP * i - HALF_WINDOW, 0), min(STEP * i + HALF_WINDOW, rows1 - 1)
    c0, c1 = max(STEP * j - HALF_WINDOW, 0), min(STEP * j + HALF_WINDOW, cols1 - 1)
    window = level1[r0:r1 + 1, c0:c1 + 1]
    valid = window[~np.isnan(window)]
    if valid.size == 0:
        return NULL_ELEVATION, NULL_ELEVATION, NULL_ELEVATION
    return _round_half_away(valid.mean()), int(valid.min()), int(valid.max())


def test_companions_match_a_plain_window_computation():
    rng = np.random.default_rng(11)
    level1 = rng.integers(-40, 500, size=(1201, 201)).astype(np.float64)  # a zone V cell: 21 DTED0 columns
    level1[0:40, 0:40] = np.nan  # a void corner: the first DTED0 posts have no valid neighbor
    level1[600:603, 100:103] = np.nan  # a small hole
    companions = dted0_companions(level1)
    assert set(companions) == {'avg', 'min', 'max'}
    assert companions['avg'].shape == (121, 21) and companions['avg'].dtype == np.int32
    for i, j in [(0, 0), (0, 3), (3, 0), (4, 4), (60, 10), (120, 20), (120, 0), (0, 20), (61, 11), (59, 9)]:
        avg, low, high = _window_stats(level1, i, j)
        got = (companions['avg'][i, j], companions['min'][i, j], companions['max'][i, j])
        assert got == (avg, low, high), (i, j)
    assert companions['avg'][0, 0] == NULL_ELEVATION and companions['min'][0, 0] == NULL_ELEVATION
    assert companions['avg'][1, 1] == NULL_ELEVATION, '11 x 11 posts around post (10, 10) are void too'
    assert companions['avg'][0, 4] != NULL_ELEVATION, 'post (0, 40) has valid neighbors east of the void'
    # An edge window reaches only the posts inside the cell: 6 x 11 posts.
    assert companions['max'][0, 10] == int(np.nanmax(level1[0:6, 95:106]))
    # A void accepted as -32767 is a void.
    level1[np.isnan(level1)] = NULL_ELEVATION
    assert np.array_equal(dted0_companions(level1)['avg'], companions['avg'])
    with pytest.raises(ValueError, match='does not thin'):
        dted0_companions(level1[:-1])


def test_a_dted0_conversion_writes_verified_companions_beside_the_cell(tmp_path, log_lines):
    src = write_tile(str(tmp_path / 'tile.tif'), feature_tile())
    dt1 = str(tmp_path / 'N85E030_level1.dt1')
    dt0 = str(tmp_path / 'N85E030.dt0')
    for level, out in ((1, dt1), (0, dt0)):
        transform_vertical_datum(
            src, out, 'EGM96', 'EGM96', True, False, 400, 'bilinear', None, False,
            dted_metadata=metadata(level), cell=CellGeometry(level, LON0, LAT0),
        )
    companions = {ext: f'{dt0[:-4]}{ext}' for ext in COMPANION_EXTENSIONS}
    assert all(os.path.isfile(path) for path in companions.values())
    assert not any(os.path.exists(f'{dt1[:-4]}{ext}') for ext in COMPANION_EXTENSIONS), 'DTED1 has no companions'
    header0 = read_header(dt0)
    level1 = read_records(dt1).values.astype(np.float64)
    level1[level1 == NULL_ELEVATION] = np.nan
    expected = dted0_companions(level1)
    for ext, path in companions.items():
        assert read_header(path) == header0, f'{ext} carries the cell header verbatim'
        decoded = read_records(path)
        assert decoded.framing_ok and decoded.bad_checksums == 0
        assert np.array_equal(decoded.values, expected[ext[1:]]), ext
        with gdal.Open(path) as ds:
            assert ds.GetDriver().ShortName == 'DTED' and ds.ReadAsArray().shape == (121, 21)
        assert any(f'Wrote {path}' in line for line in log_lines)
    posts = read_records(dt0).values
    valid = posts != NULL_ELEVATION
    assert np.all(expected['min'][valid] <= posts[valid]) and np.all(posts[valid] <= expected['max'][valid])
