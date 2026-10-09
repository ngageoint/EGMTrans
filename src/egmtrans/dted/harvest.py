"""Build index rows from what is already at hand: the headers of existing DTED
files, the tags and XML sidecars of source rasters, or any attribute table (a
footprint layer, a catalog) whose columns are mapped onto the index's."""

from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from egmtrans import _state
from egmtrans.dted.header import DtedHeader, read_header, yymm_to_iso
from egmtrans.dted.header import cell_id as make_cell_id
from egmtrans.dted.index import (
    ACCURACY_COLUMNS,
    COLUMNS_BY_NAME,
    INDEX_COLUMNS,
    Column,
    DtedIndex,
    _parquet_available,
    check_value,
    new_row,
    normalize_value,
    parse_date,
    parse_polygon_wkb,
    read_index,
    write_index,
)
from egmtrans.dted.profile import Profile
from egmtrans.dted.schema import LEVELS

DTED_EXTENSIONS = tuple(level.extension for level in LEVELS.values())
RASTER_EXTENSIONS = ('.tif', '.tiff')


def find_files(paths: Iterable[str], extensions: tuple[str, ...]) -> list[str]:
    """Files with one of *extensions* among *paths* (files or folders, walked in sorted order)."""
    found = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    if name.lower().endswith(extensions):
                        found.append(os.path.join(root, name))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise FileNotFoundError(path)
    return found


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _text(header: DtedHeader, key: str) -> str | None:
    """The field's text without padding; bytes that are not printable (the NULs
    some writers leave) are dropped, so a defective header harvests as blank."""
    value = ''.join(ch for ch in header[key] if 32 <= ord(ch) <= 126).strip()
    return value or None


def _accuracy(header: DtedHeader, key: str) -> int | None:
    try:
        return header.accuracy(key)
    except ValueError:
        return None


def _date(header: DtedHeader, key: str) -> dt.date | None:
    iso = yymm_to_iso(header[key])
    return parse_date(iso) if iso else None


def row_from_header(header: DtedHeader, path: str | None = None) -> tuple[dict, list[dict]]:
    """An index row and the subregion rows that describe an existing header."""
    cell = header.cell_id
    if cell is None:
        raise ValueError(f'{os.path.basename(path or "header")}: the origin cannot be read')
    level = header.detect_level(os.path.splitext(path)[1] if path else None).level
    row = new_row(
        cell,
        dted_level=level,
        security_code=_text(header, 'dsi.security_code'),
        security_control=_text(header, 'dsi.security_control'),
        security_handling=_text(header, 'dsi.security_handling'),
        unique_ref_uhl=_text(header, 'uhl.unique_ref'),
        unique_ref_dsi=_text(header, 'dsi.unique_ref'),
        data_edition=int(header['dsi.data_edition']) if header['dsi.data_edition'].strip().isdigit() else None,
        match_merge_version=_text(header, 'dsi.match_merge_version'),
        maintenance_date=_date(header, 'dsi.maintenance_date'),
        match_merge_date=_date(header, 'dsi.match_merge_date'),
        maintenance_code=_text(header, 'dsi.maintenance_code'),
        producer_code=_text(header, 'dsi.producer_code'),
        product_spec=_text(header, 'dsi.product_spec'),
        product_spec_amend=_text(header, 'dsi.product_spec_amend'),
        product_spec_date=_date(header, 'dsi.product_spec_date'),
        vertical_datum=_text(header, 'dsi.vertical_datum'),
        horizontal_datum=_text(header, 'dsi.horizontal_datum'),
        digitizing_system=_text(header, 'dsi.digitizing_system'),
        compilation_date=_date(header, 'dsi.compilation_date'),
        abs_horiz_acc=_accuracy(header, 'acc.abs_horiz_acc'),
        abs_vert_acc=_accuracy(header, 'acc.abs_vert_acc'),
        rel_horiz_acc=_accuracy(header, 'acc.rel_horiz_acc'),
        rel_vert_acc=_accuracy(header, 'acc.rel_vert_acc'),
        acc_nima_reserved=_text(header, 'acc.nima_reserved'),
        dsi_nima_text=_text(header, 'dsi.nima_text'),
        dsi_producer_text=_text(header, 'dsi.producer_text'),
        dsi_free_text=_text(header, 'dsi.free_text'),
        partial_cell=int(header['dsi.partial_cell']) if header['dsi.partial_cell'].strip().isdigit() else None,
        source_file=os.path.abspath(path) if path else None,
        updated=_now(),
    )
    subregions = []
    for seq, subregion in enumerate(header.subregions(), start=1):
        outline = subregion.decoded_outline()
        if any(lat is None or lon is None for lat, lon in outline):
            continue
        accuracies = subregion.accuracies()
        subregions.append({'cell_id': cell, 'seq': seq, 'outline': outline, **accuracies})
    return row, subregions


