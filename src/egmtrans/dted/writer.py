"""Assemble a complete DTED header from its sources, in order of precedence:

1. derived from the cell geometry, the target datum and the data (sentinels,
   origin, intervals, counts, corners, series, vertical and horizontal datum,
   partial cell indicator, the multiple-accuracy flags, the UHL copies of the
   DSI security code and the ACC vertical accuracy): never overridable;
2. the cell's row in the metadata index (a NULL accuracy means NA);
3. the product profile's constants;
4. the base header (the input DTED file's, for a DTED-to-DTED transform);
5. the command line's absolute horizontal accuracy, which fills the field
   only when the base header, or the spec fill, left it NA;
6. the spec fill (NA, 0000, blanks).

With a base header the input is the authority, gaps included. Built from
scratch, every required field must come from the index, the profile or the
command line; the spec fill is not a value for them (the tool never invents a
security code), and an index or profile made for another level stops the
write. A profile or index datum that differs from the output's is reported;
the output keeps the derived code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from egmtrans import _state
from egmtrans.dted.header import (
    AccSubregion,
    CellGeometry,
    DtedHeader,
    cell_geometry_of,
    changed_fields,
    decode_accuracy,
    new_header,
    to_yymm,
)
from egmtrans.dted.index import (
    ACCURACY_COLUMNS,
    COLUMNS_BY_NAME,
    HEADER_COLUMNS,
    INDEX_COLUMNS,
    DtedIndex,
    read_index,
    validate_index,
)
from egmtrans.dted.profile import Profile, load_profile
from egmtrans.dted.schema import NA_VALUE
from egmtrans.dted.validate import Issue, validate_header


class HeaderAssemblyError(ValueError):
    """The header cannot be completed from the sources at hand."""


@dataclass
class DtedMetadata:
    """What the assembler gets for one cell."""

    row: dict | None = None
    subregions: list[dict] = field(default_factory=list)
    profile: Profile | None = None

    @property
    def has_index_row(self) -> bool:
        return self.row is not None


class DtedMetadataSource:
    """The index and the profile named for a run, resolved per cell."""

    def __init__(self, index: DtedIndex | None = None, profile: Profile | None = None):
        self.index = index
        self.profile = profile

    @classmethod
    def load(cls, index_path: str | None, profile_path: str | None) -> DtedMetadataSource:
        """Load the index and the profile; either may be absent."""
        index = read_index(index_path) if index_path else None
        profile = load_profile(profile_path) if profile_path else None
        return cls(index, profile)

    @property
    def empty(self) -> bool:
        return self.index is None and self.profile is None

    @property
    def level(self) -> int | None:
        """The level the sources are for, when they say."""
        if self.index is not None and self.index.level is not None:
            return self.index.level
        if self.profile is not None:
            return self.profile.level
        return None

    def validate(self, level: int | None = None) -> list[Issue]:
        issues = []
        if self.index is not None:
            issues.extend(validate_index(self.index, level=level))
        if self.profile is not None and level is not None and self.profile.level not in (None, level):
            issues.append(Issue('error', 'PROFILE', 'dted_level',
                                f'the profile is for level {self.profile.level}, not {level}'))
        return issues

    def for_cell(self, cell_id: str) -> DtedMetadata:
        """The metadata of one cell.

        Raises:
            LookupError: If an index is loaded but has no row for the cell.
        """
        if self.index is None:
            return DtedMetadata(None, [], self.profile)
        row = self.index.get(cell_id)
        if row is None:
            raise LookupError(f'The index {os.path.basename(self.index.path)} has no row for cell {cell_id}')
        return DtedMetadata(row, self.index.subregions_of(cell_id), self.profile)

    def describe(self) -> str:
        parts = []
        if self.index is not None:
            parts.append(f'index {os.path.basename(self.index.path)} ({len(self.index)} cells)')
        if self.profile is not None:
            parts.append(f'profile {os.path.basename(self.profile.path)}')
        return ' and '.join(parts) if parts else 'no index or profile'


@dataclass
class DerivedFields:
    """What the transform itself knows about the output."""

    vertical_datum: str | None = None  # E96 or E08, from the target datum
    partial_cell: int | None = None  # None keeps the base header's value


def _apply(header: DtedHeader, sources: dict[str, str], column_name: str, value, source: str) -> None:
    column = COLUMNS_BY_NAME[column_name]
    key = column.dted_key
    try:
        if column_name in ACCURACY_COLUMNS:
            header.set(key, value)  # meters, 'NA' or None (NA)
            sources[key] = source
            return
        if value is None:
            return
        if column.type == 'date':
            header.set(key, to_yymm(value))
        elif column.type == 'int':
            header.set(key, int(value))
        else:
            header.set(key, str(value))
    except (ValueError, LookupError) as e:
        raise HeaderAssemblyError(f'{column_name} ({source}): {value!r} cannot be written: {e}') from e
    sources[key] = source


def normalize_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A subregion outline as the specification lists it: no closing vertex,
    clockwise, starting at the most southern vertex and the most western of those.

    *points* are ``(latitude, longitude)`` pairs.
    """
    ring = [(float(lat), float(lon)) for lat, lon in points]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        return ring
    # Shoelace in (x, y) = (lon, lat): positive is counterclockwise.
    area = sum(
        ring[i][1] * ring[(i + 1) % len(ring)][0] - ring[(i + 1) % len(ring)][1] * ring[i][0]
        for i in range(len(ring))
    )
    if area > 0:
        ring.reverse()
    start = min(range(len(ring)), key=lambda i: ring[i])
    return ring[start:] + ring[:start]


