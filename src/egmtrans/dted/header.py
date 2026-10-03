"""Parse, build and write the 3,428-byte DTED header (UHL, DSI, ACC).

A :class:`DtedHeader` keeps every field as the raw fixed-width text found in
the file, so ``encode_header(parse_header(b)) == b`` for any file, well formed
or not. Typed values go in and out through :func:`format_field` and the
decoders, and the accuracy subregions of the ACC record are decoded on demand
from their 2,556-character block.

Files are read and written as raw bytes, never through GDAL, so a ``.aux.xml``
sidecar or a driver default cannot stand between the tool and the header.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from egmtrans.dted import schema
from egmtrans.dted.schema import (
    ALL_FIELDS,
    COORDINATE_FIELDS,
    COORDINATE_LENGTH,
    DATA_RECORD_SENTINEL,
    FIELDS_BY_KEY,
    HEADER_LENGTH,
    LEVEL_BY_EXTENSION,
    LEVEL_BY_LAT_INTERVAL,
    LEVEL_BY_LAT_POINTS,
    LEVEL_BY_SERIES,
    LEVELS,
    MAX_COORDINATES,
    MAX_SUBREGIONS,
    NA_VALUE,
    RECORD_FIELDS,
    SUBREGION_COORDS_START,
    SUBREGION_FIELDS,
    SUBREGION_LENGTH,
    Field,
    zone_for,
)

# latin-1 maps every byte to one code point and back, so a header with stray
# NUL or high bytes survives a parse/encode round trip unchanged.
ENCODING = 'latin-1'

_DMS_FORMATS = {
    # kind: (degree digits, tenths of seconds, hemisphere letters)
    'uhl_lon': (3, False, 'EW'),
    'uhl_lat': (3, False, 'NS'),
    'dsi_lat': (2, True, 'NS'),
    'dsi_lon': (3, True, 'EW'),
    'corner_lat': (2, False, 'NS'),
    'corner_lon': (3, False, 'EW'),
}
_INTEGER_KINDS = {'interval', 'count', 'edition', 'partial', 'spec_amend', 'coord_count'}


def has_visible_text(text: str) -> bool:
    """True when *text* holds a printable character other than a blank."""
    return any(32 < ord(ch) <= 126 for ch in text)


def cell_id(lon0: int, lat0: int) -> str:
    """The cell identifier of the cell whose southwest corner is (lon0, lat0): N38E045."""
    return f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"


def parse_cell_id(text: str) -> tuple[int, int]:
    """(lon0, lat0) of a cell identifier such as N38E045 or s06e030."""
    match = re.fullmatch(r'\s*([NnSs])(\d{2})([EeWw])(\d{3})\s*', text or '')
    if not match:
        raise ValueError(f'Not a cell identifier (expected N38E045): {text!r}')
    lat0 = int(match.group(2)) * (1 if match.group(1).upper() == 'N' else -1)
    lon0 = int(match.group(4)) * (1 if match.group(3).upper() == 'E' else -1)
    if not -90 <= lat0 <= 89 or not -180 <= lon0 <= 179:
        raise ValueError(f'Cell identifier out of range: {text!r}')
    return lon0, lat0


def format_dms(degrees: float, kind: str) -> str:
    """Format signed decimal degrees in one of the header's DMS layouts."""
    deg_digits, tenths, hemispheres = _DMS_FORMATS[kind]
    hemisphere = hemispheres[0] if degrees >= 0 else hemispheres[1]
    total_tenths = round(abs(degrees) * 36000)
    d, rest = divmod(total_tenths, 36000)
    m, rest = divmod(rest, 600)
    s, t = divmod(rest, 10)
    if tenths:
        return f'{d:0{deg_digits}d}{m:02d}{s:02d}.{t}{hemisphere}'
    s += 1 if t >= 5 else 0
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f'{d:0{deg_digits}d}{m:02d}{s:02d}{hemisphere}'


def parse_dms(text: str, kind: str) -> float | None:
    """Signed decimal degrees of a DMS field, or None when it is malformed."""
    deg_digits, tenths, hemispheres = _DMS_FORMATS[kind]
    pattern = rf'^(\d{{{deg_digits}}})(\d{{2}})(\d{{2}})' + (r'\.(\d)' if tenths else '') + rf'([{hemispheres}])$'
    match = re.match(pattern, text)
    if not match:
        return None
    groups = match.groups()
    d, m, s = int(groups[0]), int(groups[1]), int(groups[2])
    t = int(groups[3]) if tenths else 0
    hemisphere = groups[-1]
    value = d + m / 60 + s / 3600 + t / 36000
    return -value if hemisphere in 'SW' else value


