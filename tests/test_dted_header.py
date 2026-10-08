"""The header codec: exact round trips, field formatting, DMS and dates, cell
geometry, level detection, subregions, and the file writer's guard."""

import datetime as dt
import os

import numpy as np
import pytest

from egmtrans.dted.header import (
    AccSubregion,
    DtedHeader,
    cell_geometry,
    cell_geometry_of,
    cell_id,
    encode_header,
    format_dms,
    format_field,
    new_header,
    parse_cell_id,
    parse_dms,
    parse_header,
    read_header,
    to_yymm,
    write_header,
    yymm_to_iso,
)
from egmtrans.dted.schema import HEADER_LENGTH, field
from egmtrans.dted.validate import validate_header
from tests.conftest import write_dted

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')


@pytest.fixture
def srtm_bytes():
    with open(FIXTURE, 'rb') as handle:
        return handle.read()


@pytest.fixture
def srtm(srtm_bytes):
    return parse_header(srtm_bytes)


def test_round_trip_is_exact(srtm_bytes):
    header = parse_header(srtm_bytes)
    assert encode_header(header) == srtm_bytes
    assert len(srtm_bytes) == HEADER_LENGTH


def test_srtm_values(srtm):
    assert srtm['uhl.sentinel'] == 'UHL' and srtm['dsi.sentinel'] == 'DSI' and srtm['acc.sentinel'] == 'ACC'
    assert srtm.origin == (8.0, 3.0)
    assert srtm.cell_id == 'N03E008'
    assert srtm['uhl.security_code'] == 'U  ' and srtm['dsi.security_code'] == 'U'
    assert srtm.accuracy('acc.abs_horiz_acc') == 12
    assert srtm.accuracy('acc.abs_vert_acc') == 6 == srtm.accuracy('uhl.abs_vert_acc')
    assert srtm.accuracy('acc.rel_horiz_acc') is None and srtm['acc.rel_horiz_acc'] == 'NA  '
    assert srtm.accuracy('acc.rel_vert_acc') == 8
    assert srtm.stripped('dsi.series') == 'DTED2'
    assert srtm.stripped('dsi.producer_code') == 'USCNIMA'
    assert srtm['dsi.vertical_datum'] == 'E96' and srtm['dsi.horizontal_datum'] == 'WGS84'
    assert srtm['dsi.partial_cell'] == '99'
    assert srtm.stripped('dsi.free_text') == 'Voids have not been filled or interpolated'
    assert srtm['acc.nima_reserved'] == 'X'
    assert srtm.outline_count() == 0 and srtm.subregions() == []
    detection = srtm.detect_level('.dt2')
    assert detection.consensus == 2 and not detection.conflict


def test_raw_values_keep_every_byte():
    header = DtedHeader()
    header.set_raw('uhl.reserved', '\x00' + ' ' * 23)
    assert encode_header(header)[56] == 0
    assert parse_header(encode_header(header))['uhl.reserved'] == '\x00' + ' ' * 23
    with pytest.raises(ValueError):
        header.set_raw('uhl.reserved', 'short')


def test_format_field_accuracies_and_justification():
    acc = field('acc.abs_horiz_acc')
    assert format_field(acc, 12) == '0012'
    assert format_field(acc, None) == 'NA  '
    assert format_field(acc, 'NA') == 'NA  '
    assert format_field(acc, '  NA') == 'NA  '
    assert format_field(acc, '7') == '0007'
    with pytest.raises(ValueError):
        format_field(acc, 10000)
    assert format_field(field('uhl.lon_lines'), 3601) == '3601'
    assert format_field(field('uhl.lon_lines'), '601') == '0601'
    assert format_field(field('dsi.data_edition'), 1) == '01'
    assert format_field(field('dsi.producer_code'), 'USNGA') == 'USNGA   '
    assert format_field(field('uhl.security_code'), 'U') == 'U  '
    assert format_field(field('dsi.partial_cell'), 7) == '07'
    assert format_field(field('uhl.multiple_accuracy'), True) == '1'
    with pytest.raises(ValueError):
        format_field(field('dsi.producer_code'), 'TOOLONGPRODUCER')
    with pytest.raises(ValueError):
        format_field(field('dsi.free_text'), 'café')


def test_format_field_dates():
    date = field('dsi.compilation_date')
    assert format_field(date, dt.date(2024, 7, 15)) == '2407'
    assert format_field(date, '2024-07') == '2407'
    assert format_field(date, '2024-07-15') == '2407'
    assert format_field(date, '2407') == '2407'
    assert format_field(date, None) == '0000'
    assert to_yymm(dt.datetime(1996, 5, 1, 12)) == '9605'
    with pytest.raises(ValueError):
        to_yymm('July 2024')
    assert yymm_to_iso('2407') == '2024-07'
    assert yymm_to_iso('9605') == '1996-05'
    assert yymm_to_iso('0000') is None
    assert yymm_to_iso('2413') is None
    assert yymm_to_iso('24x7') is None