def _subregion_from_row(item: dict) -> AccSubregion:
    return AccSubregion.from_values(
        item.get('abs_horiz_acc'), item.get('abs_vert_acc'), item.get('rel_horiz_acc'), item.get('rel_vert_acc'),
        normalize_ring(item['outline']),
    )


def _level_of(value) -> int | None:
    return None if value in (None, '') else int(value)


def assemble_header(
    cell: CellGeometry,
    *,
    base: DtedHeader | None = None,
    metadata: DtedMetadata | None = None,
    derived: DerivedFields | None = None,
    cli_abs_horiz_accuracy: int | None = None,
) -> tuple[DtedHeader, dict[str, str]]:
    """The complete header of *cell* and, for every field, where its value came from.

    Raises:
        HeaderAssemblyError: If the index row or the profile is for another
            level, a value cannot be written, the vertical datum is unknown,
            or, from scratch, a required field has no source or the result
            fails validation.
    """
    derived = derived or DerivedFields()
    metadata = metadata or DtedMetadata()
    for where, level in (
        ('index row', _level_of((metadata.row or {}).get('dted_level'))),
        ('profile', metadata.profile.level if metadata.profile is not None else None),
    ):
        if level is not None and level != cell.level:
            raise HeaderAssemblyError(
                f'The {where} is for DTED level {level}; cell {cell.cell_id} is being written at level {cell.level}'
            )
    if base is not None:
        header = base.copy()
        sources = dict.fromkeys(header.values, 'base')
        for key, raw in header.values.items():
            cleaned = ''.join(ch if 32 <= ord(ch) <= 126 else ' ' for ch in raw)
            if cleaned != raw:
                # GDAL leaves NUL bytes where the spec wants blanks; a copy of
                # such a header must not carry them on.
                header.set_raw(key, cleaned)
                sources[key] = 'base (non-printable bytes blanked)'
    else:
        header = new_header(cell)
        sources = dict.fromkeys(header.values, 'default')

    for key in ('acc.abs_horiz_acc', 'acc.abs_vert_acc', 'acc.rel_horiz_acc', 'acc.rel_vert_acc', 'uhl.abs_vert_acc'):
        raw = header[key]
        try:
            value = decode_accuracy(raw)
        except ValueError:
            # Not a value and not NA: the only honest replacement is NA.
            header.set(key, None)
            sources[key] = f'{sources[key]} (not a value, set to NA)'
            continue
        if value is None and raw != NA_VALUE:
            # NA is alpha, so the spec left justifies it (3.13.5); 1.6.0 and
            # earlier wrote it right justified.
            header.set(key, None)
            sources[key] = f'{sources[key]} (NA left justified)'

    if cli_abs_horiz_accuracy is not None:
        if not 0 <= cli_abs_horiz_accuracy <= 9999:
            raise HeaderAssemblyError(f'Absolute horizontal accuracy must be 0-9999 m, not {cli_abs_horiz_accuracy}')
        # A fallback, as documented: it fills the field only when nothing better is there.
        if header.accuracy('acc.abs_horiz_acc') is None:
            _apply(header, sources, 'abs_horiz_acc', cli_abs_horiz_accuracy, 'command line')

    checks: dict[str, tuple[str, object]] = {}
    profile = metadata.profile
    if profile is not None:
        for column in INDEX_COLUMNS:
            if column.dted_key is None or column.name not in profile.product:
                continue
            if column.check_only:
                checks[column.name] = ('profile', profile.product[column.name])
            else:
                _apply(header, sources, column.name, profile.product[column.name], 'profile')

    if metadata.row is not None:
        row = metadata.row
        for column in INDEX_COLUMNS:
            if column.dted_key is None or column.name not in row:
                continue
            value = row[column.name]
            if column.check_only:
                if value is not None:
                    checks[column.name] = ('index', value)
            elif value is not None or column.name in ACCURACY_COLUMNS:
                _apply(header, sources, column.name, value, 'index')
        subregions = [_subregion_from_row(item) for item in metadata.subregions]
        header.set_subregions(subregions)
        for key in ('acc.subregions', 'acc.outline_flag', 'uhl.multiple_accuracy'):
            sources[key] = 'index' if subregions else 'derived'

    for key, raw in cell.header_values().items():
        header.set_raw(key, raw)
        sources[key] = 'derived'
    if derived.vertical_datum:
        header.set('dsi.vertical_datum', derived.vertical_datum)
        sources['dsi.vertical_datum'] = 'derived'
    header.set('dsi.horizontal_datum', 'WGS84')
    sources['dsi.horizontal_datum'] = 'derived'
    if derived.partial_cell is not None:
        header.set('dsi.partial_cell', derived.partial_cell)
        sources['dsi.partial_cell'] = 'derived'
    header.set_raw('uhl.security_code', header['dsi.security_code'].ljust(3))
    sources['uhl.security_code'] = 'derived'
    header.set_raw('uhl.abs_vert_acc', header['acc.abs_vert_acc'])
    sources['uhl.abs_vert_acc'] = 'derived'
    if header.outline_count() == 0 and header['acc.subregions'].strip() == '':
        header.set_raw('uhl.multiple_accuracy', '0')
    elif header.outline_count() > 0:
        header.set_raw('uhl.multiple_accuracy', '1')

    problems = []
    logger = _state.get_logger()
    for column_name, (where, value) in checks.items():
        current = header.stripped(COLUMNS_BY_NAME[column_name].dted_key)
        if str(value).strip() != current:
            # The derived code is written whatever the sources say; the
            # disagreement means the index or profile describes another product.
            logger.warning(
                f'DTED header: {column_name} is {value!r} in the {where} but the output is {current!r}; '
                f'the output keeps {current!r}'
            )
    if base is None:
        # Built from scratch, every required field must come from a source:
        # the spec fill (NA, 0000) is not a value for it. With a base header
        # the input is the authority, gaps included.
        for column in HEADER_COLUMNS:
            if column.required and sources.get(column.dted_key, 'default').startswith('default'):
                also = ' or --abs_horiz_accuracy' if column.name == 'abs_horiz_acc' else ''
                problems.append(f'{column.name} is required and nothing supplies it (index or profile{also})')
    if not header['dsi.vertical_datum'].strip():
        problems.append('the vertical datum is unknown')
    if problems:
        raise HeaderAssemblyError('The DTED header cannot be completed:\n  ' + '\n  '.join(problems))

    if base is None:
        issues = validate_header(header)
        errors = [issue for issue in issues if issue.severity == 'error']
        if errors:
            raise HeaderAssemblyError(
                'The assembled DTED header is not valid:\n  ' + '\n  '.join(str(e) for e in errors)
            )
        for issue in issues:
            if issue.severity == 'warning':
                logger.warning(f'DTED header of cell {cell.cell_id}: {issue}')
    return header, sources