def yymm_to_iso(text: str) -> str | None:
    """'YYYY-MM' for a YYMM date; None for 0000 or a malformed value."""
    if not re.fullmatch(r'\d{4}', text or '') or text == '0000':
        return None
    yy, mm = int(text[:2]), int(text[2:])
    if not 1 <= mm <= 12:
        return None
    year = 1900 + yy if yy >= schema.CENTURY_PIVOT else 2000 + yy
    return f'{year:04d}-{mm:02d}'


def to_yymm(value: str | dt.date | None) -> str:
    """The YYMM form of a date given as a date, 'YYYY-MM', 'YYYY-MM-DD', or YYMM already."""
    if value is None or value == '':
        return '0000'
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return f'{value.year % 100:02d}{value.month:02d}'
    text = str(value).strip()
    if re.fullmatch(r'\d{4}', text):
        return text
    match = re.fullmatch(r'(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ].*)?', text)
    if match:
        return f'{int(match.group(1)) % 100:02d}{match.group(2)}'
    raise ValueError(f'Not a date (expected YYYY-MM, YYYY-MM-DD or YYMM): {value!r}')


def decode_accuracy(text: str) -> int | None:
    """The meters of an accuracy field; None for NA.

    Raises:
        ValueError: If the field is neither four digits nor NA.
    """
    if re.fullmatch(r'\d{4}', text):
        return int(text)
    if text.strip() == 'NA':
        return None
    raise ValueError(f'Not an accuracy value (0000-9999 or NA): {text!r}')


def format_accuracy(value: int | str | None) -> str:
    """The four-character form of an accuracy: zero-filled meters or left-justified NA."""
    if value is None:
        return NA_VALUE
    if isinstance(value, str):
        text = value.strip()
        if text.upper() == 'NA' or text == '':
            return NA_VALUE
        value = int(text)
    if not 0 <= int(value) <= 9999:
        raise ValueError(f'Accuracy must be 0-9999 m or NA, not {value!r}')
    return f'{int(value):04d}'


def format_field(item: Field, value) -> str:
    """The raw, exactly *item.length* characters long, form of *value*.

    Strings are justified per the field (numeric kinds right with zeros, the
    rest left with blanks); ints, dates and degrees are formatted for the
    field's kind. None gives the spec fill: NA for an accuracy, 0000 for a
    date or code, 00 for a flag, blanks otherwise.

    Raises:
        ValueError: If the value is too long or not ASCII.
    """
    kind = item.kind
    if value is None:
        if kind == 'accuracy':
            text = NA_VALUE
        elif kind in ('date', 'maint_code'):
            text = '0000'
        elif kind in ('outline_flag', 'partial', 'edition', 'spec_amend', 'coord_count'):
            text = '0' * item.length
        elif kind == 'flag1':
            text = '0'
        elif kind == 'angle':
            text = '0000000.0'
        elif item.fixed is not None:
            text = item.fixed
        else:
            text = ''
    elif kind == 'accuracy':
        text = format_accuracy(value)
    elif kind == 'date' and not isinstance(value, str):
        text = to_yymm(value)
    elif kind == 'date' and isinstance(value, str) and not re.fullmatch(r'\d{4}', value.strip()):
        text = to_yymm(value)
    elif kind in _DMS_FORMATS and isinstance(value, int | float) and not isinstance(value, bool):
        text = format_dms(float(value), kind)
    elif isinstance(value, bool):
        text = '1' if value else '0'
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, dt.date):
        raise ValueError(f'{item.key} does not take a date')
    else:
        text = str(value)

    if any(ord(ch) > 126 or ord(ch) < 32 for ch in text):
        raise ValueError(f'{item.key} must be printable ASCII: {text!r}')
    if len(text) > item.length:
        raise ValueError(f'{item.key} takes at most {item.length} characters: {text!r}')
    if kind in _INTEGER_KINDS and text.strip().isdigit():
        return text.strip().zfill(item.length)
    if item.justify == 'right' and kind != 'accuracy':
        return text.rjust(item.length)
    return text.ljust(item.length)


