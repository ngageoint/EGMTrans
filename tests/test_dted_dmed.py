"""The DMED volume file of MIL-PRF-89020B 3.9.5: the tree it describes, the
16 areas of a cell, the statistics, the 394-byte records and their order,
and the file read back."""

import os

import numpy as np
import pytest

from egmtrans import cli_dted
from egmtrans.dted.dmed import (
    AREA_COUNT,
    RECORD_LENGTH,
    DmedError,
    Rectangle,
    area_slices,
    area_stats,
    build_dmed,
    check_dmed,
    format_cell,
    header_record,
    locate_tree,
    parse_dmed,
    scan_volume,
    write_dmed,
)
from egmtrans.dted.header import CellGeometry, new_header
from egmtrans.dted.records import write_dted_file
from egmtrans.dted.schema import NULL_ELEVATION


def _header(cell, edition=1, version='A'):
    header = new_header(cell)
    header.set('dsi.security_code', 'U')
    header.set_raw('uhl.security_code', 'U  ')
    header.set('dsi.data_edition', edition)
    header.set('dsi.match_merge_version', version)
    header.set('dsi.producer_code', 'USNGA')
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.vertical_datum', 'E96')
    for key in ('acc.abs_horiz_acc', 'acc.abs_vert_acc', 'acc.rel_horiz_acc', 'acc.rel_vert_acc', 'uhl.abs_vert_acc'):
        header.set(key, 5)
    return header


def _values(cell, seed=0, low=-50, high=400):
    rng = np.random.default_rng(seed)
    return rng.integers(low, high, size=(cell.lat_points, cell.lon_lines), dtype=np.int32)


def write_cell(root, lon0, lat0, level=0, values=None, edition=1, version='A', seed=0, name=None):
    """A cell of the delivery under *root*/DTED, written with the codec."""
    cell = CellGeometry(level, lon0, lat0)
    if values is None:
        values = _values(cell, seed)
    lon_dir = f"{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"
    name = name or f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}.dt{level}"
    folder = os.path.join(root, 'DTED', lon_dir)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    write_dted_file(path, _header(cell, edition, version), values)
    return path, values


def _round_half_away(value):
    return int(np.floor(value + 0.5)) if value >= 0 else int(np.ceil(value - 0.5))


def _numpy_stats(values, rows, cols):
    block = values[rows, cols]
    valid = block[block != NULL_ELEVATION].astype(np.float64)
    if valid.size == 0:
        return None
    mean = valid.mean()
    std = np.sqrt(np.mean((valid - mean) ** 2))
    return int(valid.min()), int(valid.max()), _round_half_away(mean), _round_half_away(std)


class TestAreas:
    def test_the_dividing_rows_and_columns_count_in_both_areas(self):
        rows, cols = area_slices(3601, 3601, 1)
        assert (rows.start, rows.stop, cols.start, cols.stop) == (2700, 3601, 0, 901), 'area 1 is the southwest'
        rows, cols = area_slices(3601, 3601, 4)
        assert (rows.start, rows.stop, cols.start, cols.stop) == (0, 901, 0, 901), 'area 4 is the northwest'
        rows, cols = area_slices(3601, 3601, 13)
        assert (rows.start, rows.stop, cols.start, cols.stop) == (2700, 3601, 2700, 3601), 'area 13 is the southeast'
        rows, cols = area_slices(3601, 3601, 16)
        assert (rows.start, rows.stop, cols.start, cols.stop) == (0, 901, 2700, 3601), 'area 16 is the northeast'
        # Row 2700 and column 900 lie on dividing lines: in both neighbors.
        assert area_slices(3601, 3601, 2)[0].stop - 1 == 2700 == area_slices(3601, 3601, 1)[0].start
        assert area_slices(3601, 3601, 5)[1].start == 900 == area_slices(3601, 3601, 1)[1].stop - 1
        # DTED2 zone II: 1801 columns, 450 per area.
        assert area_slices(3601, 1801, 5)[1] == slice(450, 901)
        # DTED1: 1201 by 1201, 300 per area; DTED0 zone I: 121, 30 per area.
        assert area_slices(1201, 1201, 6) == (slice(600, 901), slice(300, 601))
        assert area_slices(121, 121, 16) == (slice(0, 31), slice(90, 121))

    def test_dted0_zone_iv_has_no_shared_column_on_the_quarter_lines(self):
        # 31 longitude lines: the 15' and 45' meridians fall between posts, the 30' one on column 15.
        assert [area_slices(121, 31, k)[1] for k in (1, 5, 9, 13)] == [
            slice(0, 8), slice(8, 16), slice(15, 23), slice(23, 31),
        ]
        with pytest.raises(ValueError):
            area_slices(121, 31, 17)

    def test_statistics_match_numpy_and_voids_are_left_out(self):
        cell = CellGeometry(0, 6, 49)
        values = _values(cell, seed=3, low=-120, high=900)
        values[0:31, 0:31] = NULL_ELEVATION  # the northwest area is all void
        values[60:70, 60:70] = NULL_ELEVATION  # a hole inside other areas
        for area in range(1, AREA_COUNT + 1):
            rows, cols = area_slices(*values.shape, area)
            expected = _numpy_stats(values, rows, cols)
            got = area_stats(values, area)
            if expected is None:
                assert got is None, area
            else:
                assert (got.minimum, got.maximum, got.mean, got.std) == expected, area
        assert area_stats(values, 4) is None

    def test_rounding_is_half_away_from_zero(self):
        cell = CellGeometry(0, 6, 49)
        values = np.full((cell.lat_points, cell.lon_lines), NULL_ELEVATION, dtype=np.int32)
        rows, cols = area_slices(*values.shape, 1)
        block = values[rows, cols]
        block[...] = -2
        block[0, 0] = -3  # mean -2.00104..., rounds to -2
        stats = area_stats(values, 1)
        assert (stats.minimum, stats.maximum, stats.mean) == (-3, -2, -2)
        values[rows, cols] = NULL_ELEVATION
        values[rows.start, cols.start] = 1
        values[rows.start, cols.start + 1] = 2  # mean 1.5 rounds to 2, population std 0.5 rounds to 1
        stats = area_stats(values, 1)
        assert (stats.mean, stats.std) == (2, 1)