def describe_changes(before: DtedHeader | None, after: DtedHeader, sources: dict[str, str]) -> list[str]:
    """Log lines for the fields that changed, with their source; for a header
    built from scratch (*before* is None), every supplied field with its source."""
    lines = []
    if before is None:
        counts: dict[str, int] = {}
        for source in sources.values():
            counts[source] = counts.get(source, 0) + 1
        lines.append('Header fields by source: ' + ', '.join(f'{k} {v}' for k, v in sorted(counts.items())))
        for key, source in sources.items():
            if source in ('default', 'derived'):
                continue
            value = 'subregion block' if key == 'acc.subregions' else repr(after[key])
            lines.append(f'    {key}: {value} ({source})')
        return lines
    for key, old, new in changed_fields(before, after):
        if key == 'acc.subregions':
            lines.append(f"    {key}: subregion block rewritten ({sources.get(key, '?')})")
        else:
            lines.append(f"    {key}: {old!r} -> {new!r} ({sources.get(key, '?')})")
    if not lines:
        lines.append('    (no change)')
    return lines


def cell_of_header(header: DtedHeader, extension: str | None = None) -> CellGeometry:
    cell = cell_geometry_of(header, extension)
    if cell is None:
        raise HeaderAssemblyError('The input header has no readable origin or level')
    return cell
