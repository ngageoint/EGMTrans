"""The elevation records of a DTED file: everything after the 3,428-byte header.

A cell is stored as one record per longitude line, west to east. A record is
the 0xAA sentinel, a 3-byte block count and a 2-byte longitude count (both
the line's index from 0), a 2-byte latitude count (0), the line's posts south
to north as 16-bit big-endian signed-magnitude integers (the sign in the high
bit, the magnitude never complemented; -32767, stored as 0xFFFF, is a void),
and a 4-byte checksum: the sum of every preceding byte of the record.

The codec is numpy slicing and integer arithmetic on fixed-length arrays, so
the bytes it writes depend on nothing but the values, and a value that could
not be a height (NaN, out of range, not a whole number) is refused before it
can reach a record. GDAL reads the files it writes; EGMTrans checks that too
before an output appears under its name.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from egmtrans.dted.header import DtedHeader, encode_header, read_header
from egmtrans.dted.schema import (
    DATA_RECORD_OVERHEAD,
    DATA_RECORD_SENTINEL,
    ELEVATION_MAX,
    ELEVATION_MIN,
    HEADER_LENGTH,
    NULL_ELEVATION,
)


class RecordError(ValueError):
    """Values that cannot be written as DTED records, or bytes that are not DTED records."""


def record_length(lat_points: int) -> int:
    """The length in bytes of one record holding *lat_points* posts."""
    return DATA_RECORD_OVERHEAD + 2 * lat_points


def check_values(values: np.ndarray) -> np.ndarray:
    """*values* as an int32 array fit for a record, or :class:`RecordError`.

    Every post must be a whole number between :data:`ELEVATION_MIN` and
    :data:`ELEVATION_MAX`, or the void value :data:`NULL_ELEVATION`; nothing
    else, NaN and infinities included, can be written.
    """
    array = np.asarray(values)
    if array.ndim != 2:
        raise RecordError(f'a cell is a 2-D array of posts, not {array.ndim}-D')
    if np.issubdtype(array.dtype, np.floating):
        finite = np.isfinite(array)
        if not finite.all():
            raise RecordError(f'{int((~finite).sum()):,} post(s) are NaN or infinite; voids must be {NULL_ELEVATION}')
        if not np.array_equal(array, np.rint(array)):
            raise RecordError('posts must be whole meters; round them first')
    elif not np.issubdtype(array.dtype, np.integer):
        raise RecordError(f'posts must be integers, not {array.dtype}')
    posts = array.astype(np.int32)
    out = (posts != NULL_ELEVATION) & ((posts < ELEVATION_MIN) | (posts > ELEVATION_MAX))
    if out.any():
        bad = posts[out]
        raise RecordError(
            f'{int(out.sum()):,} post(s) lie outside {ELEVATION_MIN:,} to {ELEVATION_MAX:,} m '
            f'({int(bad.min()):,} to {int(bad.max()):,}); the specification allows no such height'
        )
    return posts


def encode_records(values: np.ndarray) -> bytes:
    """The records of a cell whose posts are *values*: rows north to south and
    columns west to east, as GDAL reads a cell, with voids as :data:`NULL_ELEVATION`.

    Raises:
        RecordError: If a value cannot be written (see :func:`check_values`).
    """
    posts = check_values(values)
    lat_points, lon_lines = posts.shape
    length = record_length(lat_points)
    # Record j is column j, south to north.
    lines = np.ascontiguousarray(posts[::-1, :].T)
    magnitude = np.abs(lines).astype(np.uint16)
    words = np.where(lines < 0, magnitude | np.uint16(0x8000), magnitude).astype('>u2')

    body = np.zeros((lon_lines, length), dtype=np.uint8)
    body[:, 0] = DATA_RECORD_SENTINEL
    index = np.arange(lon_lines, dtype=np.int64)
    body[:, 1] = (index >> 16) & 0xFF
    body[:, 2] = (index >> 8) & 0xFF
    body[:, 3] = index & 0xFF
    body[:, 4] = (index >> 8) & 0xFF
    body[:, 5] = index & 0xFF
    body[:, 8:8 + 2 * lat_points] = np.ascontiguousarray(words).view(np.uint8).reshape(lon_lines, 2 * lat_points)
    checksum = body[:, :8 + 2 * lat_points].sum(axis=1, dtype=np.uint64)
    body[:, length - 4] = (checksum >> 24) & 0xFF
    body[:, length - 3] = (checksum >> 16) & 0xFF
    body[:, length - 2] = (checksum >> 8) & 0xFF
    body[:, length - 1] = checksum & 0xFF
    return body.tobytes()


@dataclass(frozen=True)
class DecodedRecords:
    """The posts of a record block and what was wrong with its framing.

    *values* is an int32 array with rows north to south and columns west to
    east, :data:`NULL_ELEVATION` where a post is void. The counts are the
    records whose sentinel, block count, longitude count or latitude count is
    wrong, and whose checksum does not match (None when not verified).
    """

    values: np.ndarray
    bad_sentinels: int
    bad_block_counts: int
    bad_lon_counts: int
    bad_lat_counts: int
    bad_checksums: int | None

    @property
    def framing_ok(self) -> bool:
        return not (self.bad_sentinels or self.bad_block_counts or self.bad_lon_counts or self.bad_lat_counts)


def decode_records(
    data: bytes | np.ndarray, lon_lines: int, lat_points: int, *, verify_checksums: bool = True
) -> DecodedRecords:
    """Decode the record block *data* of a cell of *lon_lines* by *lat_points* posts.

    Raises:
        RecordError: If *data* is not the length the counts require.
    """
    data = np.frombuffer(data, dtype=np.uint8) if isinstance(data, (bytes, bytearray, memoryview)) else data
    length = record_length(lat_points)
    expected = lon_lines * length
    if data.size != expected:
        raise RecordError(
            f'{data.size:,} bytes of records; {lon_lines} records of {length} bytes make {expected:,}'
        )
    records = data.reshape(lon_lines, length)
    index = np.arange(lon_lines, dtype=np.int64)

    bad_sentinels = int(np.count_nonzero(records[:, 0] != DATA_RECORD_SENTINEL))
    block = (records[:, 1].astype(np.int64) << 16) | (records[:, 2].astype(np.int64) << 8) | records[:, 3]
    bad_block_counts = int(np.count_nonzero(block != index))
    lon_count = (records[:, 4].astype(np.int64) << 8) | records[:, 5]
    bad_lon_counts = int(np.count_nonzero(lon_count != index))
    lat_count = (records[:, 6].astype(np.int64) << 8) | records[:, 7]
    bad_lat_counts = int(np.count_nonzero(lat_count != 0))

    bad_checksums = None
    if verify_checksums:
        body = records[:, :8 + 2 * lat_points].sum(axis=1, dtype=np.uint64)
        stored = (
            (records[:, -4].astype(np.uint64) << 24) | (records[:, -3].astype(np.uint64) << 16)
            | (records[:, -2].astype(np.uint64) << 8) | records[:, -1].astype(np.uint64)
        )
        bad_checksums = int(np.count_nonzero(body != stored))

    words = records[:, 8:8 + 2 * lat_points].reshape(lon_lines, lat_points, 2)
    raw = (words[:, :, 0].astype(np.int32) << 8) | words[:, :, 1].astype(np.int32)
    magnitude = raw & 0x7FFF
    lines = np.where(raw & 0x8000, -magnitude, magnitude)  # 0xFFFF is -32767, the void
    values = np.ascontiguousarray(lines.T[::-1, :])
    return DecodedRecords(values, bad_sentinels, bad_block_counts, bad_lon_counts, bad_lat_counts, bad_checksums)


def read_records(path: str, header: DtedHeader | None = None, *, verify_checksums: bool = True) -> DecodedRecords:
    """Decode the records of the DTED file at *path* (its header is read when not given).

    Raises:
        RecordError: If the header's counts cannot be read or the file's
            length does not match them.
    """
    if header is None:
        header = read_header(path)
    lon_lines = header.interval_tenths('uhl.lon_lines')
    lat_points = header.interval_tenths('uhl.lat_points')
    if not lon_lines or not lat_points:
        raise RecordError('the record layout cannot be read from the UHL counts')
    with open(path, 'rb') as handle:
        handle.seek(HEADER_LENGTH)
        data = np.frombuffer(handle.read(), dtype=np.uint8)
    return decode_records(data, lon_lines, lat_points, verify_checksums=verify_checksums)


def read_edges(path: str) -> dict[str, np.ndarray]:
    """The four edge profiles of the cell at *path*, as int32 arrays: ``north``
    and ``south`` west to east, ``west`` and ``east`` north to south."""
    values = read_records(path, verify_checksums=False).values
    return {
        'north': values[0, :].copy(),
        'south': values[-1, :].copy(),
        'west': values[:, 0].copy(),
        'east': values[:, -1].copy(),
    }


def partial_cell_indicator(values: np.ndarray) -> int:
    """The DSI partial cell indicator for *values*: 0 for a complete cell,
    else the percentage of posts that hold data, 1 to 99."""
    posts = np.asarray(values)
    total = posts.size
    voids = int(np.count_nonzero(posts == NULL_ELEVATION))
    if voids == 0 or total == 0:
        return 0
    return min(max((total - voids) * 100 // total, 1), 99)


def write_dted_file(path: str, header: DtedHeader, values: np.ndarray) -> bytes:
    """Write a complete DTED file: *header* followed by the records of *values*.

    The header's counts must match the array. Returns the bytes written.

    Raises:
        RecordError: If the counts and the array disagree, or a value cannot
            be written.
    """
    posts = check_values(values)
    lon_lines = header.interval_tenths('uhl.lon_lines')
    lat_points = header.interval_tenths('uhl.lat_points')
    if posts.shape != (lat_points, lon_lines):
        raise RecordError(
            f'the header announces {lon_lines} longitude lines of {lat_points} posts; the array holds '
            f'{posts.shape[1]} of {posts.shape[0]}'
        )
    content = encode_header(header) + encode_records(posts)
    with open(path, 'wb') as handle:
        handle.write(content)
    return content