def rows_from_dted(paths: Iterable[str]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Rows and subregions harvested from DTED files (or folders of them)."""
    rows: dict[str, dict] = {}
    subregions: dict[str, list[dict]] = {}
    for path in find_files(paths, DTED_EXTENSIONS):
        row, items = row_from_header(read_header(path), path)
        rows[row['cell_id']] = row
        if items:
            subregions[row['cell_id']] = items
        else:
            subregions.pop(row['cell_id'], None)
    return rows, subregions


def cells_covered(geotransform: tuple, cols: int, rows: int) -> list[str]:
    """The one-degree cells a geographic raster covers completely."""
    x0, dx, _, y0, _, dy = geotransform
    xmin, xmax = x0, x0 + cols * dx
    ymax, ymin = y0, y0 + rows * dy
    tol = max(abs(dx), abs(dy)) * 0.75
    cells = []
    for lat0 in range(math.ceil(ymin - tol), math.floor(ymax + tol)):
        for lon0 in range(math.ceil(xmin - tol), math.floor(xmax + tol)):
            if lon0 >= xmin - tol and lon0 + 1 <= xmax + tol and lat0 >= ymin - tol and lat0 + 1 <= ymax + tol:
                cells.append(make_cell_id(lon0, lat0))
    return cells


def _convert(column_name: str, text: str):
    """A harvested text value as the column's type."""
    column = COLUMNS_BY_NAME[column_name]
    text = text.strip()
    if text == '':
        return None
    if column.type == 'int':
        if column_name in ACCURACY_COLUMNS and text.upper() == 'NA':
            return None
        number = float(text)
        return math.ceil(number) if column_name in ACCURACY_COLUMNS else int(round(number))
    if column.type == 'date':
        match = re.match(r'(\d{4})[:/-](\d{2})(?:[:/-](\d{2}))?', text)
        if not match:
            raise ValueError(f'{column_name}: {text!r} is not a date')
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
    return text


def _extract(mapping: dict, value: str | None) -> str | None:
    if value is None:
        return None
    pattern = mapping.get('pattern')
    if not pattern:
        return value
    match = re.search(pattern, value)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def _sidecar_path(pattern: str, raster: str, cell: str) -> str | None:
    folder = os.path.dirname(os.path.abspath(raster))
    name = os.path.basename(raster)
    stem = os.path.splitext(name)[0]
    candidate = pattern.format(stem=stem, name=name, cell=cell, dir=folder)
    if not os.path.isabs(candidate):
        candidate = os.path.join(folder, candidate)
    matches = sorted(glob.glob(candidate))
    return matches[0] if matches else (candidate if os.path.isfile(candidate) else None)


def _safe_xml_bytes(path: str) -> bytes:
    """The sidecar's bytes, refused when it declares a DOCTYPE or entities: a
    metadata sidecar needs neither, and both are how XML parsers get attacked
    (external entities, entity expansion)."""
    with open(path, 'rb') as handle:
        data = handle.read()
    head = data[:4096].upper()
    if b'<!DOCTYPE' in head or b'<!ENTITY' in data.upper():
        raise ValueError(f'{os.path.basename(path)} declares a DOCTYPE or entities, which a metadata sidecar must not')
    return data


def _xpaths(mapping: dict) -> list[str]:
    xpath = mapping['xpath']
    return list(xpath) if isinstance(xpath, (list, tuple)) else [xpath]


def _et_find(root, xpath: str, namespaces: dict[str, str]) -> str | None:
    """The text of the first match of a simple XPath through ElementTree (no
    predicates), or an attribute's value for a path ending in ``/@name``."""
    attribute = None
    if '/@' in xpath:
        xpath, attribute = xpath.rsplit('/@', 1)
    if xpath.startswith('//'):
        xpath = '.' + xpath
    elif xpath.startswith('/'):
        xpath = '.' + xpath[xpath.index('/', 1):] if xpath.count('/') > 1 else '.'
    try:
        element = root.find(xpath, namespaces)
    except SyntaxError as e:
        raise ValueError(f'the XPath {xpath!r} needs lxml (pip install lxml): {e}') from e
    if element is None:
        return None
    return element.get(attribute) if attribute else (element.text or '')


def _xml_values(path: str, namespaces: dict[str, str], fields: dict[str, dict]) -> dict[str, str | None]:
    """The first match of each field's XPath in the sidecar; of several XPaths,
    the first that yields a value. lxml when available, ElementTree otherwise."""
    data = _safe_xml_bytes(path)
    try:
        from lxml import etree

        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
        tree = etree.ElementTree(etree.fromstring(data, parser))

        def first_text(xpath: str) -> str | None:
            found = tree.xpath(xpath, namespaces=namespaces)
            if not found:
                return None
            first = found[0]
            return first if isinstance(first, str) else (first.text or '')
    except ImportError:
        import xml.etree.ElementTree as ElementTree

        root = ElementTree.fromstring(data)

        def first_text(xpath: str) -> str | None:
            return _et_find(root, xpath, namespaces)

    values: dict[str, str | None] = {}
    for column, mapping in fields.items():
        value = None
        for xpath in _xpaths(mapping):
            try:
                value = _extract(mapping, first_text(xpath))
            except ValueError as e:
                raise ValueError(f'{column}: {e}') from e
            if value not in (None, ''):
                break
        values[column] = value
    return values


def rows_from_rasters(paths: Iterable[str], profile: Profile | None = None) -> dict[str, dict]:
    """Rows for every cell the rasters cover, with the values the profile's
    harvest mappings pull from their tags and XML sidecars."""
    from osgeo import gdal

    harvest = profile.harvest if profile else None
    rows: dict[str, dict] = {}
    for path in find_files(paths, RASTER_EXTENSIONS):
        with gdal.Open(path) as dataset:
            geotransform = dataset.GetGeoTransform()
            cols, lines = dataset.RasterXSize, dataset.RasterYSize
            tags = dataset.GetMetadata() or {}
        for cell in cells_covered(geotransform, cols, lines):
            row = new_row(cell, source_file=os.path.abspath(path), updated=_now())
            if profile is not None and profile.level is not None:
                row['dted_level'] = profile.level
            if harvest is not None:
                for column, mapping in harvest.tag_fields.items():
                    row[column] = _convert(column, _extract(mapping, tags.get(mapping['tag'])) or '')
                if harvest.xml_sidecar and harvest.xml_fields:
                    sidecar = _sidecar_path(harvest.xml_sidecar, path, cell)
                    if sidecar:
                        row['source_metadata_file'] = os.path.abspath(sidecar)
                        for column, text in _xml_values(sidecar, harvest.xml_namespaces, harvest.xml_fields).items():
                            row[column] = _convert(column, text or '')
            rows[cell] = row
    return rows


CELL_PATTERN = re.compile(r'([NS]\d{2}[EW]\d{3})')


@dataclass
class TableImport:
    """What :func:`rows_from_table` made of an attribute table, for the report."""

    rows: dict[str, dict]
    mapped: dict[str, str] = field(default_factory=dict)      # index column -> table column
    constants: dict[str, object] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)          # table columns no index column took
    cell_source: str = ''
    layer: str | None = None
    rows_read: int = 0
    rows_without_cell: int = 0
    duplicates_resolved: int = 0


