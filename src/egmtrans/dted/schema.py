"""The MIL-PRF-89020B header schema: the single source of truth for the User
Header Label (UHL), Data Set Identification (DSI) and Accuracy Description
(ACC) records of a DTED file.

Byte positions are the specification's own 1-based "Character Start" column,
so a row of the report can be checked against the spec tables as printed.
Section references ("3.12 c") point into MIL-PRF-89020B, 23 May 2000.

Spec conventions that the codec and the validator enforce (3.13.4, 3.13.5):
numeric values are right justified with leading zeros; alpha and alphanumeric
values, "NA" included, are left justified; unused positions are blank filled.
"""

from __future__ import annotations

from dataclasses import dataclass

UHL_LENGTH = 80
DSI_LENGTH = 648
ACC_LENGTH = 2700
HEADER_LENGTH = UHL_LENGTH + DSI_LENGTH + ACC_LENGTH  # 3428

RECORD_LENGTHS = {'UHL': UHL_LENGTH, 'DSI': DSI_LENGTH, 'ACC': ACC_LENGTH}
# File offset (0-based) of each record.
RECORD_OFFSETS = {'UHL': 0, 'DSI': UHL_LENGTH, 'ACC': UHL_LENGTH + DSI_LENGTH}
RECORD_TITLES = {
    'UHL': 'User Header Label',
    'DSI': 'Data Set Identification',
    'ACC': 'Accuracy Description',
}

DATA_RECORD_SENTINEL = 0xAA
DATA_RECORD_OVERHEAD = 12  # sentinel (1) + block count (3) + lon count (2) + lat count (2) + checksum (4)
NULL_ELEVATION = -32767
# The data record notes: the 16-bit signed-magnitude value allows +/-32,767 m,
# "however in practice, the terrain elevation values shall not exceed
# +9,000 meters or -12,000 meters".
ELEVATION_MIN = -12000
ELEVATION_MAX = 9000

# Accuracy subregions inside the ACC record (3.13.5.1): up to nine, 284
# characters each, starting at character 58; the last 87 characters are reserved.
SUBREGION_START = 58
SUBREGION_LENGTH = 284
MAX_SUBREGIONS = 9
MAX_COORDINATES = 14
MIN_COORDINATES = 3
COORDINATE_LENGTH = 19  # latitude DDMMSS.SH (9) + longitude DDDMMSS.SH (10)
SUBREGION_COORDS_START = 19  # 1-based within a subregion

# "NA" is alpha, so it is left justified in its 4-character field.
NA_VALUE = 'NA  '

SECURITY_CODES = ('U', 'R', 'C', 'S')
SECURITY_CODE_NAMES = {'U': 'Unclassified', 'R': 'Restricted', 'C': 'Confidential', 'S': 'Secret'}
VERTICAL_DATUM_CODES = ('MSL', 'E96')  # 3.13.4.1 m; E08 is common practice but not in the spec
VERTICAL_DATUM_CODES_ACCEPTED = ('MSL', 'E96', 'E08')
HORIZONTAL_DATUM_CODE = 'WGS84'
PRODUCT_SPEC = 'PRF89020B'
PRODUCT_SPEC_AMENDMENT = '00'
PRODUCT_SPEC_DATE = '0005'  # May 2000, YYMM


@dataclass(frozen=True)
class Level:
    """A DTED level: its latitude interval and post count for a full cell."""

    number: int
    lat_interval_tenths: int
    lat_points: int
    series: str
    extension: str


LEVELS: dict[int, Level] = {
    0: Level(0, 300, 121, 'DTED0', '.dt0'),
    1: Level(1, 30, 1201, 'DTED1', '.dt1'),
    2: Level(2, 10, 3601, 'DTED2', '.dt2'),
}
LEVEL_BY_SERIES = {level.series: level for level in LEVELS.values()}
LEVEL_BY_EXTENSION = {level.extension: level for level in LEVELS.values()}
LEVEL_BY_LAT_INTERVAL = {level.lat_interval_tenths: level for level in LEVELS.values()}
LEVEL_BY_LAT_POINTS = {level.lat_points: level for level in LEVELS.values()}

# Longitude spacing zones (Tables I to III): (upper latitude limit, multiplier
# of the latitude interval). The zone of a cell is decided by the latitude of
# its edge nearer the equator.
ZONES: tuple[tuple[int, int], ...] = ((50, 1), (70, 2), (75, 3), (80, 4), (90, 6))
ZONE_NAMES = ('I', 'II', 'III', 'IV', 'V')