class TestRecords:
    def test_header_record_uses_the_far_edges(self):
        assert header_record(Rectangle(30, 36, 20, 32)).startswith('N30N36E020E032')
        assert len(header_record(Rectangle(30, 36, 20, 32))) == RECORD_LENGTH
        assert header_record(Rectangle(-2, 1, -2, 1)).startswith('S02N01W002E001')
        assert header_record(Rectangle(89, 90, 179, 180)).startswith('N89N90E179E180')
        assert header_record(Rectangle(-1, 0, -1, 0)).startswith('S01N00W001E000')

    def test_a_cell_record_is_394_characters_with_the_sign_left_of_the_digits(self, tmp_dir):
        flat = np.full((121, 121), -12, dtype=np.int32)
        path, values = write_cell(tmp_dir, 6, 49, values=flat, edition=7, version='C')
        from egmtrans.dted.dmed import VolumeCell, cell_record

        record = cell_record(VolumeCell(path, 6, 49, 0))
        text = format_cell(record)
        assert len(text) == RECORD_LENGTH
        assert text[:10] == 'N49E00607C'
        assert text[10:34] == '   -12   -12   -12     0'
        assert text[34:58] == text[10:34] and text[370:394] == text[10:34]

    def test_write_parse_and_order(self, tmp_dir, log_lines):
        # Cells at (6,49), (6,50) and (8,49): a 3 x 2 rectangle with three gaps.
        a, va = write_cell(tmp_dir, 6, 49, seed=1)
        b, vb = write_cell(tmp_dir, 6, 50, seed=2, edition=2, version='B')
        c, vc = write_cell(tmp_dir, 8, 49, seed=3)
        # Masks, logs and companions in the tree are not cells.
        open(os.path.join(tmp_dir, 'DTED', 'E006', 'tile_mask.tif'), 'wb').close()
        open(os.path.join(tmp_dir, 'DTED', 'E006', 'N49.avg'), 'wb').close()
        open(os.path.join(tmp_dir, 'delivery_transform.log'), 'w').close()

        result = write_dmed(tmp_dir)
        assert result.path == os.path.join(tmp_dir, 'DMED')
        assert result.rectangle == Rectangle(49, 51, 6, 9)
        assert os.path.getsize(result.path) == (1 + 6) * RECORD_LENGTH == len(result.content)

        parsed = parse_dmed(result.path)
        assert parsed.rectangle == Rectangle(49, 51, 6, 9)
        order = [(r.lon0, r.lat0, r.present) for r in parsed.records]
        assert order == [(6, 49, True), (6, 50, True), (7, 49, False), (7, 50, False), (8, 49, True), (8, 50, False)]
        assert (parsed.records[1].edition, parsed.records[1].version) == ('02', 'B')
        for record, values in ((parsed.records[0], va), (parsed.records[1], vb), (parsed.records[4], vc)):
            for area in range(1, AREA_COUNT + 1):
                rows, cols = area_slices(*values.shape, area)
                expected = _numpy_stats(values, rows, cols)
                got = record.areas[area - 1]
                assert (got.minimum, got.maximum, got.mean, got.std) == expected, (record.cell_id, area)
        with open(result.path, 'rb') as handle:
            raw = handle.read()
        assert raw.isascii() and b'\n' not in raw and raw == raw.upper()
        assert raw[3 * RECORD_LENGTH:3 * RECORD_LENGTH + 7] == b'N49E007'
        assert raw[3 * RECORD_LENGTH + 7:4 * RECORD_LENGTH].strip() == b'', 'an absent cell is coordinates and blanks'

        assert check_dmed(tmp_dir) == []
        with open(result.path, 'r+b') as handle:
            handle.seek(RECORD_LENGTH + 20)
            handle.write(b'#')
        problems = check_dmed(tmp_dir)
        assert problems and 'record 2 differs' in problems[0]

    def test_southern_western_cells_sort_numerically(self, tmp_dir):
        write_cell(tmp_dir, -2, -2, seed=4)
        write_cell(tmp_dir, 0, 0, seed=5)
        root, cells = scan_volume(tmp_dir)
        assert [(c.lon0, c.lat0) for c in cells] == [(-2, -2), (0, 0)]
        rectangle, content = build_dmed(cells)
        assert content[:14] == b'S02N01W002E001'
        parsed = parse_dmed(write_dmed(tmp_dir).path)
        assert [(r.lon0, r.lat0) for r in parsed.records][:4] == [(-2, -2), (-2, -1), (-2, 0), (-1, -2)]
        assert parsed.records[0].present and parsed.records[-1].present and not parsed.records[1].present

    def test_a_dted2_cell_in_zone_v(self, tmp_dir):
        path, values = write_cell(tmp_dir, 30, 85, level=2, seed=6)
        assert values.shape == (3601, 601)
        parsed = parse_dmed(write_dmed(tmp_dir).path)
        rows, cols = area_slices(3601, 601, 16)
        assert (rows, cols) == (slice(0, 901), slice(450, 601))
        expected = _numpy_stats(values, rows, cols)
        got = parsed.records[0].areas[15]
        assert (got.minimum, got.maximum, got.mean, got.std) == expected

    def test_a_dted0_cell_in_zone_iv(self, tmp_dir):
        path, values = write_cell(tmp_dir, 10, 77, seed=7)
        assert values.shape == (121, 31)
        parsed = parse_dmed(write_dmed(tmp_dir).path)
        for area in (1, 5, 9, 13):
            rows, cols = area_slices(121, 31, area)
            expected = _numpy_stats(values, rows, cols)
            got = parsed.records[0].areas[area - 1]
            assert (got.minimum, got.maximum, got.mean, got.std) == expected, area