@dataclass
class AccSubregion:
    """One accuracy subregion: four accuracies and an outline of 3 to 14
    coordinate pairs, all kept as raw field text."""

    abs_horiz_acc: str = NA_VALUE
    abs_vert_acc: str = NA_VALUE
    rel_horiz_acc: str = NA_VALUE
    rel_vert_acc: str = NA_VALUE
    coord_count: str = '00'
    coordinates: list[tuple[str, str]] = dc_field(default_factory=list)

    @classmethod
    def from_values(
        cls,
        abs_horiz_acc: int | str | None,
        abs_vert_acc: int | str | None,
        rel_horiz_acc: int | str | None,
        rel_vert_acc: int | str | None,
        outline: list[tuple[float, float]],
    ) -> AccSubregion:
        """Build a subregion from meters (or NA) and an outline of (lat, lon) degrees."""
        if not schema.MIN_COORDINATES <= len(outline) <= MAX_COORDINATES:
            raise ValueError(f'A subregion outline has 3 to 14 coordinate pairs, not {len(outline)}')
        return cls(
            format_accuracy(abs_horiz_acc), format_accuracy(abs_vert_acc),
            format_accuracy(rel_horiz_acc), format_accuracy(rel_vert_acc),
            f'{len(outline):02d}',
            [(format_dms(lat, 'dsi_lat'), format_dms(lon, 'dsi_lon')) for lat, lon in outline],
        )

    @classmethod
    def decode(cls, text: str) -> AccSubregion:
        """Decode the 284 characters of a subregion; coordinate pairs stop at the first blank one."""
        if len(text) != SUBREGION_LENGTH:
            raise ValueError(f'A subregion is {SUBREGION_LENGTH} characters, not {len(text)}')
        values = [text[item.offset:item.offset + item.length] for item in SUBREGION_FIELDS]
        coordinates = []
        for i in range(MAX_COORDINATES):
            start = SUBREGION_COORDS_START - 1 + i * COORDINATE_LENGTH
            pair = text[start:start + COORDINATE_LENGTH]
            if pair.strip() == '':
                break
            coordinates.append((pair[:9], pair[9:]))
        return cls(*values, coordinates)

    def encode(self) -> str:
        """The 284 characters of the subregion, unused pairs blank."""
        text = ''.join(
            getattr(self, item.key).ljust(item.length)[:item.length] for item in SUBREGION_FIELDS
        )
        for lat, lon in self.coordinates:
            text += lat.ljust(9)[:9] + lon.ljust(10)[:10]
        return text.ljust(SUBREGION_LENGTH)

    @property
    def is_blank(self) -> bool:
        """True when the slot shows nothing: blanks, or bytes that are not printable."""
        return not has_visible_text(self.encode())

    def decoded_outline(self) -> list[tuple[float | None, float | None]]:
        """The outline in signed decimal degrees; None where a value is malformed."""
        return [(parse_dms(lat, 'dsi_lat'), parse_dms(lon, 'dsi_lon')) for lat, lon in self.coordinates]

    def accuracies(self) -> dict[str, int | None]:
        """The four accuracies in meters (None for NA)."""
        return {item.key: decode_accuracy(getattr(self, item.key)) for item in SUBREGION_FIELDS[:4]}


