"""Tests for egmtrans.dted.records: the elevation record codec."""

import os

import numpy as np
import pytest
from osgeo import gdal

from egmtrans.dted.header import CellGeometry, new_header, read_header
from egmtrans.dted.records import (
    RecordError,
    check_values,
    decode_records,
    encode_records,
    partial_cell_indicator,
    read_edges,
    read_records,
    record_length,
    write_dted_file,
)
from egmtrans.dted.schema import ELEVATION_MAX, ELEVATION_MIN, HEADER_LENGTH, NULL_ELEVATION
from egmtrans.dted.validate import validate_file
from tests.conftest import write_geotiff


def _cell_array(cell: CellGeometry, seed: int = 1) -> np.ndarray:
    """Posts of every sign and a void block, no two alike where that matters."""
    rng = np.random.default_rng(seed)
    array = rng.integers(-400, 3000, size=(cell.lat_points, cell.lon_lines)).astype(np.int16)
    array[:5, :7] = NULL_ELEVATION
    array[10, 10] = ELEVATION_MIN
    array[11, 11] = ELEVATION_MAX
    array[12, 12] = 0
    return array


def _gdal_bytes(cell: CellGeometry, array: np.ndarray, tmp_dir: str) -> bytes:
    """The file GDAL's DTED driver writes for *array* on *cell*."""
    scratch = os.path.join(tmp_dir, f'{cell.cell_id}.src.tif')
    rows, cols = array.shape
    step_lat = 1.0 / (rows - 1)
    step_lon = 1.0 / (cols - 1)
    geotransform = (cell.lon0 - step_lon / 2, step_lon, 0.0, cell.lat0 + 1 + step_lat / 2, 0.0, -step_lat)
    write_geotiff(scratch, array, geotransform, nodata=NULL_ELEVATION)
    path = os.path.join(tmp_dir, f'{cell.cell_id}.dt{cell.level}')
    gdal.Translate(path, scratch, format='DTED')
    with open(path, 'rb') as handle:
        return handle.read()


class TestEncoding:
    def test_record_layout_and_order(self):
        values = np.array([[5, 6], [3, 4], [1, 2]], dtype=np.int16)  # 3 posts per line, 2 lines
        data = encode_records(values)
        assert len(data) == 2 * record_length(3)
        first, second = data[:18], data[18:]
        # Sentinel, block count 0, longitude count 0, latitude count 0, then the
        # west column south to north: 1, 3, 5.
        assert first[:8] == bytes([0xAA, 0, 0, 0, 0, 0, 0, 0])
        assert first[8:14] == bytes([0, 1, 0, 3, 0, 5])
        assert second[:8] == bytes([0xAA, 0, 0, 1, 0, 1, 0, 0])
        assert second[8:14] == bytes([0, 2, 0, 4, 0, 6])
        # The checksum is the sum of the preceding bytes.
        assert int.from_bytes(first[14:], 'big') == sum(first[:14])
        assert int.from_bytes(second[14:], 'big') == 0xAA + 1 + 1 + 2 + 4 + 6

    def test_signed_magnitude_and_void(self):
        values = np.array([[-1], [NULL_ELEVATION], [ELEVATION_MIN]], dtype=np.int32)
        data = encode_records(values)
        posts = data[8:14]  # south to north: -12000, the void, -1
        assert posts[4:6] == bytes([0x80, 0x01]), '-1 is the sign bit plus magnitude 1, never complemented'
        assert posts[2:4] == bytes([0xFF, 0xFF]), 'the void is 0xFFFF'
        assert posts[0:2] == bytes([0x80 | (12000 >> 8), 12000 & 0xFF])

    def test_negative_zero_is_plain_zero(self):
        data = encode_records(np.array([[-0.0, 0.0]], dtype=np.float64))
        assert data[8:10] == b'\x00\x00' and data[8 + 14:10 + 14] == b'\x00\x00'

    def test_floats_must_be_whole_and_finite(self):
        assert encode_records(np.array([[12.0, -7.0]])) == encode_records(np.array([[12, -7]], dtype=np.int16))
        with pytest.raises(RecordError, match='NaN'):
            encode_records(np.array([[1.0, np.nan]]))
        with pytest.raises(RecordError, match='whole'):
            encode_records(np.array([[1.5, 2.0]]))

    def test_out_of_range_is_refused_not_wrapped(self):
        with pytest.raises(RecordError, match='outside'):
            encode_records(np.array([[40000]], dtype=np.int32))
        with pytest.raises(RecordError, match='outside'):
            encode_records(np.array([[ELEVATION_MAX + 1]], dtype=np.int32))
        with pytest.raises(RecordError, match='outside'):
            encode_records(np.array([[ELEVATION_MIN - 1]], dtype=np.int32))
        with pytest.raises(RecordError, match='2-D'):
            encode_records(np.arange(4))
        with pytest.raises(RecordError, match='integers'):
            check_values(np.array([['1']]))
        assert check_values(np.array([[ELEVATION_MIN, ELEVATION_MAX, NULL_ELEVATION]])).dtype == np.int32

    def test_round_trip(self):
        cell = CellGeometry(0, 126, 6)
        array = _cell_array(cell)
        decoded = decode_records(encode_records(array), cell.lon_lines, cell.lat_points)
        assert np.array_equal(decoded.values, array)
        assert decoded.framing_ok and decoded.bad_checksums == 0

    def test_decoder_reports_framing_faults(self):
        cell = CellGeometry(0, 126, 6)
        array = _cell_array(cell)
        data = bytearray(encode_records(array))
        length = record_length(cell.lat_points)
        data[0] = 0x00                   # first sentinel
        data[length + 3] = 7             # second record's block count
        data[2 * length + 5] = 9         # third record's longitude count
        data[3 * length + 7] = 1         # fourth record's latitude count
        data[4 * length + 20] ^= 0x55    # a post of the fifth record
        decoded = decode_records(bytes(data), cell.lon_lines, cell.lat_points)
        # Every tampered record fails its checksum as well.
        assert (decoded.bad_sentinels, decoded.bad_block_counts, decoded.bad_lon_counts,
                decoded.bad_lat_counts, decoded.bad_checksums) == (1, 1, 1, 1, 5)
        assert not decoded.framing_ok
        unverified = decode_records(bytes(data), cell.lon_lines, cell.lat_points, verify_checksums=False)
        assert unverified.bad_checksums is None
        with pytest.raises(RecordError, match='bytes of records'):
            decode_records(bytes(data) + b'\x00', cell.lon_lines, cell.lat_points)