@pytest.mark.parametrize(
    'degrees, kind, text',
    [(8, 'uhl_lon', '0080000E'), (-6, 'uhl_lat', '0060000S'), (3, 'dsi_lat', '030000.0N'),
     (-30, 'dsi_lon', '0300000.0W'), (3.5, 'dsi_lat', '033000.0N'), (-0.5, 'corner_lon', '0003000W'),
     (51, 'corner_lat', '510000N'), (179.999972, 'uhl_lon', '1800000E'), (12.9998, 'corner_lon', '0125959E')],
)
def test_format_dms(degrees, kind, text):
    assert format_dms(degrees, kind) == text


def test_parse_dms_round_trips_and_rejects_garbage():
    assert parse_dms('0080000E', 'uhl_lon') == 8.0
    assert parse_dms('0060000S', 'uhl_lat') == -6.0
    assert parse_dms('033000.0N', 'dsi_lat') == pytest.approx(3.5)
    assert parse_dms('0300000.0W', 'dsi_lon') == -30.0
    assert parse_dms('0003000W', 'corner_lon') == -0.5
    assert parse_dms('008000E', 'uhl_lon') is None
    assert parse_dms('0080000X', 'uhl_lon') is None
    assert parse_dms('\x00080000E', 'uhl_lon') is None


def test_cell_ids():
    assert cell_id(-1, 50) == 'N50W001'
    assert cell_id(30, -6) == 'S06E030'
    assert cell_id(0, 0) == 'N00E000'
    assert parse_cell_id('s06e030') == (30, -6)
    assert parse_cell_id(' N50W001 ') == (-1, 50)
    for bad in ('N3E8', 'N50W181', 'N91E000', '', 'X_N38E045'):
        with pytest.raises(ValueError):
            parse_cell_id(bad)


@pytest.mark.parametrize(
    'lon0, lat0, level, lon_interval, lon_lines, lat_points',
    [(8, 3, 2, 10, 3601, 3601), (-1, 50, 2, 20, 1801, 3601), (7, 63, 2, 20, 1801, 3601),
     (38, 70, 2, 30, 1201, 3601), (38, 75, 2, 40, 901, 3601), (38, 80, 2, 60, 601, 3601),
     (38, -51, 2, 20, 1801, 3601), (38, -50, 2, 10, 3601, 3601),
     (8, 3, 1, 30, 1201, 1201), (8, 55, 1, 60, 601, 1201), (8, 3, 0, 300, 121, 121), (8, 85, 0, 1800, 21, 121)],
)
def test_cell_geometry(lon0, lat0, level, lon_interval, lon_lines, lat_points):
    cell = cell_geometry(lon0, lat0, level)
    assert cell.lon_interval_tenths == lon_interval
    assert cell.lon_lines == lon_lines
    assert cell.lat_points == lat_points
    values = cell.header_values()
    assert values['uhl.lon_interval'] == f'{lon_interval:04d}' == values['dsi.lon_interval']
    assert values['uhl.lon_lines'] == f'{lon_lines:04d}' == values['dsi.lon_lines']
    assert values['dsi.series'] == f'DTED{level}'


def test_cell_geometry_corners_and_origin():
    values = cell_geometry(-1, 50, 2).header_values()
    assert values['uhl.origin_lon'] == '0010000W' and values['uhl.origin_lat'] == '0500000N'
    assert values['dsi.origin_lat'] == '500000.0N' and values['dsi.origin_lon'] == '0010000.0W'
    assert (values['dsi.sw_lat'], values['dsi.sw_lon']) == ('500000N', '0010000W')
    assert (values['dsi.nw_lat'], values['dsi.nw_lon']) == ('510000N', '0010000W')
    assert (values['dsi.ne_lat'], values['dsi.ne_lon']) == ('510000N', '0000000E')
    assert (values['dsi.se_lat'], values['dsi.se_lon']) == ('500000N', '0000000E')
    assert values['dsi.orientation'] == '0000000.0'
    with pytest.raises(ValueError):
        cell_geometry(180, 0, 2)
    with pytest.raises(ValueError):
        cell_geometry(0, 0, 3)


