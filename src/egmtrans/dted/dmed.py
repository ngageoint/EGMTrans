"""The Digital Mean Elevation Data (DMED) volume file of MIL-PRF-89020B 3.9.5.

A DTED delivery (3.10.7.2: the DTED folder with one folder per longitude and
the cells named for their southwest latitude, ``DTED/E006/N49.dt2``) carries
one DMED file at its root: "an ASCII text file" of 394-byte records that
holds, "for each 15' x 15' area of a 1 x 1 degree cell, the minimum and
maximum elevation, the mean elevation, and the standard deviation".

The first record is the header, "giving the extremes of the minimum bounding
rectangle (MBR) (in degrees) encompassing the cells", for example
``N30N36E020E032`` followed by 380 blanks for a rectangle 6 degrees high and
12 wide: the second latitude and longitude are the far edges. Then comes one
record per cell of the rectangle, "the extreme southwest 1 degree cell"
first, "the 1 degree cell above that, and so forth, to the top of the MBR",
then "moving eastwardly" the next column, south to north again. A cell that
is not in the delivery "consists of its coordinates followed by 387 spaces".

A cell record (3.9.5.1) is the cell's southwest corner (``N49E006``), the data
edition number and the match/merge version from its DSI, then for each of
the 16 areas, numbered south to north within each column and the columns
west to east, the minimum (6 characters), the maximum (6), the mean (6), one
unused character and the standard deviation about the mean (5), all to the
nearest meter; "if negative, sign will be the next place left of most
significant digit". "In a 1 degree cell, the elevations in the three rows and
three columns, which divide the 15' x 15' areas from each other, are counted
in two areas." Here an area holds the posts whose coordinates lie within its
closed 15-minute interval on each axis, which is that rule wherever a
dividing line falls on a post (every level and zone but DTED0 in zone IV,
whose 31 longitude lines put the 15' and 45' meridians between posts).

What the specification leaves open is settled as follows: void posts are
left out of the statistics and an area with no valid post is blank in all
four fields; the standard deviation is the population standard deviation
about the area's mean; the mean and the standard deviation are rounded to the
nearest meter, halves away from zero, like the posts; records have no line
terminator (the record length is 394 bytes including everything); the file
is named ``DMED`` at the delivery's root (3.9 writes ``dmed.``, the ISO 9660
form of a name without extension).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import numpy as np

from egmtrans.config import DTED_ROOT
from egmtrans.dted.header import DtedHeader, read_header
from egmtrans.dted.header import cell_id as make_cell_id
from egmtrans.dted.records import read_records
from egmtrans.dted.schema import LEVEL_BY_EXTENSION, NULL_ELEVATION

DMED_NAME = 'DMED'
RECORD_LENGTH = 394
AREAS_PER_SIDE = 4
AREA_COUNT = AREAS_PER_SIDE * AREAS_PER_SIDE
AREA_RECORD_LENGTH = 24  # min (6), max (6), mean (6), blank (1), standard deviation (5)
CELL_PREFIX_LENGTH = 10  # coordinates (7), edition (2), version (1)

_LON_FOLDER = re.compile(r'^([EW])(\d{3})$', re.IGNORECASE)
_CELL_FILE = re.compile(r'^([NS])(\d{2})\.(dt[012])$', re.IGNORECASE)


class DmedError(ValueError):
    """The delivery cannot be described by a DMED file."""


@dataclass(frozen=True)
class VolumeCell:
    """One DTED cell of the delivery, located by its header."""

    path: str
    lon0: int
    lat0: int
    level: int

    @property
    def cell_id(self) -> str:
        return make_cell_id(self.lon0, self.lat0)


@dataclass(frozen=True)
class AreaStats:
    """The statistics of one 15' x 15' area, in whole meters."""

    minimum: int
    maximum: int
    mean: int
    std: int


@dataclass
class CellRecord:
    """What one cell record of the DMED holds."""

    lon0: int
    lat0: int
    edition: str
    version: str
    areas: list[AreaStats | None]

    @property
    def cell_id(self) -> str:
        return make_cell_id(self.lon0, self.lat0)


@dataclass
class Rectangle:
    """The minimum bounding rectangle of the cells: south and west are the
    origins of the extreme cells, north and east their far edges."""

    south: int
    north: int
    west: int
    east: int

    def cells(self):
        """Every ``(lon0, lat0)`` of the rectangle in DMED order: columns west
        to east, south to north within a column."""
        for lon0 in range(self.west, self.east):
            for lat0 in range(self.south, self.north):
                yield lon0, lat0


@dataclass
class DmedResult:
    path: str
    rectangle: Rectangle
    cells: list[VolumeCell] = field(default_factory=list)
    content: bytes = b''

    @property
    def record_count(self) -> int:
        return len(self.content) // RECORD_LENGTH


def locate_tree(folder: str) -> tuple[str, str]:
    """The delivery root and its ``DTED`` folder for *folder*, which is either
    the delivery root or the ``DTED`` folder itself.

    Raises:
        DmedError: If *folder* is not a folder or holds no ``DTED`` folder.
    """
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        raise DmedError(f'{folder} is not a folder')
    if os.path.basename(folder).upper() == DTED_ROOT:
        return os.path.dirname(folder) or os.curdir, folder
    for name in sorted(os.listdir(folder)):
        if name.upper() == DTED_ROOT and os.path.isdir(os.path.join(folder, name)):
            return folder, os.path.join(folder, name)
    raise DmedError(
        f'{folder} holds no {DTED_ROOT} folder; a delivery is laid out as {DTED_ROOT}/E006/N49.dt2 '
        f'(MIL-PRF-89020B 3.10.7.2)'
    )


def scan_volume(folder: str) -> tuple[str, list[VolumeCell]]:
    """The delivery root of *folder* and its cells, ordered west to east and
    south to north by the origins their headers carry.

    Only ``DTED/<E|W>DDD/<N|S>DD.dtN`` files are cells (matched case
    insensitively); masks, companion files, logs and anything else in the
    tree are ignored.

    Raises:
        DmedError: If there is no ``DTED`` folder or no cell under it, a cell's
            name disagrees with its header, or the cells are of several levels.
    """
    root, tree = locate_tree(folder)
    cells: list[VolumeCell] = []
    problems: list[str] = []
    for lon_name in sorted(os.listdir(tree)):
        lon_folder = os.path.join(tree, lon_name)
        if not os.path.isdir(lon_folder) or not _LON_FOLDER.match(lon_name):
            continue
        for name in sorted(os.listdir(lon_folder)):
            match = _CELL_FILE.match(name)
            if not match:
                continue
            path = os.path.join(lon_folder, name)
            try:
                header = read_header(path)
            except (OSError, ValueError) as e:
                problems.append(f'{path}: {e}')
                continue
            lon, lat = header.origin
            if lon is None or lat is None:
                problems.append(f'{path}: the header has no readable origin')
                continue
            lon0, lat0 = int(round(lon)), int(round(lat))
            named = make_cell_id(*_named_origin(lon_name, match.group(1), match.group(2)))
            if named != make_cell_id(lon0, lat0):
                problems.append(f'{path}: the name says {named} but the header says {make_cell_id(lon0, lat0)}')
                continue
            cells.append(VolumeCell(path, lon0, lat0, LEVEL_BY_EXTENSION['.' + match.group(3).lower()].number))
    if problems:
        shown = '\n  '.join(problems[:10])
        more = f'\n  ... and {len(problems) - 10} more' if len(problems) > 10 else ''
        raise DmedError(f'Cells that cannot be described:\n  {shown}{more}')
    if not cells:
        raise DmedError(f'No DTED cell under {tree} ({DTED_ROOT}/E006/N49.dt2 and the like)')
    levels = sorted({cell.level for cell in cells})
    if len(levels) > 1:
        raise DmedError(f'The cells are of several levels ({", ".join(str(level) for level in levels)}); '
                        f'a DMED describes one level')
    cells.sort(key=lambda cell: (cell.lon0, cell.lat0))
    return root, cells


def _named_origin(lon_name: str, lat_hemisphere: str, lat_degrees: str) -> tuple[int, int]:
    lon_match = _LON_FOLDER.match(lon_name)
    lon0 = int(lon_match.group(2)) * (1 if lon_match.group(1).upper() == 'E' else -1)
    lat0 = int(lat_degrees) * (1 if lat_hemisphere.upper() == 'N' else -1)
    return lon0, lat0


def rectangle_of(cells: list[VolumeCell]) -> Rectangle:
    """The minimum bounding rectangle of *cells*."""
    return Rectangle(
        south=min(cell.lat0 for cell in cells),
        north=max(cell.lat0 for cell in cells) + 1,
        west=min(cell.lon0 for cell in cells),
        east=max(cell.lon0 for cell in cells) + 1,
    )


def area_slices(lat_points: int, lon_lines: int, area: int) -> tuple[slice, slice]:
    """The array rows and columns of *area* (1 to 16) of a cell of
    *lat_points* rows (north to south) by *lon_lines* columns.

    Area ``k`` lies in column ``(k - 1) // 4`` from the west and row
    ``(k - 1) % 4`` from the south, and holds the posts whose longitude and
    latitude offsets lie within the closed 15-minute intervals of that column
    and row: post ``j`` (offset ``j / (n - 1)`` degrees) is in column ``c``
    when ``c * (n - 1) <= 4 * j <= (c + 1) * (n - 1)``, so a dividing line
    that falls on a post puts that post in both areas.
    """
    if not 1 <= area <= AREA_COUNT:
        raise ValueError(f'a cell has areas 1 to {AREA_COUNT}, not {area}')
    column, row = (area - 1) // AREAS_PER_SIDE, (area - 1) % AREAS_PER_SIDE
    first_col, last_col = _interval(lon_lines, column)
    first_south, last_south = _interval(lat_points, row)
    # Rows run north to south: offset s from the south edge is array row (m - 1 - s).
    return slice(lat_points - 1 - last_south, lat_points - last_south + (last_south - first_south)), slice(
        first_col, last_col + 1
    )


def _interval(points: int, index: int) -> tuple[int, int]:
    """The first and last post (offsets from the west or south edge) of the
    closed 15-minute interval *index* of a side with *points* posts."""
    n = points - 1
    first = -(-(index * n) // AREAS_PER_SIDE)  # ceil
    last = ((index + 1) * n) // AREAS_PER_SIDE  # floor
    return first, last


def _round_half_away(value: float) -> int:
    return int(np.floor(value + 0.5)) if value >= 0 else int(np.ceil(value - 0.5))


def area_stats(values: np.ndarray, area: int) -> AreaStats | None:
    """The statistics of *area* of the cell array *values* (rows north to
    south, voids -32767), or None when the area holds no valid post."""
    rows, cols = area_slices(*values.shape, area)
    block = values[rows, cols]
    valid = block[block != NULL_ELEVATION].astype(np.float64)
    if valid.size == 0:
        return None
    mean = float(valid.mean())
    std = float(np.sqrt(np.mean((valid - mean) ** 2)))
    return AreaStats(int(valid.min()), int(valid.max()), _round_half_away(mean), _round_half_away(std))


def cell_record(cell: VolumeCell, header: DtedHeader | None = None) -> CellRecord:
    """The DMED record of *cell*, from its DSI and its posts."""
    header = header or read_header(cell.path)
    values = read_records(cell.path, header, verify_checksums=False).values
    return CellRecord(
        cell.lon0, cell.lat0, header['dsi.data_edition'], header['dsi.match_merge_version'],
        [area_stats(values, area) for area in range(1, AREA_COUNT + 1)],
    )


def _lat_text(degrees: int) -> str:
    return f"{'N' if degrees >= 0 else 'S'}{abs(degrees):02d}"


def _lon_text(degrees: int) -> str:
    return f"{'E' if degrees >= 0 else 'W'}{abs(degrees):03d}"


def header_record(rectangle: Rectangle) -> str:
    """The first DMED record: the rectangle's south and north latitudes and
    west and east longitudes, then blanks."""
    text = _lat_text(rectangle.south) + _lat_text(rectangle.north) + _lon_text(rectangle.west) + _lon_text(
        rectangle.east
    )
    return text.ljust(RECORD_LENGTH)


def format_area(stats: AreaStats | None) -> str:
    """The 24 characters of one area: blank when the area holds no valid post."""
    if stats is None:
        return ' ' * AREA_RECORD_LENGTH
    return f'{stats.minimum:6d}{stats.maximum:6d}{stats.mean:6d} {stats.std:5d}'


def format_cell(record: CellRecord) -> str:
    """The 394 characters of a cell record."""
    prefix = _lat_text(record.lat0) + _lon_text(record.lon0) + record.edition.rjust(2) + record.version.rjust(1)
    text = prefix + ''.join(format_area(stats) for stats in record.areas)
    if len(text) != RECORD_LENGTH:
        raise DmedError(f'a cell record is {RECORD_LENGTH} characters, not {len(text)}')
    return text.upper()


def absent_record(lon0: int, lat0: int) -> str:
    """The record of a cell of the rectangle that is not in the delivery."""
    return (_lat_text(lat0) + _lon_text(lon0)).ljust(RECORD_LENGTH)


def build_dmed(cells: list[VolumeCell]) -> tuple[Rectangle, bytes]:
    """The DMED content for *cells*: the header record, then one record per
    cell of their bounding rectangle in DMED order."""
    rectangle = rectangle_of(cells)
    by_origin = {(cell.lon0, cell.lat0): cell for cell in cells}
    records = [header_record(rectangle)]
    for lon0, lat0 in rectangle.cells():
        cell = by_origin.get((lon0, lat0))
        records.append(format_cell(cell_record(cell)) if cell is not None else absent_record(lon0, lat0))
    content = ''.join(records)
    if not content.isascii():
        raise DmedError('a DMED record holds a character that is not ASCII')
    return rectangle, content.encode('ascii')


def write_dmed(folder: str, out: str | None = None) -> DmedResult:
    """Write the DMED of the delivery under *folder* (its root, or its ``DTED``
    folder) to *out*, by default ``DMED`` at the delivery's root.

    Raises:
        DmedError: See :func:`scan_volume`.
        OSError: If the file cannot be written.
    """
    root, cells = scan_volume(folder)
    rectangle, content = build_dmed(cells)
    path = out or os.path.join(root, DMED_NAME)
    with open(path, 'wb') as handle:
        handle.write(content)
    return DmedResult(path, rectangle, cells, content)


@dataclass
class ParsedRecord:
    lon0: int
    lat0: int
    present: bool
    edition: str = ''
    version: str = ''
    areas: list[AreaStats | None] = field(default_factory=list)


@dataclass
class ParsedDmed:
    rectangle: Rectangle
    records: list[ParsedRecord]


def _parse_lat(text: str) -> int:
    return int(text[1:]) * (1 if text[0] == 'N' else -1)


def _parse_lon(text: str) -> int:
    return int(text[1:]) * (1 if text[0] == 'E' else -1)


def _parse_int(text: str) -> int:
    return int(text)


def parse_dmed(path: str) -> ParsedDmed:
    """Read a DMED file back into its rectangle and records.

    Raises:
        DmedError: If the file is not a sequence of 394-byte records with a
            well-formed header.
    """
    with open(path, 'rb') as handle:
        data = handle.read()
    if not data or len(data) % RECORD_LENGTH:
        raise DmedError(f'{path} is {len(data):,} bytes, not a whole number of {RECORD_LENGTH}-byte records')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError as e:
        raise DmedError(f'{path} is not ASCII: {e}') from e
    records = [text[i:i + RECORD_LENGTH] for i in range(0, len(text), RECORD_LENGTH)]
    header = re.fullmatch(r'([NS]\d{2})([NS]\d{2})([EW]\d{3})([EW]\d{3}) {380}', records[0])
    if not header:
        raise DmedError(f'{path}: the first record is not a DMED header (N30N36E020E032 and 380 blanks)')
    rectangle = Rectangle(_parse_lat(header.group(1)), _parse_lat(header.group(2)),
                          _parse_lon(header.group(3)), _parse_lon(header.group(4)))
    parsed = []
    for number, record in enumerate(records[1:], start=2):
        prefix = re.match(r'([NS]\d{2})([EW]\d{3})', record)
        if not prefix:
            raise DmedError(f'{path}: record {number} does not start with a cell coordinate')
        lat0, lon0 = _parse_lat(prefix.group(1)), _parse_lon(prefix.group(2))
        if record[7:].strip() == '':
            parsed.append(ParsedRecord(lon0, lat0, False))
            continue
        areas: list[AreaStats | None] = []
        for k in range(AREA_COUNT):
            start = CELL_PREFIX_LENGTH + k * AREA_RECORD_LENGTH
            chunk = record[start:start + AREA_RECORD_LENGTH]
            if chunk.strip() == '':
                areas.append(None)
                continue
            try:
                areas.append(AreaStats(_parse_int(chunk[0:6]), _parse_int(chunk[6:12]), _parse_int(chunk[12:18]),
                                       _parse_int(chunk[19:24])))
            except ValueError as e:
                raise DmedError(f'{path}: record {number}, area {k + 1} is malformed: {chunk!r}') from e
        parsed.append(ParsedRecord(lon0, lat0, True, record[7:9], record[9], areas))
    return ParsedDmed(rectangle, parsed)


def check_dmed(folder: str, path: str | None = None) -> list[str]:
    """Compare the DMED at *path* (default: the delivery's) with the one the
    cells under *folder* give; returns the differences, empty when none."""
    root, cells = scan_volume(folder)
    path = path or os.path.join(root, DMED_NAME)
    if not os.path.isfile(path):
        return [f'{path} does not exist']
    rectangle, expected = build_dmed(cells)
    with open(path, 'rb') as handle:
        actual = handle.read()
    if actual == expected:
        return []
    problems = []
    if len(actual) != len(expected):
        problems.append(f'{len(actual):,} bytes; the cells give {len(expected):,}')
    for number in range(min(len(actual), len(expected)) // RECORD_LENGTH):
        start = number * RECORD_LENGTH
        if actual[start:start + RECORD_LENGTH] != expected[start:start + RECORD_LENGTH]:
            problems.append(f'record {number + 1} differs: {expected[start:start + 10].decode("ascii")!r}')
            if len(problems) >= 10:
                problems.append('...')
                break
    return problems
