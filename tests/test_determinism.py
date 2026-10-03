"""The central promise of a conversion: the same version, grids and inputs give
the same bytes, whatever the host, the path, the thread count or Numba."""

import hashlib
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from egmtrans import transform
from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.dted.header import CellGeometry
from egmtrans.dted.profile import HarvestConfig, Profile
from egmtrans.dted.schema import HEADER_LENGTH
from egmtrans.dted.writer import DtedMetadataSource
from egmtrans.transform import transform_vertical_datum
from tests.conftest import lattice_geotransform, synthetic_cell, write_geotiff

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PER_DEGREE = 300
LON0, LAT0 = 30, 85

# Pinned on the reference host: the same-datum conversion of source_tile()
# (resampling, water, low spots, rounding and the header, with no geoid grid)
# and the EGM2008 to EGM96 conversion. Regenerate them with the self-test's
# reference whenever the conversion is meant to change.
PINNED_SAME_DATUM = (
    'a7e77798fe5df0b5912218317b398cda081d81f13e80cd63412cb4c59658a62e',
    'dc3c290867199adea673864d154fc666ae6e60355cb67705f623707882ac2855',
)
PINNED_WITH_SHIFT = (
    '3902ab6c42d4b4a6bbb9cc44121870761d3c832032528dc767b39a82b8c7fd31',
    'a56c15daded8594d18ddbc58ad7a1a7ba7755f94d87d718199d4f30a57ed3859',
)

PRODUCT = {
    'security_code': 'U', 'data_edition': 1, 'match_merge_version': 'A', 'producer_code': 'USNGA',
    'compilation_date': '2026-01', 'abs_horiz_acc': 10, 'abs_vert_acc': 5, 'rel_horiz_acc': 'NA',
    'rel_vert_acc': 3, 'digitizing_system': 'TEST',
}


def _grids_available() -> bool:
    return all(
        os.path.isfile(os.path.join(BASE_PATH, 'datums', DATUM_MAPPING[datum]['grid']))
        for datum in ('EGM96', 'EGM2008')
    )


requires_grids = pytest.mark.skipif(not _grids_available(), reason='Geoid grid files not present.')


def source_tile():
    heights = synthetic_cell(PER_DEGREE, base_cm=40000)
    cm = np.rint(heights.astype(np.float64) * 100).astype(np.int64)
    cm[100:200, 120:220] = 35000
    cm[200, 170] = 34960
    cm[201, 170] = 35050
    cm[:, 0] = 0
    cm[150, 1] = -5
    cm[20:40, 20:40] = 12345
    heights = (cm / 100).astype(np.float32)
    heights[270:280, 250:260] = np.nan
    heights[0, 5] = 0.5       # halves away from zero, either side
    heights[0, 6] = -0.5
    heights[0, 7] = 0.49999
    heights[0, 8] = 2.5
    return heights


def metadata(level=2):
    product = dict(PRODUCT, dted_level=level)
    return DtedMetadataSource(None, Profile(path='<test>', product=product, harvest=HarvestConfig()))


def convert(src, out, level=2, src_datum='EGM2008', tgt_datum='EGM2008'):
    transform_vertical_datum(
        src, out, src_datum, tgt_datum, True, False, 400, 'bilinear', None, False,
        dted_metadata=metadata(level), cell=CellGeometry(level, LON0, LAT0),
    )
    with open(out, 'rb') as handle:
        content = handle.read()
    return hashlib.sha256(content[:HEADER_LENGTH]).hexdigest(), hashlib.sha256(content[HEADER_LENGTH:]).hexdigest()


SUBPROCESS_SCRIPT = textwrap.dedent('''
    import hashlib, os, sys
    if {hide_numba}:
        sys.modules['numba'] = None
    sys.path.insert(0, {root!r})
    sys.path.insert(0, os.path.join({root!r}, 'src'))
    from egmtrans.numba_utils import NUMBA_AVAILABLE
    assert NUMBA_AVAILABLE is not {hide_numba}
    from tests.test_determinism import convert
    header, records = convert({src!r}, {out!r})
    print(header, records)
''')


def _in_subprocess(src, out, *, hide_numba=False, env=None):
    script = SUBPROCESS_SCRIPT.format(hide_numba=hide_numba, root=ROOT, src=src, out=out)
    environment = dict(os.environ)
    environment.update(env or {})
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, env=environment,
                               cwd=os.path.dirname(out))
    assert completed.returncode == 0, completed.stderr[-2000:]
    return tuple(completed.stdout.split()[-2:])


class TestSameBytes:
    @pytest.fixture(scope='class')
    def source(self, tmp_path_factory):
        folder = str(tmp_path_factory.mktemp('determinism'))
        return write_geotiff(os.path.join(folder, 'tile.tif'), source_tile(),
                             lattice_geotransform(LON0, LAT0, PER_DEGREE, PER_DEGREE), nodata=-32767.0)

    def test_two_runs_two_paths_two_working_directories(self, source, tmp_path, monkeypatch):
        (tmp_path / 'a').mkdir()
        first = convert(source, str(tmp_path / 'a' / 'N85E030.dt2'))
        (tmp_path / 'b' / 'deeper').mkdir(parents=True)
        monkeypatch.chdir(tmp_path / 'b')
        second = convert(source, str(tmp_path / 'b' / 'deeper' / 'other_name.dt2'))
        assert first == second

    def test_pinned_hashes(self, source, tmp_path):
        header, records = convert(source, str(tmp_path / 'N85E030.dt2'))
        assert (header, records) == PINNED_SAME_DATUM

    def test_without_numba_in_a_subprocess(self, source, tmp_path):
        expected = convert(source, str(tmp_path / 'ref.dt2'))
        assert _in_subprocess(source, str(tmp_path / 'plain.dt2'), hide_numba=True) == expected

    def test_thread_count_does_not_change_the_bytes(self, source, tmp_path):
        expected = convert(source, str(tmp_path / 'ref.dt2'))
        assert _in_subprocess(source, str(tmp_path / 'one.dt2'), env={'NUMBA_NUM_THREADS': '1'}) == expected
        assert _in_subprocess(source, str(tmp_path / 'two.dt2'), env={'OMP_NUM_THREADS': '2'}) == expected


@requires_grids
class TestWithGrids:
    def test_selftest_matches_the_reference(self):
        from egmtrans.cli_dted import main
        from egmtrans.dted.selftest import REFERENCE, run_selftest

        result = run_selftest()
        assert [item.name for item in result.items] == list(REFERENCE)
        for item in result.items:
            assert item.ok, f'{item.name}: {item.header} {item.records} != {item.expected}'
        assert main(['dted-selftest']) == 0

    def test_grid_checksum_is_verified_before_a_dted_is_created(self, tmp_path, monkeypatch):
        source = write_geotiff(str(tmp_path / 'tile.tif'), source_tile(),
                               lattice_geotransform(LON0, LAT0, PER_DEGREE, PER_DEGREE), nodata=-32767.0)
        transform._verified_grids.clear()
        monkeypatch.setattr(transform, 'verify_checksum', lambda path, expected: False)
        with pytest.raises(ValueError, match='does not match its published checksum'):
            convert(source, str(tmp_path / 'N85E030.dt2'), tgt_datum='EGM96')
        assert not os.path.exists(tmp_path / 'N85E030.dt2')
        transform._verified_grids.clear()
        monkeypatch.undo()
        header, records = convert(source, str(tmp_path / 'N85E030.dt2'), tgt_datum='EGM96')
        assert (header, records) == PINNED_WITH_SHIFT