class TestScan:
    def test_the_dted_folder_itself_and_lower_case_names_are_accepted(self, tmp_dir):
        path, _ = write_cell(tmp_dir, 6, 49)
        lower = os.path.join(tmp_dir, 'DTED', 'e007')
        os.makedirs(lower)
        os.replace(path, os.path.join(lower, 'n49.dt0'))
        os.rmdir(os.path.join(tmp_dir, 'DTED', 'E006'))
        # The header says E006; the lower-case name says E007: refused.
        with pytest.raises(DmedError, match='name says N49E007 but the header says N49E006'):
            scan_volume(tmp_dir)
        os.makedirs(os.path.join(tmp_dir, 'DTED', 'e006'))
        os.replace(os.path.join(lower, 'n49.dt0'), os.path.join(tmp_dir, 'DTED', 'e006', 'n49.dt0'))
        root, cells = scan_volume(os.path.join(tmp_dir, 'DTED'))
        assert root == tmp_dir and [(c.lon0, c.lat0, c.level) for c in cells] == [(6, 49, 0)]
        assert locate_tree(tmp_dir) == (tmp_dir, os.path.join(tmp_dir, 'DTED'))
        result = write_dmed(os.path.join(tmp_dir, 'DTED'))
        assert result.path == os.path.join(tmp_dir, 'DMED')

    def test_refusals(self, tmp_dir):
        empty = os.path.join(tmp_dir, 'empty')
        os.makedirs(empty)
        with pytest.raises(DmedError, match='no DTED folder'):
            scan_volume(empty)
        os.makedirs(os.path.join(empty, 'DTED', 'E006'))
        with pytest.raises(DmedError, match='No DTED cell'):
            scan_volume(empty)
        write_cell(tmp_dir, 6, 49, level=0)
        write_cell(tmp_dir, 7, 49, level=1)
        with pytest.raises(DmedError, match='several levels'):
            scan_volume(tmp_dir)
        with pytest.raises(DmedError, match='not a folder'):
            scan_volume(os.path.join(tmp_dir, 'nowhere'))


def test_the_dmed_subcommand(tmp_dir, capsys):
    write_cell(tmp_dir, 6, 49)
    write_cell(tmp_dir, 6, 50)
    assert cli_dted.main(['dmed', tmp_dir]) == 0
    assert 'Wrote' in capsys.readouterr().err and os.path.isfile(os.path.join(tmp_dir, 'DMED'))
    assert cli_dted.main(['dmed', tmp_dir, '--check']) == 0
    assert 'matches the cells' in capsys.readouterr().err
    other = os.path.join(tmp_dir, 'elsewhere.dmed')
    assert cli_dted.main(['dmed', tmp_dir, '--out', other]) == 0
    assert os.path.getsize(other) == 3 * RECORD_LENGTH
    with open(os.path.join(tmp_dir, 'DMED'), 'r+b') as handle:
        handle.seek(RECORD_LENGTH + 12)
        handle.write(b'0')
    assert cli_dted.main(['dmed', tmp_dir, '--check']) == 1
    assert 'does not match' in capsys.readouterr().err
    assert cli_dted.main(['dmed', os.path.join(tmp_dir, 'nowhere')]) == 2
