"""The validator: a conforming header is clean, and every rule fires on the
corruption it is for."""

import os

import numpy as np
import pytest

from egmtrans.dted.header import AccSubregion, cell_geometry, new_header, parse_header, read_header, write_header
from egmtrans.dted.validate import count, validate_file, validate_header, validate_records
from tests.conftest import write_dted

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')


@pytest.fixture
def srtm():
    with open(FIXTURE, 'rb') as handle:
        return parse_header(handle.read())


def errors(issues):
    return {issue.key for issue in issues if issue.severity == 'error'}


def warnings(issues):
    return {issue.key for issue in issues if issue.severity == 'warning'}


def test_srtm_header_is_clean(srtm):
    issues = validate_header(srtm, extension='.dt2', file_size=25_981_042)
    assert errors(issues) == set() and warnings(issues) == set()


def test_file_size_rule(srtm):
    assert 'size' in errors(validate_header(srtm, extension='.dt2', file_size=25_981_041))


def test_sentinels_and_fixed_values(srtm):
    srtm.set_raw('uhl.sentinel', 'UHX')
    srtm.set_raw('uhl.fixed', '2')
    srtm.set_raw('acc.sentinel', 'acc')
    assert {'sentinel', 'fixed'} <= errors(validate_header(srtm))


def test_non_printable_bytes_are_warnings_read_as_blanks(srtm):
    """GDAL-written headers carry NUL bytes where the specification blank-fills; readers
    take them as blanks, so the report says what they are and goes on checking the value."""
    srtm.set_raw('dsi.security_control', '\x00 ')
    srtm.set_raw('uhl.reserved', '\x00' + ' ' * 23)
    srtm.set_raw('acc.subregions', '\x00' + ' ' * 2555)
    srtm.set_raw('acc.abs_horiz_acc', 'NA\x00 ')
    issues = validate_header(srtm)
    assert {'security_control', 'reserved', 'subregions', 'abs_horiz_acc'} <= warnings(issues)
    assert errors(issues) == set(), 'NA with a NUL decodes as NA once the byte is read as a blank'
    message = next(issue.message for issue in issues if issue.key == 'security_control')
    assert message.startswith('1 byte(s) that are not printable characters (NUL at character 1), read as blanks: ')
    assert 'the elevations are not affected' in message
    srtm.set_raw('dsi.data_edition', '\x00\x00')
    assert 'data_edition' in errors(validate_header(srtm)), 'a value that is blank once read stays wrong'


def test_na_justification(srtm):
    srtm.set_raw('acc.rel_horiz_acc', '  NA')
    issues = validate_header(srtm)
    assert 'rel_horiz_acc' in warnings(issues) and 'rel_horiz_acc' not in errors(issues)
    srtm.set_raw('acc.rel_horiz_acc', 'N/A ')
    assert 'rel_horiz_acc' in errors(validate_header(srtm))


def test_geometry_rules(srtm):
    srtm.set_raw('uhl.lon_interval', '0020')
    assert 'lon_interval' in errors(validate_header(srtm))
    srtm.set_raw('uhl.lon_interval', '0010')
    srtm.set_raw('dsi.lon_interval', '0020')
    assert 'intervals' in errors(validate_header(srtm))
    srtm.set_raw('dsi.lon_interval', '0010')
    srtm.set_raw('uhl.lon_lines', '1801')
    assert 'lon_lines' in errors(validate_header(srtm))
    srtm.set_raw('uhl.lon_lines', '3601')
    srtm.set_raw('dsi.ne_lon', '0100000E')
    assert 'ne_corner' in errors(validate_header(srtm))
    srtm.set_raw('dsi.ne_lon', '0090000E')
    srtm.set_raw('dsi.origin_lat', '030000.0S')
    assert 'origin' in errors(validate_header(srtm))
    srtm.set_raw('dsi.origin_lat', '030000.0N')
    srtm.set_raw('uhl.origin_lat', '0030030N')
    assert 'origin' in errors(validate_header(srtm))


def test_level_rules(srtm):
    srtm.set_raw('dsi.series', 'DTED1')
    assert 'level' in errors(validate_header(srtm, extension='.dt2'))
    srtm.set_raw('dsi.series', 'DTED2')
    assert 'level' in errors(validate_header(srtm, extension='.dt1'))
    srtm.set_raw('uhl.lat_interval', '0015')
    # The series and the point count still say level 2, so only the interval is wrong.
    assert 'lat_interval' in errors(validate_header(srtm)) and 'level' not in errors(validate_header(srtm))
    srtm.set_raw('uhl.lat_points', '0121')
    srtm.set_raw('dsi.series', '     ')
    assert 'level' not in errors(validate_header(srtm))  # 121 points still say level 0
    srtm.set_raw('uhl.lat_points', '0100')
    assert 'level' in errors(validate_header(srtm))


def test_security_accuracy_and_flags(srtm):
    srtm.set_raw('dsi.security_code', 'R')
    assert 'security_code' in errors(validate_header(srtm))
    srtm.set_raw('dsi.security_code', 'U')
    srtm.set_raw('uhl.security_code', ' U ')
    assert 'security_code' in errors(validate_header(srtm))
    srtm.set_raw('uhl.security_code', 'U  ')
    srtm.set_raw('uhl.abs_vert_acc', '0007')
    assert 'abs_vert_acc' in errors(validate_header(srtm))
    srtm.set_raw('uhl.abs_vert_acc', '0006')
    srtm.set_raw('uhl.multiple_accuracy', '1')
    assert 'multiple_accuracy' in errors(validate_header(srtm))
    srtm.set_raw('uhl.multiple_accuracy', '0')
    srtm.set_raw('acc.outline_flag', '01')
    assert 'outline_flag' in errors(validate_header(srtm))