def _table_records(path: str, layer: str | None) -> tuple[list[dict], list[str], str | None]:
    """Every row of a table as a dict of its attributes plus ``_envelope``
    (the southwest corner of its geometry, or None): (records, field names,
    layer name). A .parquet file is read with pyarrow when it is installed,
    everything else through OGR."""
    if path.lower().endswith('.parquet') and _parquet_available():
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        metadata = table.schema.metadata or {}
        geometry_column = None
        raw = metadata.get(b'geo')
        if raw:
            geometry_column = json.loads(raw).get('primary_column', 'geometry')
        elif 'geometry' in table.column_names:
            geometry_column = 'geometry'
        fields = [name for name in table.column_names if name != geometry_column]
        records = []
        for record in table.to_pylist():
            blob = record.pop(geometry_column, None) if geometry_column else None
            envelope = None
            if blob:
                ring = parse_polygon_wkb(blob)
                if ring:
                    envelope = (min(x for x, _ in ring), min(y for _, y in ring))
            record['_envelope'] = envelope
            records.append(record)
        return records, fields, None

    from osgeo import ogr

    source = ogr.Open(path)
    if source is None:
        raise ValueError(f'Cannot open {path}')
    features = source.GetLayerByName(layer) if layer else source.GetLayer(0)
    if features is None:
        raise ValueError(f'{path} has no layer {layer!r}')
    definition = features.GetLayerDefn()
    fields = [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())]
    kinds = [definition.GetFieldDefn(i).GetType() for i in range(definition.GetFieldCount())]
    records = []
    for feature in features:
        record = {}
        for i, (name, kind) in enumerate(zip(fields, kinds)):
            if not feature.IsFieldSetAndNotNull(i):
                record[name] = None
            elif kind in (ogr.OFTInteger, ogr.OFTInteger64):
                record[name] = feature.GetFieldAsInteger64(i)
            elif kind == ogr.OFTReal:
                record[name] = feature.GetFieldAsDouble(i)
            else:
                record[name] = feature.GetFieldAsString(i)  # dates as YYYY/MM/DD, text as is
        geometry = feature.GetGeometryRef()
        envelope = None
        if geometry is not None:
            xmin, _xmax, ymin, _ymax = geometry.GetEnvelope()
            envelope = (xmin, ymin)
        record['_envelope'] = envelope
        records.append(record)
    name = features.GetName()
    source = None
    return records, fields, name