# Two-digit years in YYMM dates: the spec is from 2000, DTED compilations go
# back to the 1980s, so 80-99 are 1980-1999 and 00-79 are 2000-2079.
CENTURY_PIVOT = 80


@dataclass(frozen=True)
class Field:
    """One field of a header record.

    *start* is 1-based within the record, as in the specification. *kind*
    selects the decoder and the validation rule; *pattern* is the regular
    expression a well-formed value matches (None for free text); *fixed* is a
    value the spec prescribes.
    """

    key: str
    record: str
    start: int
    length: int
    title: str
    description: str
    kind: str = 'text'
    justify: str = 'left'
    pattern: str | None = None
    fixed: str | None = None
    spec: str = ''

    @property
    def end(self) -> int:
        """1-based position of the last character."""
        return self.start + self.length - 1

    @property
    def offset(self) -> int:
        """0-based offset within the record."""
        return self.start - 1

    @property
    def file_offset(self) -> int:
        """0-based offset within the file."""
        return RECORD_OFFSETS[self.record] + self.offset


# Patterns shared by several fields.
_DIGITS4 = r'^\d{4}$'
_ACCURACY = r'^(\d{4}|NA  |  NA)$'
_YYMM = r'^(\d{2}(0[1-9]|1[0-2])|0000)$'