def test_new_header_needs_only_the_producer_fields():
    header = new_header(cell_geometry(8, 3, 2))
    assert encode_header(header)[:4] == b'UHL1'
    assert header['dsi.product_spec'] == 'PRF89020B' and header['dsi.product_spec_date'] == '0005'
    assert header['acc.abs_horiz_acc'] == 'NA  ' and header['uhl.abs_vert_acc'] == 'NA  '
    assert header['dsi.maintenance_date'] == '0000' and header['acc.outline_flag'] == '00'
    errors = {issue.key for issue in validate_header(header) if issue.severity == 'error'}
    assert errors == {'security_code', 'data_edition', 'match_merge_version', 'vertical_datum'}


def test_detect_level_from_a_written_file(tmp_dir):
    path = write_dted(os.path.join(tmp_dir, 'n06e126.dt0'), np.zeros((121, 121), dtype=np.int16), 126, 6)
    header = read_header(path)
    detection = header.detect_level('.dt0')
    assert detection.consensus == 0 and detection.level == 0
    assert cell_geometry_of(header, '.dt0').cell_id == 'N06E126'
    # A DTED2 name on a level 0 grid: the grid wins, and the conflict is flagged.
    conflict = header.detect_level('.dt2')
    assert conflict.conflict and conflict.consensus is None and conflict.level == 0
    header.set_raw('dsi.series', '     ')
    assert header.detect_level().level == 0


def test_subregions_round_trip():
    header = new_header(cell_geometry(8, 3, 2))
    subregions = []
    for i in range(9):
        outline = [(3.0 + 0.1 * i, 8.0), (3.1 + 0.1 * i, 8.0), (3.1 + 0.1 * i, 9.0), (3.0 + 0.1 * i, 9.0)]
        outline = outline + [(3.05 + 0.1 * i, 8.0 + 0.1 * k) for k in range(10)]
        subregions.append(AccSubregion.from_values(10 + i, 5 + i, None, 'NA', outline[:14]))
    header.set_subregions(subregions)
    assert header['acc.outline_flag'] == '09' and header['uhl.multiple_accuracy'] == '1'
    again = parse_header(encode_header(header))
    decoded = again.subregions()
    assert len(decoded) == 9
    assert decoded[3].accuracies() == {
        'abs_horiz_acc': 13, 'abs_vert_acc': 8, 'rel_horiz_acc': None, 'rel_vert_acc': None,
    }
    assert decoded[3].coord_count == '14'
    assert decoded[0].decoded_outline()[0] == (pytest.approx(3.0), pytest.approx(8.0))
    assert again['acc.subregions'][:4] == '0010'
    header.set_subregions([])
    assert header['acc.outline_flag'] == '00' and header['uhl.multiple_accuracy'] == '0'
    assert header['acc.subregions'].strip() == ''
    with pytest.raises(ValueError):
        header.set_subregions(subregions[:1])
    with pytest.raises(ValueError):
        AccSubregion.from_values(1, 1, 1, 1, [(3.0, 8.0), (4.0, 8.0)])


def test_write_header_guards_the_data_sentinel(tmp_dir, srtm_bytes):
    path = os.path.join(tmp_dir, 'n06e126.dt0')
    write_dted(path, np.full((121, 121), 7, dtype=np.int16), 126, 6)
    header = read_header(path)
    header.set('dsi.producer_code', 'USTEST')
    write_header(path, header)
    assert read_header(path)['dsi.producer_code'] == 'USTEST  '
    with open(path, 'rb') as handle:
        assert handle.read()[HEADER_LENGTH] == 0xAA

    not_dted = os.path.join(tmp_dir, 'not_dted.dt0')
    with open(not_dted, 'wb') as handle:
        handle.write(srtm_bytes + b'\x00' * 100)
    with pytest.raises(ValueError, match='refusing'):
        write_header(not_dted, header)
    short = os.path.join(tmp_dir, 'short.dt0')
    with open(short, 'wb') as handle:
        handle.write(b'UHL1' * 10)
    with pytest.raises(ValueError, match='too short'):
        read_header(short)


def test_dates_outside_the_readers_century_are_refused():
    assert to_yymm('1980-01') == '8001' and to_yymm('2079-12') == '7912'
    assert yymm_to_iso('8001') == '1980-01' and yymm_to_iso('7912') == '2079-12'
    for value in ('1975-06', '2080-01', dt.date(2207, 1, 1), '1899-12-31'):
        with pytest.raises(ValueError, match='1980-2079'):
            to_yymm(value)
    assert yymm_to_iso('7506') == '2075-06', 'the reader is unchanged: it assumes the century'


def test_the_dsi_unique_reference_is_zero_filled_by_default():
    header = new_header(cell_geometry(6, 49, 2))
    assert header['dsi.unique_ref'] == '0' * 15 and header['uhl.unique_ref'] == ' ' * 12
    assert not any(issue.key == 'unique_ref' for issue in validate_header(header))