def test_codes_dates_and_identifiers(srtm):
    srtm.set_raw('dsi.data_edition', '00')
    srtm.set_raw('dsi.match_merge_version', 'a')
    srtm.set_raw('dsi.maintenance_date', '2413')
    srtm.set_raw('dsi.maintenance_code', 'ABCD')
    srtm.set_raw('dsi.vertical_datum', 'EGM')
    srtm.set_raw('dsi.horizontal_datum', 'NAD83')
    srtm.set_raw('dsi.partial_cell', '9 ')
    assert {'data_edition', 'match_merge_version', 'maintenance_date', 'maintenance_code', 'vertical_datum',
            'horizontal_datum', 'partial_cell'} <= errors(validate_header(srtm))


def test_warnings_for_deviations(srtm):
    srtm.set_raw('dsi.vertical_datum', 'E08')
    srtm.set_raw('dsi.producer_code', '1NGA    ')
    srtm.set_raw('dsi.product_spec', 'PRF89020A')
    srtm.set_raw('dsi.compilation_date', '0000')
    srtm.set_raw('dsi.orientation', '0000000.1')
    issues = validate_header(srtm)
    assert {'vertical_datum', 'producer_code', 'product_spec', 'compilation_date', 'orientation'} <= warnings(issues)
    assert errors(issues) == set()


def test_blank_vertical_datum_and_reserved_text(srtm):
    srtm.set_raw('dsi.vertical_datum', '   ')
    srtm.set_raw('dsi.reserved_1', 'hello'.ljust(26))
    issues = validate_header(srtm)
    assert 'vertical_datum' in errors(issues)
    assert any(issue.severity == 'info' and issue.key == 'reserved_1' for issue in issues)


def _square(lat0, lon0, lat1, lon1):
    return [(lat0, lon0), (lat1, lon0), (lat1, lon1), (lat0, lon1)]


def test_subregion_rules():
    header = new_header(cell_geometry(8, 3, 2))
    header.set('dsi.security_code', 'U')
    header.set('uhl.security_code', 'U')
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.producer_code', 'USTEST')
    header.set('dsi.data_edition', 1)
    header.set('dsi.match_merge_version', 'A')
    header.set('dsi.vertical_datum', 'E96')
    header.set('acc.abs_horiz_acc', 20)
    header.set('acc.abs_vert_acc', 10)
    header.set_raw('uhl.abs_vert_acc', '0010')
    header.set_subregions([
        AccSubregion.from_values(12, 6, None, 8, _square(3.0, 8.0, 4.0, 8.5)),
        AccSubregion.from_values(20, 10, None, 12, _square(3.0, 8.5, 4.0, 9.0)),
    ])
    issues = validate_header(header)
    assert errors(issues) == set() and warnings(issues) == set()

    header.set('acc.abs_vert_acc', 6)
    header.set_raw('uhl.abs_vert_acc', '0006')
    assert any(issue.severity == 'info' and 'worst' in issue.message for issue in validate_header(header))

    counterclockwise = AccSubregion.from_values(1, 1, 1, 1, [(3.0, 8.0), (3.0, 9.0), (4.0, 9.0), (4.0, 8.0)])
    outside = AccSubregion.from_values(1, 1, 1, 1, _square(3.0, 8.0, 5.0, 9.0))
    header.set_subregions([counterclockwise, outside])
    issues = validate_header(header)
    assert any('counterclockwise' in issue.message for issue in issues)
    assert any('outside the cell' in issue.message for issue in issues)

    header.set_raw('acc.outline_flag', '03')
    assert 'subregions' in errors(validate_header(header))
    block = header['acc.subregions']
    header.set_raw('acc.subregions', block[:16] + '15' + block[18:])
    header.set_raw('acc.outline_flag', '02')
    assert any('coordinate count' in issue.message for issue in validate_header(header))


def test_records_of_a_written_file(tmp_dir):
    array = np.arange(121 * 121, dtype=np.int16).reshape(121, 121) % 500 - 50
    array[:10, :10] = -32767
    path = write_dted(os.path.join(tmp_dir, 'n06e126.dt0'), array, 126, 6)
    header, issues = validate_file(path, check_data=True)
    assert not {issue.key for issue in issues if issue.severity == 'error' and issue.record == 'DATA'}
    info = [issue for issue in issues if issue.record == 'DATA' and issue.severity == 'info']
    assert info and '-50 to 449 m' in info[0].message and '100 null' in info[0].message
    # GDAL computed the partial cell indicator from the voids, so no mismatch.
    assert 'partial_cell' not in warnings(issues)

    header.set_raw('dsi.partial_cell', '00')
    write_header(path, header)
    assert 'partial_cell' in warnings(validate_records(path, header))

    offset = 3428 + (12 + 2 * 121) + 8 + 20  # inside the second record's elevations
    with open(path, 'r+b') as handle:
        handle.seek(offset)
        byte = handle.read(1)[0]
        handle.seek(offset)
        handle.write(bytes([byte ^ 0x55]))
    bad = validate_records(path, read_header(path))
    assert 'checksum' in errors(bad)
    with open(path, 'r+b') as handle:
        handle.seek(3428)
        handle.write(b'\x00')
    assert 'sentinel' in errors(validate_records(path, read_header(path)))
    with open(path, 'ab') as handle:
        handle.write(b'\x00')
    assert '' in errors(validate_records(path, read_header(path)))
    assert count(validate_records(path, read_header(path)), 'error') == 1