UHL_FIELDS: tuple[Field, ...] = (
    Field('uhl.sentinel', 'UHL', 1, 3, 'Recognition sentinel', 'Always "UHL".',
          kind='sentinel', pattern=r'^UHL$', fixed='UHL', spec='3.12 c'),
    Field('uhl.fixed', 'UHL', 4, 1, 'Fixed by standard', 'Always "1".',
          kind='fixed', pattern=r'^1$', fixed='1', spec='3.12 c'),
    Field('uhl.origin_lon', 'UHL', 5, 8, 'Longitude of origin',
          'DDDMMSSH, southwest corner of the cell; always a full degree; H is E or W.',
          kind='uhl_lon', pattern=r'^\d{7}[EW]$', spec='3.12 c, 3.13.3.1 a'),
    Field('uhl.origin_lat', 'UHL', 13, 8, 'Latitude of origin',
          'DDDMMSSH, southwest corner of the cell; always a full degree; H is N or S.',
          kind='uhl_lat', pattern=r'^\d{7}[NS]$', spec='3.12 c, 3.13.3.1 b'),
    Field('uhl.lon_interval', 'UHL', 21, 4, 'Longitude data interval',
          'Tenths of arc seconds between columns of posts; depends on the level and the latitude zone '
          '(Tables I to III).', kind='interval', justify='right', pattern=_DIGITS4, spec='3.12 c, 3.13.3.1 c'),
    Field('uhl.lat_interval', 'UHL', 25, 4, 'Latitude data interval',
          'Tenths of arc seconds between rows of posts: 0300 for level 0, 0030 for level 1, 0010 for level 2.',
          kind='interval', justify='right', pattern=_DIGITS4, spec='3.12 c, 3.13.3.1 d'),
    Field('uhl.abs_vert_acc', 'UHL', 29, 4, 'Absolute vertical accuracy (LE90, m)',
          '90% linear error relative to mean sea level, in meters: 0000-9999, or NA when not available. '
          'Repeats the ACC absolute vertical accuracy.',
          kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 c, 3.3'),
    Field('uhl.security_code', 'UHL', 33, 3, 'Security code',
          'S Secret, C Confidential, U Unclassified, R Restricted; left justified, blank filled.',
          kind='security3', pattern=r'^[URCS]  $', spec='3.12 c'),
    Field('uhl.unique_ref', 'UHL', 36, 12, 'Unique reference number',
          'Defined by the producer; may be blank.', kind='text', spec='3.12 c'),
    Field('uhl.lon_lines', 'UHL', 48, 4, 'Number of longitude lines',
          'Count of longitude lines (profiles) for a full cell: 36000 / longitude interval + 1.',
          kind='count', justify='right', pattern=_DIGITS4, spec='3.12 c, 3.13.3.1 e'),
    Field('uhl.lat_points', 'UHL', 52, 4, 'Number of latitude points',
          'Count of posts per longitude line for a full cell: 121 for level 0, 1201 for level 1, 3601 for level 2.',
          kind='count', justify='right', pattern=_DIGITS4, spec='3.12 c, 3.13.3.1 f'),
    Field('uhl.multiple_accuracy', 'UHL', 56, 1, 'Multiple accuracy',
          '0 single accuracy for the cell, 1 accuracy subregions in the ACC record.',
          kind='flag1', pattern=r'^[01]$', spec='3.12 c'),
    Field('uhl.reserved', 'UHL', 57, 24, 'Reserved', 'Unused, for future use; blank filled.',
          kind='reserved', spec='3.12 c'),
)

DSI_FIELDS: tuple[Field, ...] = (
    Field('dsi.sentinel', 'DSI', 1, 3, 'Recognition sentinel', 'Always "DSI".',
          kind='sentinel', pattern=r'^DSI$', fixed='DSI', spec='3.12 d'),
    Field('dsi.security_code', 'DSI', 4, 1, 'Security classification code',
          'S Secret, C Confidential, U Unclassified, R Restricted.',
          kind='security1', pattern=r'^[URCS]$', spec='3.12 d'),
    Field('dsi.security_control', 'DSI', 5, 2, 'Security control and release markings',
          'Two-character code for DoD use, or blank.', kind='text', spec='3.12 d, 3.13.4.1 a'),
    Field('dsi.security_handling', 'DSI', 7, 27, 'Security handling description',
          'Other security description; free text or blank.', kind='text', spec='3.12 d'),
    Field('dsi.reserved_1', 'DSI', 34, 26, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 d'),
    Field('dsi.series', 'DSI', 60, 5, 'NIMA series designator',
          'DTED0, DTED1 or DTED2 for the product level.',
          kind='series', pattern=r'^DTED[012]$', spec='3.12 d, 3.13.4.1 b'),
    Field('dsi.unique_ref', 'DSI', 65, 15, 'Unique reference number',
          'For the producing nation\'s own use; free text or zero filled.', kind='text',
          spec='3.12 d, 3.13.4.1 c'),
    Field('dsi.reserved_2', 'DSI', 80, 8, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 d'),
    Field('dsi.data_edition', 'DSI', 88, 2, 'Data edition number',
          '01-99: 01 for the original compilation, higher for recompilations and revisions.',
          kind='edition', justify='right', pattern=r'^\d{2}$', spec='3.12 d, 3.13.4.1 d'),
    Field('dsi.match_merge_version', 'DSI', 90, 1, 'Match/merge version',
          'A for the original release of the edition, B-Z for each change made for boundary continuity.',
          kind='version', pattern=r'^[A-Z]$', spec='3.12 d, 3.13.4.1 e'),
    Field('dsi.maintenance_date', 'DSI', 91, 4, 'Maintenance date',
          'YYMM of the last revision or recompilation; 0000 until used.',
          kind='date', justify='right', pattern=_YYMM, spec='3.12 d, 3.13.4.1 f'),
    Field('dsi.match_merge_date', 'DSI', 95, 4, 'Match/merge date',
          'YYMM of the last change for boundary continuity; 0000 until used.',
          kind='date', justify='right', pattern=_YYMM, spec='3.12 d, 3.13.4.1 g'),
    Field('dsi.maintenance_code', 'DSI', 99, 4, 'Maintenance description code',
          '0000 until used, otherwise ANNN.', kind='maint_code', pattern=r'^(0000|[A-Z]\d{3})$',
          spec='3.12 d, 3.13.4.1 h'),
    Field('dsi.producer_code', 'DSI', 103, 8, 'Producer code',
          'First two characters are the producing nation (FIPS 10-4, e.g. US), the last six are free.',
          kind='text', spec='3.12 d, 3.13.4.1 i'),
    Field('dsi.reserved_3', 'DSI', 111, 16, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 d'),
    Field('dsi.product_spec', 'DSI', 127, 9, 'Product specification',
          'Specification the data were produced to; PRF89020B for this one.',
          kind='text', spec='3.12 d, 3.13.4.1 j'),
    Field('dsi.product_spec_amend', 'DSI', 136, 2, 'Product specification amendment and change',
          'First digit the amendment number, second the change notice number.',
          kind='spec_amend', justify='right', pattern=r'^\d{2}$', spec='3.12 d, 3.13.4.1 k'),
    Field('dsi.product_spec_date', 'DSI', 138, 4, 'Product specification date',
          'YYMM the specification was published (0005 for MIL-PRF-89020B).',
          kind='date', justify='right', pattern=_YYMM, spec='3.12 d, 3.13.4.1 l'),
    Field('dsi.vertical_datum', 'DSI', 142, 3, 'Vertical datum code',
          'MSL or E96 (EGM96) in the spec; E08 (EGM2008) is used in practice.',
          kind='vdatum', pattern=r'^(MSL|E96|E08)$', spec='3.12 d, 3.13.4.1 m'),
    Field('dsi.horizontal_datum', 'DSI', 145, 5, 'Horizontal datum code',
          'WGS84: the version of WGS the product is controlled to.',
          kind='hdatum', pattern=r'^WGS84$', fixed='WGS84', spec='3.12 d, 3.13.4.1 n'),
    Field('dsi.digitizing_system', 'DSI', 150, 10, 'Digitizing/collection system',
          'Equipment or source used to collect the elevations; free text.', kind='text',
          spec='3.12 d, 3.13.4.1 o'),
    Field('dsi.compilation_date', 'DSI', 160, 4, 'Compilation date',
          'YYMM of the original compilation or of the last major recompilation.',
          kind='date', justify='right', pattern=_YYMM, spec='3.12 d, 3.13.4.1 p'),
    Field('dsi.reserved_4', 'DSI', 164, 22, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 d'),
    Field('dsi.origin_lat', 'DSI', 186, 9, 'Latitude of origin',
          'DDMMSS.SH, southwest corner; always a full degree; H is N or S.',
          kind='dsi_lat', pattern=r'^\d{6}\.\d[NS]$', spec='3.12 d, 3.13.4.1 q'),
    Field('dsi.origin_lon', 'DSI', 195, 10, 'Longitude of origin',
          'DDDMMSS.SH, southwest corner; always a full degree; H is E or W.',
          kind='dsi_lon', pattern=r'^\d{7}\.\d[EW]$', spec='3.12 d, 3.13.4.1 r'),
    Field('dsi.sw_lat', 'DSI', 205, 7, 'Latitude of SW corner', 'DDMMSSH of the bounding rectangle.',
          kind='corner_lat', pattern=r'^\d{6}[NS]$', spec='3.12 d'),
    Field('dsi.sw_lon', 'DSI', 212, 8, 'Longitude of SW corner', 'DDDMMSSH of the bounding rectangle.',
          kind='corner_lon', pattern=r'^\d{7}[EW]$', spec='3.12 d'),
    Field('dsi.nw_lat', 'DSI', 220, 7, 'Latitude of NW corner', 'DDMMSSH of the bounding rectangle.',
          kind='corner_lat', pattern=r'^\d{6}[NS]$', spec='3.12 d'),
    Field('dsi.nw_lon', 'DSI', 227, 8, 'Longitude of NW corner', 'DDDMMSSH of the bounding rectangle.',
          kind='corner_lon', pattern=r'^\d{7}[EW]$', spec='3.12 d'),
    Field('dsi.ne_lat', 'DSI', 235, 7, 'Latitude of NE corner', 'DDMMSSH of the bounding rectangle.',
          kind='corner_lat', pattern=r'^\d{6}[NS]$', spec='3.12 d'),
    Field('dsi.ne_lon', 'DSI', 242, 8, 'Longitude of NE corner', 'DDDMMSSH of the bounding rectangle.',
          kind='corner_lon', pattern=r'^\d{7}[EW]$', spec='3.12 d'),
    Field('dsi.se_lat', 'DSI', 250, 7, 'Latitude of SE corner', 'DDMMSSH of the bounding rectangle.',
          kind='corner_lat', pattern=r'^\d{6}[NS]$', spec='3.12 d'),
    Field('dsi.se_lon', 'DSI', 257, 8, 'Longitude of SE corner', 'DDDMMSSH of the bounding rectangle.',
          kind='corner_lon', pattern=r'^\d{7}[EW]$', spec='3.12 d'),
    Field('dsi.orientation', 'DSI', 265, 9, 'Clockwise orientation angle',
          'DDDMMSS.S with respect to true north; all zeros for a DTED cell.',
          kind='angle', pattern=r'^\d{7}\.\d$', fixed='0000000.0', spec='3.12 d'),
    Field('dsi.lat_interval', 'DSI', 274, 4, 'Latitude interval',
          'Tenths of arc seconds between rows; repeats the UHL value.',
          kind='interval', justify='right', pattern=_DIGITS4, spec='3.12 d'),
    Field('dsi.lon_interval', 'DSI', 278, 4, 'Longitude interval',
          'Tenths of arc seconds between columns; repeats the UHL value.',
          kind='interval', justify='right', pattern=_DIGITS4, spec='3.12 d'),
    Field('dsi.lat_lines', 'DSI', 282, 4, 'Number of latitude lines',
          'Posts per longitude line for a full cell (1201 for DTED1, 3601 for DTED2).',
          kind='count', justify='right', pattern=_DIGITS4, spec='3.12 d, 3.13.4.1 s'),
    Field('dsi.lon_lines', 'DSI', 286, 4, 'Number of longitude lines',
          'Longitude lines for a full cell, by level and latitude zone (Tables II and III).',
          kind='count', justify='right', pattern=_DIGITS4, spec='3.12 d, 3.13.4.1 t'),
    Field('dsi.partial_cell', 'DSI', 290, 2, 'Partial cell indicator',
          '00 for a complete cell, 01-99 the percentage of the cell with data.',
          kind='partial', justify='right', pattern=r'^\d{2}$', spec='3.12 d, 3.11.3'),
    Field('dsi.nima_text', 'DSI', 292, 101, 'Reserved for NIMA use', 'Free text or blank filled.',
          kind='text', spec='3.12 d'),
    Field('dsi.producer_text', 'DSI', 393, 100, 'Reserved for producing nation use',
          'Free text or blank filled.', kind='text', spec='3.12 d'),
    Field('dsi.free_text', 'DSI', 493, 156, 'Free text comments', 'Free text or blank filled.',
          kind='text', spec='3.12 d'),
)

ACC_FIELDS: tuple[Field, ...] = (
    Field('acc.sentinel', 'ACC', 1, 3, 'Recognition sentinel', 'Always "ACC".',
          kind='sentinel', pattern=r'^ACC$', fixed='ACC', spec='3.12 e'),
    Field('acc.abs_horiz_acc', 'ACC', 4, 4, 'Absolute horizontal accuracy (CE90, m)',
          '90% circular error of the product on WGS 84, in meters: 0000-9999, or NA when not available.',
          kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e, 3.3'),
    Field('acc.abs_vert_acc', 'ACC', 8, 4, 'Absolute vertical accuracy (LE90, m)',
          '90% linear error of the product relative to mean sea level, in meters: 0000-9999, or NA.',
          kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e, 3.3'),
    Field('acc.rel_horiz_acc', 'ACC', 12, 4, 'Point-to-point horizontal accuracy (CE90, m)',
          'Relative (point-to-point) 90% circular error of the product, in meters: 0000-9999, or NA.',
          kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e, 3.3'),
    Field('acc.rel_vert_acc', 'ACC', 16, 4, 'Point-to-point vertical accuracy (LE90, m)',
          'Relative (point-to-point) 90% linear error of the product, in meters: 0000-9999, or NA.',
          kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e, 3.3'),
    Field('acc.reserved_1', 'ACC', 20, 4, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 e'),
    Field('acc.nima_reserved', 'ACC', 24, 1, 'Reserved for NIMA use',
          'One character for NIMA use ("X" in SRTM products, 3.10.9.3); otherwise blank.',
          kind='text', spec='3.12 e'),
    Field('acc.reserved_2', 'ACC', 25, 31, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 e'),
    Field('acc.outline_flag', 'ACC', 56, 2, 'Multiple accuracy outline flag',
          '00 no accuracy subregions, 02-09 the number of subregions in the cell (maximum 9).',
          kind='outline_flag', justify='right', pattern=r'^(00|0[2-9])$', spec='3.12 e'),
    Field('acc.subregions', 'ACC', 58, 2556, 'Accuracy subregion descriptions',
          'Up to nine subregions of 284 characters each; unused subregions and coordinate pairs are blank.',
          kind='subregions', spec='3.12 e, 3.13.5.1'),
    Field('acc.nima_trailer', 'ACC', 2614, 18, 'Reserved for NIMA use', 'Blank unless used by NIMA.',
          kind='text', spec='3.12 e'),
    Field('acc.reserved_trailer', 'ACC', 2632, 69, 'Reserved', 'For future use; blank filled.',
          kind='reserved', spec='3.12 e'),
)

# Fields of one accuracy subregion; *start* is 1-based within the subregion.
SUBREGION_FIELDS: tuple[Field, ...] = (
    Field('abs_horiz_acc', 'ACC', 1, 4, 'Absolute horizontal accuracy of subregion (CE90, m)',
          '0000-9999 or NA.', kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e'),
    Field('abs_vert_acc', 'ACC', 5, 4, 'Absolute vertical accuracy of subregion (LE90, m)',
          '0000-9999 or NA.', kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e'),
    Field('rel_horiz_acc', 'ACC', 9, 4, 'Point-to-point horizontal accuracy of subregion (CE90, m)',
          '0000-9999 or NA.', kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e'),
    Field('rel_vert_acc', 'ACC', 13, 4, 'Point-to-point vertical accuracy of subregion (LE90, m)',
          '0000-9999 or NA.', kind='accuracy', justify='right', pattern=_ACCURACY, spec='3.12 e'),
    Field('coord_count', 'ACC', 17, 2, 'Number of coordinates in subregion outline',
          '03-14 coordinate pairs; the first is the most southwestern, the rest follow clockwise, '
          'closure is implied.', kind='coord_count', justify='right', pattern=r'^(0[3-9]|1[0-4])$',
          spec='3.12 e, 3.13.5.1'),
)
# One coordinate pair; *start* is 1-based within the pair.
COORDINATE_FIELDS: tuple[Field, ...] = (
    Field('lat', 'ACC', 1, 9, 'Latitude', 'DDMMSS.SH; H is N or S.',
          kind='dsi_lat', pattern=r'^\d{6}\.\d[NS]$', spec='3.12 e'),
    Field('lon', 'ACC', 10, 10, 'Longitude', 'DDDMMSS.SH; H is E or W.',
          kind='dsi_lon', pattern=r'^\d{7}\.\d[EW]$', spec='3.12 e'),
)

RECORD_FIELDS: dict[str, tuple[Field, ...]] = {'UHL': UHL_FIELDS, 'DSI': DSI_FIELDS, 'ACC': ACC_FIELDS}
ALL_FIELDS: tuple[Field, ...] = UHL_FIELDS + DSI_FIELDS + ACC_FIELDS
FIELDS_BY_KEY: dict[str, Field] = {field.key: field for field in ALL_FIELDS}


def field(key: str) -> Field:
    """The schema field with this key."""
    return FIELDS_BY_KEY[key]


def subregion_offset(index: int) -> int:
    """0-based offset within the ACC record of subregion *index* (0-8)."""
    return SUBREGION_START - 1 + index * SUBREGION_LENGTH


def zone_for(lat0: int) -> tuple[int, int]:
    """The longitude spacing zone of the cell whose southwest corner latitude
    is *lat0*: (zone number 1-5, multiplier of the latitude interval).

    The zone is decided by the latitude of the cell edge nearer the equator,
    so N50 (50 to 51) and S51 (-51 to -50) are both zone II while N49 and S50
    are zone I.
    """
    lat_eq = lat0 if lat0 >= 0 else -lat0 - 1
    for number, (limit, multiplier) in enumerate(ZONES, start=1):
        if lat_eq < limit:
            return number, multiplier
    raise ValueError(f'Latitude {lat0} is outside -90..89')


def check_schema() -> None:
    """Raise if the field tables do not tile each record exactly once."""
    for record, fields in RECORD_FIELDS.items():
        position = 1
        for item in fields:
            if item.record != record:
                raise AssertionError(f'{item.key} is filed under {record}')
            if item.start != position:
                raise AssertionError(f'{item.key} starts at {item.start}, expected {position}')
            position += item.length
        if position - 1 != RECORD_LENGTHS[record]:
            raise AssertionError(f'{record} fields cover {position - 1} characters, not {RECORD_LENGTHS[record]}')
    covered = sum(item.length for item in SUBREGION_FIELDS) + MAX_COORDINATES * COORDINATE_LENGTH
    if covered != SUBREGION_LENGTH:
        raise AssertionError(f'A subregion covers {covered} characters, not {SUBREGION_LENGTH}')
