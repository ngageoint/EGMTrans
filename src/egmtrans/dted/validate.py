"""Check a DTED header, and optionally its elevation records, against
MIL-PRF-89020B.

Every finding is an :class:`Issue` with a severity: ``error`` for a value a
reader cannot rely on or a mandatory rule broken, ``warning`` for a deviation
from the spec that readers tolerate, ``info`` for something worth knowing
(free text in a reserved area, the elevation range).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import numpy as np

from egmtrans.dted import schema
from egmtrans.dted.fips import producer_code_warning
from egmtrans.dted.header import DtedHeader, decode_accuracy, has_visible_text, parse_dms, read_header
from egmtrans.dted.records import RecordError, read_records
from egmtrans.dted.schema import (
    ALL_FIELDS,
    DATA_RECORD_OVERHEAD,
    ELEVATION_MAX,
    ELEVATION_MIN,
    HEADER_LENGTH,
    LEVELS,
    MAX_SUBREGIONS,
    NA_VALUE,
    NULL_ELEVATION,
    SUBREGION_FIELDS,
    ZONE_NAMES,
    Field,
    zone_for,
)

SEVERITIES = ('error', 'warning', 'info')


@dataclass(frozen=True)
class Issue:
    severity: str
    record: str
    key: str
    message: str

    def __str__(self) -> str:
        where = f'{self.record}.{self.key}' if self.key else self.record
        return f'{self.severity.upper()} {where}: {self.message}'

    def to_dict(self) -> dict[str, str]:
        return {'severity': self.severity, 'record': self.record, 'key': self.key, 'message': self.message}


def count(issues: list[Issue], severity: str) -> int:
    return sum(1 for issue in issues if issue.severity == severity)


def _short_key(key: str) -> str:
    return key.split('.', 1)[1] if '.' in key else key


def _printable_problems(raw: str) -> list[str]:
    found = []
    for position, char in enumerate(raw, start=1):
        code = ord(char)
        if code < 32 or code > 126:
            name = 'NUL' if code == 0 else f'byte 0x{code:02x}'
            found.append(f'{name} at character {position}')
    return found


def _check_field(item: Field, raw: str, issues: list[Issue]) -> bool:
    """Format checks of one field; returns False when its value cannot be decoded."""
    problems = _printable_problems(raw)
    if problems:
        where = ', '.join(problems[:3]) + (', ...' if len(problems) > 3 else '')
        issues.append(Issue('error', item.record, _short_key(item.key),
                            f'{len(problems)} non-printable byte(s) ({where}); the spec blank-fills unused positions'))
        return False

    if item.kind == 'reserved':
        if raw.strip():
            issues.append(Issue('info', item.record, _short_key(item.key),
                                f'reserved area carries text: {raw.strip()!r}'))
        return True

    if item.kind == 'accuracy':
        if re.fullmatch(r'\d{4}', raw):
            return True
        if raw == NA_VALUE:
            return True
        if raw.strip() == 'NA':
            issues.append(Issue('warning', item.record, _short_key(item.key),
                                f'{raw!r}: NA is alpha and belongs left justified ({NA_VALUE!r}), see 3.13.5'))
            return True
        issues.append(Issue('error', item.record, _short_key(item.key), f'{raw!r} is neither 0000-9999 nor NA'))
        return False

    if item.pattern and not re.match(item.pattern, raw):
        severity = 'error'
        detail = f'{raw!r} does not match the expected form'
        if item.kind == 'vdatum':
            detail = f'{raw!r} is not a vertical datum code (MSL, E96; E08 in practice)'
        elif item.kind == 'security3':
            detail = f'{raw!r}: one of U, R, C, S left justified and blank filled'
        elif item.kind == 'security1':
            detail = f'{raw!r}: one of U, R, C, S'
        elif item.kind == 'angle':
            severity = 'warning'
            detail = f'{raw!r}: a DTED cell has orientation 0000000.0'
        elif item.kind == 'date':
            detail = f'{raw!r} is not a YYMM date (or 0000)'
        elif item.kind == 'outline_flag':
            detail = f'{raw!r}: the outline flag is 00 or 02-09'
        elif item.kind == 'edition':
            detail = f'{raw!r}: the data edition number is 01-99'
        issues.append(Issue(severity, item.record, _short_key(item.key), detail))
        return False

    if item.kind == 'angle' and raw != item.fixed:
        issues.append(Issue('warning', item.record, _short_key(item.key),
                            f'{raw!r}: a DTED cell has orientation 0000000.0'))
    if item.kind == 'vdatum' and raw == 'E08':
        issues.append(Issue('warning', item.record, _short_key(item.key),
                            'E08 (EGM2008) is not a MIL-PRF-89020B vertical datum code (MSL or E96)'))
    if item.kind == 'edition' and raw == '00':
        issues.append(Issue('error', item.record, _short_key(item.key), 'the data edition number is 01-99, not 00'))
        return False
    if item.key == 'dsi.product_spec' and raw.strip() != schema.PRODUCT_SPEC:
        issues.append(Issue('warning', item.record, _short_key(item.key),
                            f'{raw.strip()!r}: data produced to this spec carry {schema.PRODUCT_SPEC}'))
    if item.key == 'dsi.compilation_date' and raw == '0000':
        issues.append(Issue('warning', item.record, _short_key(item.key), 'compilation date is not set'))
    if item.key == 'dsi.producer_code':
        if not raw.strip():
            issues.append(Issue('warning', item.record, _short_key(item.key), 'producer code is blank'))
        elif not re.match(r'^[A-Z]{2}', raw):
            issues.append(Issue('warning', item.record, _short_key(item.key),
                                f'{raw.strip()!r} does not begin with a FIPS 10-4 country code (3.13.4.1 i)'))
        else:
            nation = producer_code_warning(raw)
            if nation:
                issues.append(Issue('warning', item.record, _short_key(item.key), nation))
    if item.key == 'dsi.vertical_datum' and not raw.strip():
        issues.append(Issue('error', item.record, _short_key(item.key), 'vertical datum is blank'))
        return False
    return True


def _check_geometry(header: DtedHeader, ok: set[str], issues: list[Issue]) -> None:
    uhl_lon = parse_dms(header['uhl.origin_lon'], 'uhl_lon') if 'uhl.origin_lon' in ok else None
    uhl_lat = parse_dms(header['uhl.origin_lat'], 'uhl_lat') if 'uhl.origin_lat' in ok else None
    if uhl_lon is None or uhl_lat is None:
        return
    if uhl_lon != int(uhl_lon) or uhl_lat != int(uhl_lat):
        issues.append(Issue('error', 'UHL', 'origin', 'the origin is not a full degree (3.13.3.1 a, b)'))
        return
    lon0, lat0 = int(uhl_lon), int(uhl_lat)

    def near(a: float | None, b: float) -> bool:
        return a is not None and abs(a - b) < 1e-9

    dsi_lon = parse_dms(header['dsi.origin_lon'], 'dsi_lon')
    dsi_lat = parse_dms(header['dsi.origin_lat'], 'dsi_lat')
    if not (near(dsi_lon, lon0) and near(dsi_lat, lat0)):
        issues.append(Issue('error', 'DSI', 'origin', 'the DSI origin differs from the UHL origin'))
    corners = {
        'sw': (lat0, lon0), 'nw': (lat0 + 1, lon0), 'ne': (lat0 + 1, lon0 + 1), 'se': (lat0, lon0 + 1),
    }
    for name, (lat, lon) in corners.items():
        got_lat = parse_dms(header[f'dsi.{name}_lat'], 'corner_lat')
        got_lon = parse_dms(header[f'dsi.{name}_lon'], 'corner_lon')
        if not (near(got_lat, lat) and near(got_lon, lon)):
            issues.append(Issue('error', 'DSI', f'{name}_corner',
                                f'{name.upper()} corner is not ({lat}, {lon}) for a cell with origin ({lat0}, {lon0})'))

    lat_interval = header.interval_tenths('uhl.lat_interval')
    lon_interval = header.interval_tenths('uhl.lon_interval')
    if lat_interval is None or lon_interval is None:
        return
    dsi_intervals = (header.interval_tenths('dsi.lat_interval'), header.interval_tenths('dsi.lon_interval'))
    if dsi_intervals != (lat_interval, lon_interval):
        issues.append(Issue('error', 'DSI', 'intervals', 'the DSI intervals differ from the UHL intervals'))
    level = schema.LEVEL_BY_LAT_INTERVAL.get(lat_interval)
    if level is None:
        issues.append(Issue('error', 'UHL', 'lat_interval',
                            f'{lat_interval} tenths of a second is not a DTED level (0300, 0030 or 0010)'))
        return
    zone_number, multiplier = zone_for(lat0)
    expected_lon = lat_interval * multiplier
    if lon_interval != expected_lon:
        issues.append(Issue('error', 'UHL', 'lon_interval',
                            f'{lon_interval:04d} does not match zone {ZONE_NAMES[zone_number - 1]} '
                            f'for latitude {lat0} ({expected_lon:04d} expected, Tables I to III)'))
        return
    expected_lines = 36000 // lon_interval + 1
    for key in ('uhl.lon_lines', 'dsi.lon_lines'):
        if header.interval_tenths(key) != expected_lines:
            issues.append(Issue('error', key.split('.')[0].upper(), _short_key(key),
                                f'{header[key]!r}: a full cell at this interval has {expected_lines} longitude lines'))
    for key in ('uhl.lat_points', 'dsi.lat_lines'):
        if header.interval_tenths(key) != level.lat_points:
            issues.append(Issue('error', key.split('.')[0].upper(), _short_key(key),
                                f'{header[key]!r}: level {level.number} has {level.lat_points} latitude points'))


def _check_subregions(header: DtedHeader, ok: set[str], issues: list[Issue]) -> None:
    flag = header.outline_count() if 'acc.outline_flag' in ok else None
    multiple = header['uhl.multiple_accuracy']
    present = header.subregions(announced_only=False)
    if flag is not None:
        if multiple in ('0', '1') and (multiple == '1') != (flag > 0):
            issues.append(Issue('error', 'UHL', 'multiple_accuracy',
                                f'{multiple!r} disagrees with the ACC outline flag {header["acc.outline_flag"]!r}'))
        if len(present) != flag:
            issues.append(Issue('error', 'ACC', 'subregions',
                                f'the outline flag announces {flag} subregion(s) but {len(present)} are filled in'))
    lon0, lat0 = header.origin
    overall = {}
    for item in SUBREGION_FIELDS[:4]:
        try:
            overall[item.key] = header.accuracy(f'acc.{item.key}')
        except ValueError:
            overall[item.key] = None
    worst: dict[str, int | None] = {}
    block = header['acc.subregions']
    for index, subregion in enumerate(header.subregions(announced_only=True), start=1):
        where = f'subregion {index}'
        for item in SUBREGION_FIELDS[:4]:
            raw = getattr(subregion, item.key)
            try:
                value = decode_accuracy(raw)
            except ValueError:
                issues.append(Issue('error', 'ACC', where, f'{item.key} {raw!r} is neither 0000-9999 nor NA'))
                continue
            if raw != NA_VALUE and raw.strip() == 'NA':
                issues.append(Issue('warning', 'ACC', where, f'{item.key} {raw!r}: NA belongs left justified'))
            if value is not None:
                worst[item.key] = max(worst.get(item.key) or 0, value)
        count_text = subregion.coord_count
        if not re.fullmatch(r'(0[3-9]|1[0-4])', count_text):
            issues.append(Issue('error', 'ACC', where, f'coordinate count {count_text!r} is not 03-14'))
        elif int(count_text) != len(subregion.coordinates):
            issues.append(Issue('error', 'ACC', where, f'coordinate count {count_text} but '
                                f'{len(subregion.coordinates)} pair(s) are filled in'))
        outline = subregion.decoded_outline()
        if any(lat is None or lon is None for lat, lon in outline):
            issues.append(Issue('error', 'ACC', where, 'a coordinate pair is malformed (DDMMSS.SH, DDDMMSS.SH)'))
            continue
        if lon0 is not None and lat0 is not None:
            outside = [(lat, lon) for lat, lon in outline
                       if not (lat0 - 1e-9 <= lat <= lat0 + 1 + 1e-9 and lon0 - 1e-9 <= lon <= lon0 + 1 + 1e-9)]
            if outside:
                issues.append(Issue('warning', 'ACC', where, f'{len(outside)} coordinate pair(s) lie outside the cell'))
        if len(outline) >= 3:
            first = outline[0]
            southwest = min(outline, key=lambda p: (p[0], p[1]))
            if first != southwest:
                issues.append(Issue('warning', 'ACC', where,
                                    'the first coordinate is not the most southern, then western, point (3.13.5.1 b)'))
            area = 0.0
            for (lat_a, lon_a), (lat_b, lon_b) in zip(outline, outline[1:] + outline[:1], strict=True):
                area += lon_a * lat_b - lon_b * lat_a
            if area > 0:
                issues.append(Issue('warning', 'ACC', where, 'the outline runs counterclockwise (3.13.5.1 d)'))
            if outline[0] == outline[-1]:
                issues.append(Issue('warning', 'ACC', where, 'the first and last coordinate pairs are the same; '
                                    'closure is implied (3.13.5.1 h)'))
    if worst:
        for key, value in worst.items():
            if overall.get(key) is not None and overall[key] < value:
                issues.append(Issue('info', 'ACC', _short_key(key),
                                    f'{overall[key]} m is better than the worst subregion ({value} m); '
                                    f'the overall accuracy should be the worst (3.12 e note)'))
    if flag is not None and flag == 0 and has_visible_text(block):
        issues.append(Issue('error', 'ACC', 'subregions', 'no subregions announced but the block is not blank'))
    if len(present) > MAX_SUBREGIONS:
        issues.append(Issue('error', 'ACC', 'subregions', f'more than {MAX_SUBREGIONS} subregions'))


def validate_header(
    header: DtedHeader, *, extension: str | None = None, file_size: int | None = None
) -> list[Issue]:
    """Every spec problem the header has, worst first within each record."""
    issues: list[Issue] = []
    ok: set[str] = set()
    for item in ALL_FIELDS:
        if item.kind == 'subregions':
            problems = _printable_problems(header[item.key])
            if problems:
                where = ', '.join(problems[:3]) + (', ...' if len(problems) > 3 else '')
                issues.append(Issue('error', 'ACC', 'subregions',
                                    f'{len(problems)} non-printable byte(s) ({where}); unused slots are blank filled'))
            continue
        if _check_field(item, header[item.key], issues):
            ok.add(item.key)

    detection = header.detect_level(extension)
    if detection.conflict:
        votes = ', '.join(f'{name} {vote}' for name, vote in detection.votes.items() if vote is not None)
        issues.append(Issue('error', 'FILE', 'level', f'the level is ambiguous: {votes}'))
    elif detection.level is None:
        issues.append(Issue('error', 'FILE', 'level', 'the level cannot be determined'))

    _check_geometry(header, ok, issues)

    codes_ok = 'uhl.security_code' in ok and 'dsi.security_code' in ok
    if codes_ok and header['uhl.security_code'][0] != header['dsi.security_code']:
        issues.append(Issue('error', 'DSI', 'security_code', f'{header["dsi.security_code"]!r} differs from the '
                            f'UHL security code {header["uhl.security_code"]!r}'))

    try:
        uhl_acc = header.accuracy('uhl.abs_vert_acc')
        acc_acc = header.accuracy('acc.abs_vert_acc')
    except ValueError:
        pass
    else:
        if uhl_acc != acc_acc:
            issues.append(Issue('error', 'UHL', 'abs_vert_acc',
                                f'{header["uhl.abs_vert_acc"]!r} differs from the ACC absolute vertical accuracy '
                                f'{header["acc.abs_vert_acc"]!r}; the UHL repeats it'))

    _check_subregions(header, ok, issues)

    if file_size is not None and detection.level is not None:
        lon_lines = header.interval_tenths('uhl.lon_lines')
        lat_points = header.interval_tenths('uhl.lat_points')
        if lon_lines and lat_points:
            expected = HEADER_LENGTH + lon_lines * (DATA_RECORD_OVERHEAD + 2 * lat_points)
            if file_size != expected:
                issues.append(Issue('error', 'FILE', 'size', f'{file_size:,} bytes; {lon_lines} full records of '
                                    f'{lat_points} posts make {expected:,}'))

    order = {s: i for i, s in enumerate(SEVERITIES)}
    issues.sort(key=lambda issue: order[issue.severity])
    return issues


def validate_records(path: str, header: DtedHeader, *, verify_checksums: bool = True) -> list[Issue]:
    """Check the elevation records that follow the header: sentinels, block
    and line counts, checksums, the elevation limits, and the null share
    against the partial cell indicator. Reports the elevation range as info."""
    issues: list[Issue] = []
    try:
        decoded = read_records(path, header, verify_checksums=verify_checksums)
    except RecordError as e:
        issues.append(Issue('error', 'DATA', '', str(e)))
        return issues

    lon_lines = header.interval_tenths('uhl.lon_lines')
    if decoded.bad_sentinels:
        issues.append(Issue('error', 'DATA', 'sentinel',
                            f'{decoded.bad_sentinels} record(s) do not start with the 0xAA sentinel'))
    if decoded.bad_block_counts:
        issues.append(Issue('error', 'DATA', 'block_count',
                            f'{decoded.bad_block_counts} record(s) have a block count out of sequence'))
    if decoded.bad_lon_counts:
        issues.append(Issue('error', 'DATA', 'lon_count',
                            f'{decoded.bad_lon_counts} record(s) have a longitude count out of sequence'))
    if decoded.bad_lat_counts:
        issues.append(Issue('error', 'DATA', 'lat_count',
                            f'{decoded.bad_lat_counts} record(s) do not start at latitude count 0'))
    if decoded.bad_checksums:
        issues.append(Issue('error', 'DATA', 'checksum',
                            f'{decoded.bad_checksums} of {lon_lines} record checksums do not match'))

    values = decoded.values
    nulls = values == NULL_ELEVATION
    null_count = int(nulls.sum())
    total = values.size
    valid = values[~nulls]
    if valid.size:
        issues.append(Issue('info', 'DATA', 'elevations',
                            f'{int(valid.min())} to {int(valid.max())} m over {total - null_count:,} posts; '
                            f'{null_count:,} null ({100 * null_count / total:.2f}%)'))
        outside = int(np.count_nonzero((valid < ELEVATION_MIN) | (valid > ELEVATION_MAX)))
        if outside:
            issues.append(Issue('warning', 'DATA', 'elevations',
                                f'{outside:,} post(s) lie outside the {ELEVATION_MIN:,} to {ELEVATION_MAX:,} m '
                                f'range of the specification'))
    else:
        issues.append(Issue('warning', 'DATA', 'elevations', 'every post is null'))
    partial = header['dsi.partial_cell']
    if partial.isdigit():
        coverage = 100 * (total - null_count) / total
        if int(partial) == 0 and null_count:
            issues.append(Issue('warning', 'DSI', 'partial_cell',
                                f'00 (complete cell) but {null_count:,} posts ({100 - coverage:.2f}%) are null'))
        elif int(partial) and abs(coverage - int(partial)) > 2:
            issues.append(Issue('warning', 'DSI', 'partial_cell',
                                f'{partial}% announced but {coverage:.1f}% of the posts hold data'))
    return issues


def validate_file(
    path: str, *, check_data: bool = False, extension: str | None = None
) -> tuple[DtedHeader, list[Issue]]:
    """Read the header of *path* and validate it (and its records when asked).

    *extension* stands in for the path's own when the file is a scratch copy
    of a file that will get another name, so the level the name claims is
    checked against the header before the rename.
    """
    header = read_header(path)
    if extension is None:
        extension = os.path.splitext(path)[1]
    issues = validate_header(header, extension=extension, file_size=os.path.getsize(path))
    if check_data:
        issues.extend(validate_records(path, header))
    return header, issues


def level_name(level: int | None) -> str:
    return LEVELS[level].series if level in LEVELS else 'unknown level'