def _coerce(column: Column, value):
    """A table value as the column's type: text as :func:`_convert` reads it,
    numbers and dates as they are (an accuracy rounds up to whole meters)."""
    if value is None:
        return None
    if isinstance(value, str):
        return _convert(column.name, value)
    if column.type == 'int':
        if isinstance(value, bool):
            raise ValueError(f'{value!r} is not an integer')
        if isinstance(value, float):
            if value != value:
                return None
            return math.ceil(value) if column.name in ACCURACY_COLUMNS else int(round(value))
        return int(value)
    if column.type == 'date':
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        raise ValueError(f'{value!r} is not a date')
    return str(value)


def _cell_from_text(text) -> str | None:
    match = CELL_PATTERN.search(str(text).upper()) if text is not None else None
    return match.group(1) if match else None


def parse_column_map(pairs: Iterable[str]) -> dict[str, str]:
    """``INDEX_COLUMN=TABLE_COLUMN`` pairs as a mapping."""
    mapping: dict[str, str] = {}
    for pair in pairs or ():
        if '=' not in pair:
            raise ValueError(f'--map {pair!r} is not INDEX_COLUMN=TABLE_COLUMN')
        name, source = pair.split('=', 1)
        mapping[name.strip()] = source.strip()
    return mapping


def parse_constants(pairs: Iterable[str]) -> dict[str, object]:
    """``INDEX_COLUMN=VALUE`` pairs as typed, checked column values."""
    constants: dict[str, object] = {}
    for pair in pairs or ():
        if '=' not in pair:
            raise ValueError(f'--set {pair!r} is not INDEX_COLUMN=VALUE')
        name, text = pair.split('=', 1)
        name = name.strip()
        if name == 'cell_id' or name not in COLUMNS_BY_NAME:
            raise ValueError(f'--set {name}: not an index column that can be set')
        try:
            value = _convert(name, text)
        except ValueError as e:
            raise ValueError(f'--set {name}: {e}') from e
        problem = check_value(COLUMNS_BY_NAME[name], value)
        if problem:
            raise ValueError(f'--set {name}: {problem}')
        constants[name] = value
    return constants