class DtedHeader:
    """The three header records as raw fixed-width text, keyed by schema field."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values: dict[str, str] = {item.key: ' ' * item.length for item in ALL_FIELDS}
        for key, raw in (values or {}).items():
            self.set_raw(key, raw)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __eq__(self, other) -> bool:
        return isinstance(other, DtedHeader) and self.values == other.values

    def __repr__(self) -> str:
        return f'DtedHeader({self.cell_id or "?"}, level {self.detect_level().level})'

    def copy(self) -> DtedHeader:
        return DtedHeader(dict(self.values))

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def stripped(self, key: str) -> str:
        """The field without its padding blanks."""
        return self.values[key].strip()

    def set_raw(self, key: str, raw: str) -> None:
        """Store *raw* as the field's text; it must have the field's exact length."""
        item = FIELDS_BY_KEY[key]
        if len(raw) != item.length:
            raise ValueError(f'{key} takes exactly {item.length} characters, got {len(raw)}')
        self.values[key] = raw

    def set(self, key: str, value) -> None:
        """Store a typed value, formatted for the field."""
        self.values[key] = format_field(FIELDS_BY_KEY[key], value)

    def record(self, name: str) -> str:
        """The text of one record (UHL, DSI or ACC)."""
        return ''.join(self.values[item.key] for item in RECORD_FIELDS[name])

    # Typed views

    def accuracy(self, key: str) -> int | None:
        """An accuracy field in meters; None for NA. Raises ValueError when malformed."""
        return decode_accuracy(self.values[key])

    @property
    def origin(self) -> tuple[float | None, float | None]:
        """(lon, lat) of the southwest corner from the UHL, None where malformed."""
        return parse_dms(self.values['uhl.origin_lon'], 'uhl_lon'), parse_dms(self.values['uhl.origin_lat'], 'uhl_lat')

    @property
    def cell_id(self) -> str | None:
        lon, lat = self.origin
        if lon is None or lat is None:
            return None
        return cell_id(int(round(lon)), int(round(lat)))

    def interval_tenths(self, key: str) -> int | None:
        text = self.values[key]
        return int(text) if text.isdigit() else None

    def outline_count(self) -> int:
        """The number of subregions announced by the ACC outline flag (0 when 00 or malformed)."""
        text = self.values['acc.outline_flag']
        return int(text) if text.isdigit() and text != '00' else 0

    def subregions(self, announced_only: bool = True) -> list[AccSubregion]:
        """The decoded accuracy subregions.

        With *announced_only* the number comes from the outline flag, as the
        spec defines it; otherwise every subregion slot that is not blank is
        returned, which is what the validator compares against the flag.
        """
        block = self.values['acc.subregions']
        count = self.outline_count() if announced_only else MAX_SUBREGIONS
        found = []
        for i in range(count):
            subregion = AccSubregion.decode(block[i * SUBREGION_LENGTH:(i + 1) * SUBREGION_LENGTH])
            if announced_only or not subregion.is_blank:
                found.append(subregion)
        return found

    def set_subregions(self, subregions: list[AccSubregion]) -> None:
        """Encode *subregions* into the ACC block and set both multiple-accuracy flags."""
        if len(subregions) > MAX_SUBREGIONS:
            raise ValueError(f'At most {MAX_SUBREGIONS} accuracy subregions, not {len(subregions)}')
        if len(subregions) == 1:
            raise ValueError('The outline flag allows 00 or 02-09 subregions, not one')
        block = ''.join(s.encode() for s in subregions)
        self.set_raw('acc.subregions', block.ljust(FIELDS_BY_KEY['acc.subregions'].length))
        self.set_raw('acc.outline_flag', f'{len(subregions):02d}')
        self.set_raw('uhl.multiple_accuracy', '1' if subregions else '0')

    def detect_level(self, extension: str | None = None) -> LevelDetection:
        """What the extension, the DSI series, the UHL latitude interval and the
        UHL latitude point count each say the level is."""
        by_extension = LEVEL_BY_EXTENSION.get((extension or '').lower()) if extension else None
        by_series = LEVEL_BY_SERIES.get(self.stripped('dsi.series'))
        by_interval = LEVEL_BY_LAT_INTERVAL.get(self.interval_tenths('uhl.lat_interval'))
        by_points = LEVEL_BY_LAT_POINTS.get(self.interval_tenths('uhl.lat_points'))
        return LevelDetection(
            by_extension.number if by_extension else None,
            by_series.number if by_series else None,
            by_interval.number if by_interval else None,
            by_points.number if by_points else None,
        )

    def to_dict(self) -> dict[str, str]:
        return dict(self.values)


@dataclass(frozen=True)
class LevelDetection:
    """The level each source of evidence points to (None when it says nothing)."""

    by_extension: int | None
    by_series: int | None
    by_lat_interval: int | None
    by_lat_points: int | None

    @property
    def votes(self) -> dict[str, int | None]:
        return {
            'extension': self.by_extension,
            'DSI series': self.by_series,
            'UHL latitude interval': self.by_lat_interval,
            'UHL latitude points': self.by_lat_points,
        }

    @property
    def distinct(self) -> set[int]:
        return {v for v in self.votes.values() if v is not None}

    @property
    def consensus(self) -> int | None:
        """The level when every source that says anything agrees; otherwise None."""
        return next(iter(self.distinct)) if len(self.distinct) == 1 else None

    @property
    def conflict(self) -> bool:
        return len(self.distinct) > 1

    @property
    def level(self) -> int | None:
        """The best estimate: the consensus, else what the post grid itself says."""
        if self.consensus is not None:
            return self.consensus
        for vote in (self.by_lat_interval, self.by_lat_points, self.by_series, self.by_extension):
            if vote is not None:
                return vote
        return None


@dataclass(frozen=True)
class CellGeometry:
    """A one-degree cell at a DTED level: its post grid and every header field
    that follows from it."""

    level: int
    lon0: int
    lat0: int

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f'DTED level must be 0, 1 or 2, not {self.level}')
        if not -180 <= self.lon0 <= 179 or not -90 <= self.lat0 <= 89:
            raise ValueError(f'Cell origin out of range: lon {self.lon0}, lat {self.lat0}')

    @property
    def cell_id(self) -> str:
        return cell_id(self.lon0, self.lat0)

    @property
    def zone(self) -> int:
        return zone_for(self.lat0)[0]

    @property
    def zone_multiplier(self) -> int:
        return zone_for(self.lat0)[1]

    @property
    def lat_interval_tenths(self) -> int:
        return LEVELS[self.level].lat_interval_tenths

    @property
    def lon_interval_tenths(self) -> int:
        return self.lat_interval_tenths * self.zone_multiplier

    @property
    def lat_points(self) -> int:
        return LEVELS[self.level].lat_points

    @property
    def lon_lines(self) -> int:
        return 36000 // self.lon_interval_tenths + 1

    @property
    def series(self) -> str:
        return LEVELS[self.level].series

    @property
    def geotransform(self) -> tuple[float, float, float, float, float, float]:
        """The GDAL geotransform of the cell's post grid, as GDAL's DTED driver
        computes it: posts are pixel centers, so the raster's edges lie half a
        post beyond the cell. The arithmetic is the driver's, in its order, so
        the value is the one GDAL reports for the file."""
        px = self.lon_interval_tenths / 36000.0
        py = self.lat_interval_tenths / 36000.0
        return (self.lon0 - 0.5 * px, px, 0.0, self.lat0 - 0.5 * py + self.lat_points * py, 0.0, -py)

    def header_values(self) -> dict[str, str]:
        """Raw text of every header field that the cell geometry determines."""
        lon0, lat0, lon1, lat1 = self.lon0, self.lat0, self.lon0 + 1, self.lat0 + 1
        return {
            'uhl.origin_lon': format_dms(lon0, 'uhl_lon'),
            'uhl.origin_lat': format_dms(lat0, 'uhl_lat'),
            'uhl.lon_interval': f'{self.lon_interval_tenths:04d}',
            'uhl.lat_interval': f'{self.lat_interval_tenths:04d}',
            'uhl.lon_lines': f'{self.lon_lines:04d}',
            'uhl.lat_points': f'{self.lat_points:04d}',
            'dsi.series': self.series,
            'dsi.origin_lat': format_dms(lat0, 'dsi_lat'),
            'dsi.origin_lon': format_dms(lon0, 'dsi_lon'),
            'dsi.sw_lat': format_dms(lat0, 'corner_lat'),
            'dsi.sw_lon': format_dms(lon0, 'corner_lon'),
            'dsi.nw_lat': format_dms(lat1, 'corner_lat'),
            'dsi.nw_lon': format_dms(lon0, 'corner_lon'),
            'dsi.ne_lat': format_dms(lat1, 'corner_lat'),
            'dsi.ne_lon': format_dms(lon1, 'corner_lon'),
            'dsi.se_lat': format_dms(lat0, 'corner_lat'),
            'dsi.se_lon': format_dms(lon1, 'corner_lon'),
            'dsi.orientation': '0000000.0',
            'dsi.lat_interval': f'{self.lat_interval_tenths:04d}',
            'dsi.lon_interval': f'{self.lon_interval_tenths:04d}',
            'dsi.lat_lines': f'{self.lat_points:04d}',
            'dsi.lon_lines': f'{self.lon_lines:04d}',
        }


def detect_level(header: DtedHeader, extension: str | None = None) -> LevelDetection:
    """What the extension, the DSI series and the UHL grid say the level of *header* is."""
    return header.detect_level(extension)


def cell_geometry(lon0: int, lat0: int, level: int) -> CellGeometry:
    """The geometry of the cell whose southwest corner is (lon0, lat0) at *level*."""
    return CellGeometry(level, int(lon0), int(lat0))


def cell_geometry_of(header: DtedHeader, extension: str | None = None) -> CellGeometry | None:
    """The cell geometry a header describes, or None when its origin or level cannot be read."""
    lon, lat = header.origin
    level = header.detect_level(extension).level
    if lon is None or lat is None or level is None:
        return None
    return CellGeometry(level, int(round(lon)), int(round(lat)))


def new_header(cell: CellGeometry) -> DtedHeader:
    """A header with the sentinels, fixed values, spec fills and the fields the
    cell geometry determines; everything else blank. The vertical datum, the
    security codes, the producer and the accuracies are left for the caller."""
    header = DtedHeader()
    for item in ALL_FIELDS:
        if item.fixed is not None:
            header.set_raw(item.key, item.fixed.ljust(item.length))
        elif item.kind in ('date', 'maint_code', 'outline_flag', 'partial', 'flag1', 'accuracy'):
            header.set(item.key, None)
    header.set('dsi.product_spec', schema.PRODUCT_SPEC)
    header.set('dsi.product_spec_amend', schema.PRODUCT_SPEC_AMENDMENT)
    header.set('dsi.product_spec_date', schema.PRODUCT_SPEC_DATE)
    for key, raw in cell.header_values().items():
        header.set_raw(key, raw)
    return header


def parse_header(buffer: bytes) -> DtedHeader:
    """Decode the first 3,428 bytes of a DTED file."""
    if len(buffer) < HEADER_LENGTH:
        raise ValueError(f'A DTED header is {HEADER_LENGTH} bytes; got {len(buffer)}')
    text = buffer[:HEADER_LENGTH].decode(ENCODING)
    header = DtedHeader()
    for item in ALL_FIELDS:
        header.values[item.key] = text[item.file_offset:item.file_offset + item.length]
    return header


def encode_header(header: DtedHeader) -> bytes:
    """The 3,428 bytes of the header."""
    text = header.record('UHL') + header.record('DSI') + header.record('ACC')
    if len(text) != HEADER_LENGTH:
        raise ValueError(f'Header text is {len(text)} characters, not {HEADER_LENGTH}')
    return text.encode(ENCODING)


def read_header(path: str) -> DtedHeader:
    """The header of the DTED file at *path*, read from raw bytes."""
    with open(path, 'rb') as handle:
        buffer = handle.read(HEADER_LENGTH)
    if len(buffer) < HEADER_LENGTH:
        raise ValueError(f'{os.path.basename(path)} is too short for a DTED header ({len(buffer)} bytes)')
    return parse_header(buffer)


def write_header(path: str, header: DtedHeader) -> None:
    """Overwrite the header of the DTED file at *path* in place.

    The file must already hold a header (at least 3,428 bytes); if elevation
    records follow, the first must start with the 0xAA sentinel, so a file
    that is not DTED cannot be damaged by mistake.
    """
    size = os.path.getsize(path)
    if size < HEADER_LENGTH:
        raise ValueError(f'{os.path.basename(path)} is too short for a DTED header ({size} bytes)')
    with open(path, 'r+b') as handle:
        if size > HEADER_LENGTH:
            handle.seek(HEADER_LENGTH)
            sentinel = handle.read(1)
            if sentinel != bytes([DATA_RECORD_SENTINEL]):
                raise ValueError(
                    f'{os.path.basename(path)} has no DTED data record after the header '
                    f'(byte {HEADER_LENGTH} is {sentinel.hex()}, not aa); refusing to overwrite'
                )
        handle.seek(0)
        handle.write(encode_header(header))


def changed_fields(before: DtedHeader, after: DtedHeader) -> list[tuple[str, str, str]]:
    """(key, old raw, new raw) for every field that differs between two headers."""
    return [
        (key, before.values[key], after.values[key])
        for key in before.values if before.values[key] != after.values[key]
    ]


def coordinate_field(index: int) -> Field:
    """The schema field of one half of a coordinate pair: 0 latitude, 1 longitude."""
    return COORDINATE_FIELDS[index]