@pytest.mark.parametrize(
    'cell',
    [CellGeometry(0, 126, 6), CellGeometry(1, -1, 50), CellGeometry(2, 7, 63), CellGeometry(2, 30, -60),
     CellGeometry(0, 0, 85), CellGeometry(1, -180, -89)],
    ids=lambda c: f'{c.series}-{c.cell_id}',
)
def test_records_equal_gdal_create_copy(cell, tmp_dir):
    """GDAL's writer and the codec produce the same record block, byte for byte."""
    array = _cell_array(cell)
    expected = _gdal_bytes(cell, array, tmp_dir)
    assert len(expected) == HEADER_LENGTH + cell.lon_lines * record_length(cell.lat_points)
    assert encode_records(array) == expected[HEADER_LENGTH:]
    header = read_header(os.path.join(tmp_dir, f'{cell.cell_id}.dt{cell.level}'))
    assert header['dsi.partial_cell'] == f'{partial_cell_indicator(array):02d}'


def test_gdal_reads_a_native_file_with_checksum_verification(tmp_dir):
    cell = CellGeometry(2, 30, -6)
    array = _cell_array(cell, seed=3)
    header = new_header(cell)
    header.set('dsi.partial_cell', partial_cell_indicator(array))
    path = os.path.join(tmp_dir, 'native.dt2')
    content = write_dted_file(path, header, array)
    assert os.path.getsize(path) == len(content) == HEADER_LENGTH + cell.lon_lines * record_length(cell.lat_points)

    with gdal.config_option('DTED_VERIFY_CHECKSUM', 'YES'), gdal.config_option('DTED_ASSUME_CONFORMANT', 'TRUE'):
        with gdal.Open(path) as ds:
            band = ds.GetRasterBand(1)
            values = band.ReadAsArray()
            nodata = band.GetNoDataValue()
            geotransform = ds.GetGeoTransform()
    assert nodata == NULL_ELEVATION
    assert np.array_equal(values, array)
    cols = cell.lon_lines
    assert geotransform == pytest.approx((30 - 0.5 / (cols - 1), 1 / (cols - 1), 0, -5 + 0.5 / 3600, 0, -1 / 3600))

    decoded = read_records(path)
    assert np.array_equal(decoded.values, array) and decoded.bad_checksums == 0
    edges = read_edges(path)
    assert np.array_equal(edges['north'], array[0]) and np.array_equal(edges['south'], array[-1])
    assert np.array_equal(edges['west'], array[:, 0]) and np.array_equal(edges['east'], array[:, -1])
    _header, issues = validate_file(path, check_data=True)
    assert not [issue for issue in issues if issue.severity == 'error' and issue.record == 'DATA']


def test_write_refuses_a_header_that_disagrees_with_the_array(tmp_dir):
    cell = CellGeometry(0, 126, 6)
    header = new_header(cell)
    path = os.path.join(tmp_dir, 'wrong.dt0')
    with pytest.raises(RecordError, match='announces'):
        write_dted_file(path, header, np.zeros((121, 120), dtype=np.int16))
    with pytest.raises(RecordError, match='NaN'):
        write_dted_file(path, header, np.full((121, 121), np.nan))
    assert not os.path.exists(path)


def test_out_of_range_posts_are_a_validation_warning(tmp_dir):
    cell = CellGeometry(0, 126, 6)
    array = _cell_array(cell)
    array[20, 20] = 9500
    header = new_header(cell)
    path = os.path.join(tmp_dir, 'high.dt0')
    write_dted_file(path, header, check_values(np.where(array == 9500, 0, array)))
    # Patch the post in place: the writer refuses such a value, a file can still hold one.
    with open(path, 'r+b') as handle:
        handle.seek(HEADER_LENGTH + 20 * record_length(cell.lat_points) + 8 + 2 * (cell.lat_points - 1 - 20))
        handle.write((9500).to_bytes(2, 'big'))
    decoded = read_records(path)
    assert decoded.values[20, 20] == 9500 and decoded.bad_checksums == 1
    _header, issues = validate_file(path, check_data=True)
    warnings = [issue.message for issue in issues if issue.severity == 'warning']
    assert any('outside the -12,000 to 9,000 m range' in message for message in warnings)


def test_partial_cell_indicator():
    assert partial_cell_indicator(np.zeros((4, 4), dtype=np.int16)) == 0
    half = np.zeros((4, 4), dtype=np.int16)
    half[:2] = NULL_ELEVATION
    assert partial_cell_indicator(half) == 50
    one = np.zeros((10, 10), dtype=np.int16)
    one[0, 0] = NULL_ELEVATION
    assert partial_cell_indicator(one) == 99
    assert partial_cell_indicator(np.full((10, 10), NULL_ELEVATION, dtype=np.int16)) == 1
    nearly = np.full((1000, 1), NULL_ELEVATION, dtype=np.int16)
    nearly[0] = 5
    assert partial_cell_indicator(nearly) == 1