def rows_from_table(
    path: str,
    *,
    cell_field: str | None = None,
    layer: str | None = None,
    column_map: dict[str, str] | None = None,
    constants: dict[str, object] | None = None,
    prefer: str | None = None,
) -> TableImport:
    """A row per record of an attribute table (a footprint layer, a catalog,
    an exported table), its columns mapped onto the index's.

    A table column whose name matches an index column, case apart, fills it;
    *column_map* names the rest (index column to table column); *constants*
    fill a column with one value for every row (over a mapped column of the
    same name). The cell comes from *cell_field* (any text holding N38E045),
    else from a ``cell_id`` column, else from the geometry's southwest
    corner; a record with no cell is skipped. Values are read with the
    column's type (an accuracy rounds up to whole meters). When several
    records share a cell, *prefer* names the table column whose greatest
    value wins; without it they are an error. Table columns no index column
    took are dropped and listed.

    Raises:
        ValueError: If the table cannot be read, a mapping names an unknown
            column, a value does not fit its column, or cells are shared.
    """
    records, fields, layer_name = _table_records(path, layer)
    lower: dict[str, str] = {}
    for name in fields:
        lower.setdefault(name.lower(), name)
    mapped: dict[str, str] = {}
    for column in INDEX_COLUMNS:
        if column.name != 'cell_id' and column.name.lower() in lower:
            mapped[column.name] = lower[column.name.lower()]
    for name, source in (column_map or {}).items():
        if name == 'cell_id':
            raise ValueError('--map cell_id: name the column that holds the cell with --cell-field')
        if name not in COLUMNS_BY_NAME:
            raise ValueError(f'--map {name}: not an index column')
        if source not in fields:
            raise ValueError(f'--map {name}={source}: the table has no column {source!r}')
        mapped[name] = source
    constants = dict(constants or {})
    for name in constants:
        if name == 'cell_id' or name not in COLUMNS_BY_NAME:
            raise ValueError(f'--set {name}: not an index column that can be set')
        mapped.pop(name, None)

    field_for_cell = None
    if cell_field:
        if cell_field not in fields:
            raise ValueError(f'--cell-field {cell_field}: the table has no column {cell_field!r}')
        field_for_cell = cell_field
        cell_source = f'column {cell_field}'
    elif 'cell_id' in lower:
        field_for_cell = lower['cell_id']
        cell_source = f'column {field_for_cell}'
    elif any(record['_envelope'] is not None for record in records):
        cell_source = 'the geometry'
    elif records:
        raise ValueError('No cell for the rows: the table has no cell_id column and no geometry; give --cell-field')
    else:
        cell_source = 'none (the table is empty)'
    if prefer is not None and prefer not in fields:
        raise ValueError(f'--prefer {prefer}: the table has no column {prefer!r}')

    candidates: dict[str, list[tuple[dict, dict]]] = {}
    problems: list[str] = []
    without_cell = 0
    for number, record in enumerate(records, start=1):
        if field_for_cell is not None:
            cell = _cell_from_text(record.get(field_for_cell))
        else:
            envelope = record['_envelope']
            cell = make_cell_id(int(round(envelope[0])), int(round(envelope[1]))) if envelope else None
        if cell is None:
            without_cell += 1
            continue
        row = new_row(cell, updated=_now())
        if cell_field and 'source_id' not in mapped and 'source_id' not in constants:
            text = record.get(cell_field)
            row['source_id'] = None if text is None else str(text)
        for name, source in mapped.items():
            column = COLUMNS_BY_NAME[name]
            try:
                value = _coerce(column, record.get(source))
            except ValueError as e:
                problems.append(f'row {number} ({cell}): {name} <- {source}: {e}')
                continue
            problem = check_value(column, value)
            if problem:
                problems.append(f'row {number} ({cell}): {name} <- {source}: {problem}')
                continue
            row[name] = value
        row.update(constants)
        candidates.setdefault(cell, []).append((row, record))
    if problems:
        shown = problems[:10] + ([f'... and {len(problems) - 10} more'] if len(problems) > 10 else [])
        raise ValueError('The table holds values the index cannot take:\n  ' + '\n  '.join(shown))

    rows: dict[str, dict] = {}
    resolved = 0
    shared: list[str] = []
    for cell, items in candidates.items():
        if len(items) == 1:
            rows[cell] = items[0][0]
            continue
        if prefer is None:
            shared.append(f'{cell} ({len(items)} rows)')
            continue

        def rank(item):
            value = item[1].get(prefer)
            return (value is not None, value if value is not None else 0)

        best = max(items, key=rank)
        winners = [item for item in items if rank(item) == rank(best)]
        if len(winners) > 1:
            shared.append(f'{cell} ({len(items)} rows, a tie on {prefer})')
            continue
        rows[cell] = winners[0][0]
        resolved += 1
    if shared:
        shown = shared[:10] + ([f'... and {len(shared) - 10} more'] if len(shared) > 10 else [])
        hint = '' if prefer else '; --prefer COLUMN keeps the row with the greatest COLUMN'
        raise ValueError('Several rows share a cell: ' + ', '.join(shown) + hint)

    taken = set(mapped.values()) | {cell_field or '', field_for_cell or '', 'fid'}
    dropped = sorted(name for name in fields if name not in taken)
    return TableImport(rows, mapped, constants, dropped, cell_source, layer_name, len(records), without_cell, resolved)


def rows_from_footprints(path: str, cell_field: str | None = None, layer: str | None = None) -> dict[str, dict]:
    """A row per feature of a footprint layer: the cell id from *cell_field*,
    or from the feature's envelope when the field is not given. The same as
    :func:`rows_from_table` without a mapping."""
    return rows_from_table(path, cell_field=cell_field, layer=layer).rows


def import_report_lines(imported: TableImport, path: str) -> list[str]:
    """What the table import did, for the log."""
    where = os.path.basename(path) + (f' (layer {imported.layer})' if imported.layer else '')
    lines = [f'Table {where}: {imported.rows_read} row(s) read, {len(imported.rows)} cell(s), '
             f'the cell from {imported.cell_source}']
    if imported.rows_without_cell:
        lines.append(f'  {imported.rows_without_cell} row(s) without a cell were skipped')
    if imported.duplicates_resolved:
        lines.append(f'  {imported.duplicates_resolved} cell(s) had several rows; the preferred row was kept')
    for name, source in imported.mapped.items():
        lines.append(f'  {name} <- {source}')
    for name, value in imported.constants.items():
        lines.append(f'  {name} = {value!r} (constant)')
    if imported.dropped:
        lines.append('  dropped (no index column of that name): ' + ', '.join(imported.dropped))
    return lines


def merge_rows(base: dict[str, dict], incoming: dict[str, dict]) -> dict[str, dict]:
    """*incoming* values that are not NULL override *base*; other base values stay."""
    merged = {cell: dict(row) for cell, row in base.items()}
    for cell, row in incoming.items():
        target = merged.setdefault(cell, new_row(cell))
        for name, value in row.items():
            if value is not None or name not in target:
                target[name] = value
    return merged


def fill_from_profile(rows: dict[str, dict], profile: Profile) -> dict[str, int]:
    """Fill the profile's header values into every row that has NULL for them,
    so a complete table shows what the header gets; returns how many cells each
    column was filled in. A profile value that means NULL ("NA", a blank) fills
    nothing: NULL already says it. An accuracy column that some cell fills is
    left as it is, since its NULLs mean NA and never fall through to the profile."""
    filled: dict[str, int] = {}
    for column in INDEX_COLUMNS:
        if column.dted_key is None or column.name not in profile.product:
            continue
        if column.name in ACCURACY_COLUMNS and any(row.get(column.name) is not None for row in rows.values()):
            continue
        try:
            value = normalize_value(column, profile.product[column.name])
        except (TypeError, ValueError):
            continue  # the profile's own validation reports it
        if value is None:
            continue
        for row in rows.values():
            if row.get(column.name) is None:
                row[column.name] = value
                filled[column.name] = filled.get(column.name, 0) + 1
    return filled


def build_index(
    out: str,
    *,
    from_dted: Iterable[str] = (),
    from_rasters: Iterable[str] = (),
    from_footprints: str | None = None,
    from_table: str | None = None,
    cell_field: str | None = None,
    layer: str | None = None,
    column_map: dict[str, str] | None = None,
    constants: dict[str, object] | None = None,
    prefer: str | None = None,
    profile: Profile | None = None,
    level: int | None = None,
    update: bool = False,
    product: str | None = None,
    all_columns: bool = False,
) -> DtedIndex:
    """Harvest rows from the given sources and write (or update) the index at *out*.

    *from_table* (or *from_footprints*, the same thing) is any attribute
    table, imported through :func:`rows_from_table`; the import is reported
    in the log. Sources merge in the order table, rasters, DTED headers, a
    later non-NULL value over an earlier one. With *all_columns* the index
    holds every column, and the profile's constants fill the rows that have
    no value of their own (:func:`fill_from_profile`).
    """
    logger = _state.get_logger()
    rows: dict[str, dict] = {}
    subregions: dict[str, list[dict]] = {}
    meta: dict = {}
    if update and os.path.isfile(out):
        existing = read_index(out)
        if existing.duplicates:
            # Rewriting would keep one of each cell's rows without a word.
            raise ValueError(f'{os.path.basename(out)} holds several rows for {len(existing.duplicates)} cell(s) '
                             f'({", ".join(sorted(existing.duplicates)[:5])}); keep one row per cell, then update')
        rows, subregions, meta = existing.rows, existing.subregions, dict(existing.meta)
        meta.pop('created', None)
        meta.pop('cells', None)
    table = from_table or from_footprints
    if table:
        imported = rows_from_table(
            table, cell_field=cell_field, layer=layer, column_map=column_map, constants=constants, prefer=prefer,
        )
        for line in import_report_lines(imported, table):
            logger.info(line)
        rows = merge_rows(rows, imported.rows)
    if from_rasters:
        rows = merge_rows(rows, rows_from_rasters(list(from_rasters), profile))
    if from_dted:
        dted_rows, dted_subregions = rows_from_dted(list(from_dted))
        rows = merge_rows(rows, dted_rows)
        for cell, items in dted_subregions.items():
            subregions[cell] = items
    if level is None:
        level = profile.level if profile and profile.level is not None else None
    if level is None:
        levels = {row.get('dted_level') for row in rows.values() if row.get('dted_level') is not None}
        level = levels.pop() if len(levels) == 1 else None
    for row in rows.values():
        if row.get('dted_level') is None and level is not None:
            row['dted_level'] = level
    if all_columns and profile is not None:
        filled = fill_from_profile(rows, profile)
        if filled:
            logger.info('filled from the profile: ' + ', '.join(f'{name} ({n} cell(s))' for name, n in filled.items()))
    if product:
        meta['product'] = product
    if profile is not None:
        meta['profile'] = os.path.basename(profile.path)
    return write_index(out, rows, subregions, meta, level=level, all_columns=all_columns)
