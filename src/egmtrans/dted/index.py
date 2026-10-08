"""The DTED metadata index: one row per one-degree cell holding the header
values that cannot be derived from the raster, kept for a whole collection in
a GeoPackage or a GeoParquet file.

Layers:

- ``dted_cells`` (polygons, WGS 84): the per-cell header values and catalog
  columns, keyed by ``cell_id`` (N38E045).
- ``dted_acc_subregions`` (polygons): the accuracy subregions of a cell, up
  to nine, each with its four accuracies and an outline of 3 to 14 vertices.
- ``dted_index_meta`` (key/value): schema version, product, level, generator.

In a GeoParquet index the cells are the file itself, the subregions a sibling
``<name>_subregions.parquet``, and the meta the file's key-value metadata.
Dates are ISO dates in the index; the header assembler turns them into YYMM.
A column that is NULL in every row is not written, so the profile supplies
that field; a NULL among values means NA for an accuracy and "not from the
index" for any other field.

A GeoPackage without the cells layer is not an index, and neither is a
GeoParquet file with neither the index metadata nor a header column (a
catalog, say): reading one fails with a message that says so. A cell with
several rows is an error of :func:`validate_index`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import struct
from dataclasses import dataclass, field

from egmtrans.dted.fips import producer_code_warning
from egmtrans.dted.header import cell_id as make_cell_id
from egmtrans.dted.header import parse_cell_id
from egmtrans.dted.schema import (
    MAX_COORDINATES,
    MAX_SUBREGIONS,
    MIN_COORDINATES,
    SECURITY_CODES,
    VERTICAL_DATUM_CODES_ACCEPTED,
)
from egmtrans.dted.validate import Issue

SCHEMA_VERSION = 1
CELLS_LAYER = 'dted_cells'
SUBREGIONS_LAYER = 'dted_acc_subregions'
META_TABLE = 'dted_index_meta'
PARQUET_META_KEY = 'egmtrans_dted_index'
SUPPORTED_INDEX_EXTENSIONS = ('.gpkg', '.parquet')


@dataclass(frozen=True)
class Column:
    """A column of the cells layer.

    *dted_key* names the header field the column fills (None for catalog
    columns the writer ignores); *check_only* columns are compared with the
    derived value instead of written; *required* columns must be present in
    the index or the profile before a header can be assembled from scratch.
    """

    name: str
    type: str  # 'str', 'int' or 'date'
    dted_key: str | None = None
    max_len: int | None = None
    required: bool = False
    check_only: bool = False
    description: str = ''


INDEX_COLUMNS: tuple[Column, ...] = (
    Column('cell_id', 'str', None, 7, True, description='Cell identifier, N38E045; the key.'),
    Column('dted_level', 'int', None, description='0, 1 or 2; the level the row describes.'),
    Column('security_code', 'str', 'dsi.security_code', 1, True, description='U, R, C or S (UHL and DSI).'),
    Column('security_control', 'str', 'dsi.security_control', 2, description='Security control and release markings.'),
    Column('security_handling', 'str', 'dsi.security_handling', 27, description='Security handling description.'),
    Column('unique_ref_uhl', 'str', 'uhl.unique_ref', 12, description='UHL unique reference number.'),
    Column('unique_ref_dsi', 'str', 'dsi.unique_ref', 15, description='DSI unique reference number.'),
    Column('data_edition', 'int', 'dsi.data_edition', None, True, description='Data edition number, 1-99.'),
    Column('match_merge_version', 'str', 'dsi.match_merge_version', 1, True, description='Match/merge version, A-Z.'),
    Column('maintenance_date', 'date', 'dsi.maintenance_date', description='Maintenance date; NULL until used.'),
    Column('match_merge_date', 'date', 'dsi.match_merge_date', description='Match/merge date; NULL until used.'),
    Column('maintenance_code', 'str', 'dsi.maintenance_code', 4, description='Maintenance description code.'),
    Column('producer_code', 'str', 'dsi.producer_code', 8, True, description='Producer code, FIPS 10-4 country first.'),
    Column('product_spec', 'str', 'dsi.product_spec', 9, description='Product specification (PRF89020B).'),
    Column('product_spec_amend', 'str', 'dsi.product_spec_amend', 2, description='Amendment and change number.'),
    Column('product_spec_date', 'date', 'dsi.product_spec_date', description='Product specification date.'),
    Column('vertical_datum', 'str', 'dsi.vertical_datum', 3, check_only=True,
           description='Checked against the target datum, never written from here.'),
    Column('horizontal_datum', 'str', 'dsi.horizontal_datum', 5, check_only=True,
           description='Checked against WGS84, never written from here.'),
    Column('digitizing_system', 'str', 'dsi.digitizing_system', 10, description='Digitizing/collection system.'),
    Column('compilation_date', 'date', 'dsi.compilation_date', None, True, description='Compilation date.'),
    Column('abs_horiz_acc', 'int', 'acc.abs_horiz_acc', None, True, description='CE90 in meters; NULL is NA.'),
    Column('abs_vert_acc', 'int', 'acc.abs_vert_acc', None, True, description='LE90 in meters; NULL is NA.'),
    Column('rel_horiz_acc', 'int', 'acc.rel_horiz_acc', None, True, description='Point-to-point CE90; NULL is NA.'),
    Column('rel_vert_acc', 'int', 'acc.rel_vert_acc', None, True, description='Point-to-point LE90; NULL is NA.'),
    Column('acc_nima_reserved', 'str', 'acc.nima_reserved', 1, description='ACC character 24.'),
    Column('dsi_nima_text', 'str', 'dsi.nima_text', 101, description='DSI 292-392, reserved for NIMA use.'),
    Column('dsi_producer_text', 'str', 'dsi.producer_text', 100, description='DSI 393-492, producing nation use.'),
    Column('dsi_free_text', 'str', 'dsi.free_text', 156, description='DSI 493-648, free text comments.'),
    Column('partial_cell', 'int', None, description='Catalog: data coverage in percent (00 complete).'),
    Column('source_id', 'str', None, 64, description='Catalog: source tile identifier.'),
    Column('source_file', 'str', None, 255, description='Catalog: source raster file.'),
    Column('source_metadata_file', 'str', None, 255, description='Catalog: source metadata file.'),
    Column('source_date', 'date', None, description='Catalog: source date.'),
    Column('source_version', 'str', None, 32, description='Catalog: source version.'),
    Column('qc_status', 'str', None, 32, description='Catalog: quality status.'),
    Column('notes', 'str', None, 1024, description='Catalog: free notes.'),
    Column('updated', 'str', None, 32, description='Catalog: when the row was last written (ISO).'),
)
COLUMNS_BY_NAME: dict[str, Column] = {column.name: column for column in INDEX_COLUMNS}
HEADER_COLUMNS: tuple[Column, ...] = tuple(c for c in INDEX_COLUMNS if c.dted_key and not c.check_only)
ACCURACY_COLUMNS = ('abs_horiz_acc', 'abs_vert_acc', 'rel_horiz_acc', 'rel_vert_acc')
ALWAYS_WRITTEN = ('cell_id', 'dted_level', 'updated')

SUBREGION_COLUMNS: tuple[Column, ...] = (
    Column('cell_id', 'str', None, 7, True),
    Column('seq', 'int', None, None, True, description='1-9, the subregion number.'),
    Column('abs_horiz_acc', 'int', None, description='CE90 in meters; NULL is NA.'),
    Column('abs_vert_acc', 'int', None, description='LE90 in meters; NULL is NA.'),
    Column('rel_horiz_acc', 'int', None, description='Point-to-point CE90; NULL is NA.'),
    Column('rel_vert_acc', 'int', None, description='Point-to-point LE90; NULL is NA.'),
)

_RANGES = {
    'dted_level': (0, 2), 'data_edition': (1, 99), 'partial_cell': (0, 99), 'seq': (1, MAX_SUBREGIONS),
    'abs_horiz_acc': (0, 9999), 'abs_vert_acc': (0, 9999), 'rel_horiz_acc': (0, 9999), 'rel_vert_acc': (0, 9999),
}


def parse_date(value) -> dt.date | None:
    """A date from an ISO string ('2024-07', '2024-07-01', '2024/07/01'), a date, or None."""
    if value is None or value == '':
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    match = re.fullmatch(r'(\d{4})[-/](\d{2})(?:[-/](\d{2}))?(?:[T ].*)?', text)
    if not match:
        raise ValueError(f'not a date (YYYY-MM or YYYY-MM-DD): {value!r}')
    return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))


def check_value(column: Column, value) -> str | None:
    """Why *value* is not acceptable for *column*, or None when it is."""
    if value is None:
        return None
    if column.type == 'int':
        if isinstance(value, str) and column.name in ACCURACY_COLUMNS and value.strip().upper() == 'NA':
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and value.strip().lstrip('-').isdigit():
                value = int(value)
            elif isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                return f'{value!r} is not an integer'
        low, high = _RANGES.get(column.name, (None, None))
        if low is not None and not low <= value <= high:
            return f'{value} is outside {low}-{high}'
        return None
    if column.type == 'date':
        try:
            date = parse_date(value)
        except ValueError as e:
            return str(e)
        if date is not None and column.dted_key is not None:
            # A header date is written as YYMM: only the readers' century round-trips.
            from egmtrans.dted.header import check_year

            try:
                check_year(date.year)
            except ValueError as e:
                return str(e)
        return None
    if not isinstance(value, str):
        return f'{value!r} is not text'
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
        return f'{value!r} is not printable ASCII'
    if column.max_len is not None and len(value) > column.max_len:
        return f'{value!r} is longer than {column.max_len} characters'
    if column.name == 'security_code' and value not in SECURITY_CODES:
        return f'{value!r} is not one of U, R, C, S'
    if column.name == 'match_merge_version' and not re.fullmatch(r'[A-Z]', value):
        return f'{value!r} is not a letter A-Z'
    if column.name == 'vertical_datum' and value not in VERTICAL_DATUM_CODES_ACCEPTED:
        return f'{value!r} is not MSL, E96 or E08'
    if column.name == 'horizontal_datum' and value != 'WGS84':
        return f'{value!r} is not WGS84'
    if column.name == 'maintenance_code' and not re.fullmatch(r'0000|[A-Z]\d{3}', value):
        return f'{value!r} is not 0000 or ANNN'
    if column.name == 'product_spec_amend' and not re.fullmatch(r'\d{2}', value):
        return f'{value!r} is not two digits'
    return None


def normalize_value(column: Column, value):
    """The canonical Python value of a column: int, date, str or None.

    A blank string is NULL for every column but free text: a required text
    column (the producer code) holding blanks has no value.
    """
    if value is None or (isinstance(value, str) and value.strip() == '' and (column.type != 'str' or column.required)):
        return None
    if column.type == 'int':
        if isinstance(value, str) and value.strip().upper() == 'NA':
            return None
        return int(value)
    if column.type == 'date':
        return parse_date(value)
    return str(value)


def cell_polygon(cell_id: str) -> list[tuple[float, float]]:
    """The (lon, lat) ring of a cell, closed, counterclockwise."""
    lon0, lat0 = parse_cell_id(cell_id)
    return [(lon0, lat0), (lon0 + 1, lat0), (lon0 + 1, lat0 + 1), (lon0, lat0 + 1), (lon0, lat0)]


def polygon_wkt(ring: list[tuple[float, float]]) -> str:
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return 'POLYGON((' + ', '.join(f'{x:.9g} {y:.9g}' for x, y in ring) + '))'


def polygon_wkb(ring: list[tuple[float, float]]) -> bytes:
    """Little-endian WKB of one polygon ring of (x, y) pairs."""
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    body = struct.pack('<BII', 1, 3, 1) + struct.pack('<I', len(ring))
    for x, y in ring:
        body += struct.pack('<dd', x, y)
    return body


def parse_polygon_wkb(blob: bytes) -> list[tuple[float, float]]:
    """The outer ring (closing vertex dropped) of a WKB polygon or multipolygon."""
    order = '<' if blob[0] == 1 else '>'
    geometry_type = struct.unpack(f'{order}I', blob[1:5])[0] & 0xFF
    position = 5
    if geometry_type == 6:  # MultiPolygon: first polygon
        position += 4 + 5
    elif geometry_type != 3:
        raise ValueError(f'WKB geometry type {geometry_type} is not a polygon')
    ring_count = struct.unpack(f'{order}I', blob[position:position + 4])[0]
    if ring_count < 1:
        return []
    position += 4
    point_count = struct.unpack(f'{order}I', blob[position:position + 4])[0]
    position += 4
    ring = []
    for _ in range(point_count):
        x, y = struct.unpack(f'{order}dd', blob[position:position + 16])
        ring.append((x, y))
        position += 16
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


@dataclass
class DtedIndex:
    """A loaded index: rows by cell id, subregions by cell id, the meta table,
    and the cells the file holds more than one row for (with how many; the
    last row read is the one in *rows*)."""

    path: str
    rows: dict[str, dict] = field(default_factory=dict)
    subregions: dict[str, list[dict]] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, cell_id: str) -> dict | None:
        return self.rows.get(cell_id.upper())

    def subregions_of(self, cell_id: str) -> list[dict]:
        return sorted(self.subregions.get(cell_id.upper(), []), key=lambda s: s['seq'])

    @property
    def level(self) -> int | None:
        value = self.meta.get('dted_level')
        return int(value) if value not in (None, '') else None

    @property
    def columns(self) -> set[str]:
        """The columns present in the cells layer."""
        for row in self.rows.values():
            return set(row)
        return set(self.meta.get('columns', '').split(',')) - {''}


def _add_row(index: DtedIndex, cell: str, row: dict) -> None:
    """A row read from a file, counting a cell that comes again."""
    if cell in index.rows:
        index.duplicates[cell] = index.duplicates.get(cell, 1) + 1
    index.rows[cell] = row


def _not_an_index(path: str, reason: str, names: list[str], kind: str) -> ValueError:
    shown = ', '.join(names[:8]) + (f' and {len(names) - 8} more' if len(names) > 8 else '')
    return ValueError(f'{os.path.basename(path)} is not a DTED metadata index: {reason}'
                      + (f' (its {kind}: {shown})' if names else '')
                      + '. "egmtrans dted-index build --from-table" makes an index from a table.')


def _holds_header_column(names) -> bool:
    return any(COLUMNS_BY_NAME[name].dted_key for name in names if name in COLUMNS_BY_NAME)


def _driver_for(path: str) -> str:
    extension = os.path.splitext(path)[1].lower()
    if extension == '.gpkg':
        return 'gpkg'
    if extension == '.parquet':
        return 'parquet'
    raise ValueError(f'An index is a .gpkg or .parquet file, not {os.path.basename(path)}')


def _meta_for(rows: dict[str, dict], meta: dict | None, level: int | None) -> dict[str, str]:
    """The meta table to write: defaults plus whatever the caller passed."""
    from egmtrans._version import __version__

    merged = {
        'schema_version': str(SCHEMA_VERSION),
        'created': dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        'generator': f'EGMTrans {__version__}',
        'cells': str(len(rows)),
    }
    for key, value in (meta or {}).items():
        if value is not None:
            merged[str(key)] = str(value)
    if level is not None:
        merged['dted_level'] = str(level)
    return merged


def columns_to_write(rows: dict[str, dict]) -> tuple[Column, ...]:
    """The index columns an index holds: the key, the level and the timestamp
    always, any other column only when some row has a value. An absent column
    is one the profile supplies; a NULL among values means NA (accuracies) or
    "nothing from the index" (the rest)."""
    return tuple(
        column for column in INDEX_COLUMNS
        if column.name in ALWAYS_WRITTEN or any(row.get(column.name) is not None for row in rows.values())
    )


def _prepare_rows(rows: dict[str, dict] | list[dict]) -> dict[str, dict]:
    """Rows keyed by upper-case cell id, values normalized, every column present."""
    if isinstance(rows, list):
        rows = {row['cell_id']: row for row in rows}
    prepared = {}
    for key, row in rows.items():
        cell = str(row.get('cell_id') or key).upper()
        parse_cell_id(cell)
        clean = {}
        for column in INDEX_COLUMNS:
            value = row.get(column.name)
            problem = check_value(column, value)
            if problem:
                raise ValueError(f'{cell} {column.name}: {problem}')
            clean[column.name] = normalize_value(column, value)
        clean['cell_id'] = cell
        extra = set(row) - set(COLUMNS_BY_NAME)
        for name in sorted(extra):
            clean[name] = row[name]
        prepared[cell] = clean
    return prepared


def _prepare_subregions(subregions: dict[str, list[dict]] | list[dict] | None) -> dict[str, list[dict]]:
    """Subregions by cell id, outlines as (lat, lon) lists, accuracies normalized."""
    if not subregions:
        return {}
    if isinstance(subregions, dict):
        flat = [dict(s, cell_id=cell) for cell, items in subregions.items() for s in items]
    else:
        flat = list(subregions)
    prepared: dict[str, list[dict]] = {}
    for item in flat:
        cell = str(item['cell_id']).upper()
        parse_cell_id(cell)
        outline = [(float(lat), float(lon)) for lat, lon in item['outline']]
        if not MIN_COORDINATES <= len(outline) <= MAX_COORDINATES:
            raise ValueError(f'{cell} subregion {item.get("seq")}: 3 to 14 vertices, not {len(outline)}')
        clean = {'cell_id': cell, 'seq': int(item['seq']), 'outline': outline}
        for column in SUBREGION_COLUMNS[2:]:
            value = item.get(column.name)
            problem = check_value(column, value)
            if problem:
                raise ValueError(f'{cell} subregion {clean["seq"]} {column.name}: {problem}')
            clean[column.name] = normalize_value(column, value)
        prepared.setdefault(cell, []).append(clean)
    return prepared


# GeoPackage through OGR

def _ogr_type(column: Column):
    from osgeo import ogr

    return {'str': ogr.OFTString, 'int': ogr.OFTInteger, 'date': ogr.OFTDate}[column.type]


def _set_ogr_field(feature, column: Column, value) -> None:
    if value is None:
        feature.SetFieldNull(column.name)
    elif column.type == 'date':
        feature.SetField(column.name, value.year, value.month, value.day, 0, 0, 0, 0)
    else:
        feature.SetField(column.name, value)


def _write_gpkg(path: str, rows: dict[str, dict], subregions: dict[str, list[dict]], meta: dict[str, str],
                columns: tuple[Column, ...] = INDEX_COLUMNS) -> None:
    from osgeo import ogr, osr

    driver = ogr.GetDriverByName('GPKG')
    if os.path.exists(path):
        driver.DeleteDataSource(path)
    source = driver.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    extra_columns = sorted({name for row in rows.values() for name in row} - set(COLUMNS_BY_NAME))

    cells = source.CreateLayer(CELLS_LAYER, srs, ogr.wkbPolygon, ['GEOMETRY_NAME=geometry'])
    for column in columns:
        definition = ogr.FieldDefn(column.name, _ogr_type(column))
        if column.max_len:
            definition.SetWidth(column.max_len)
        cells.CreateField(definition)
    for name in extra_columns:
        cells.CreateField(ogr.FieldDefn(name, ogr.OFTString))
    layer_definition = cells.GetLayerDefn()
    for cell, row in sorted(rows.items()):
        feature = ogr.Feature(layer_definition)
        for column in columns:
            _set_ogr_field(feature, column, row.get(column.name))
        for name in extra_columns:
            value = row.get(name)
            if value is None:
                feature.SetFieldNull(name)
            else:
                feature.SetField(name, str(value))
        feature.SetGeometry(ogr.CreateGeometryFromWkt(polygon_wkt(cell_polygon(cell))))
        cells.CreateFeature(feature)
        feature = None

    outlines = source.CreateLayer(SUBREGIONS_LAYER, srs, ogr.wkbPolygon, ['GEOMETRY_NAME=geometry'])
    for column in SUBREGION_COLUMNS:
        definition = ogr.FieldDefn(column.name, _ogr_type(column))
        if column.max_len:
            definition.SetWidth(column.max_len)
        outlines.CreateField(definition)
    outline_definition = outlines.GetLayerDefn()
    for cell in sorted(subregions):
        for item in sorted(subregions[cell], key=lambda s: s['seq']):
            feature = ogr.Feature(outline_definition)
            for column in SUBREGION_COLUMNS:
                _set_ogr_field(feature, column, item.get(column.name))
            ring = [(lon, lat) for lat, lon in item['outline']]
            feature.SetGeometry(ogr.CreateGeometryFromWkt(polygon_wkt(ring)))
            outlines.CreateFeature(feature)
            feature = None

    table = source.CreateLayer(META_TABLE, None, ogr.wkbNone)
    table.CreateField(ogr.FieldDefn('key', ogr.OFTString))
    table.CreateField(ogr.FieldDefn('value', ogr.OFTString))
    table_definition = table.GetLayerDefn()
    for key, value in meta.items():
        feature = ogr.Feature(table_definition)
        feature.SetField('key', key)
        feature.SetField('value', value)
        table.CreateFeature(feature)
        feature = None
    source = None


def _ogr_value(feature, index: int, column: Column | None):
    if not feature.IsFieldSetAndNotNull(index):
        return None
    if column is not None and column.type == 'date':
        return parse_date(feature.GetFieldAsString(index))
    if column is not None and column.type == 'int':
        return feature.GetFieldAsInteger(index)
    return feature.GetFieldAsString(index)


def _read_ogr(path: str, driver_name: str | None = None) -> DtedIndex:
    from osgeo import ogr

    source = ogr.Open(path) if driver_name is None else ogr.GetDriverByName(driver_name).Open(path)
    if source is None:
        raise ValueError(f'Cannot open index {path}')
    index = DtedIndex(path=path)
    cells = source.GetLayerByName(CELLS_LAYER) if driver_name is None else source.GetLayer(0)
    if cells is None:
        layers = [source.GetLayer(i).GetName() for i in range(source.GetLayerCount())]
        raise _not_an_index(path, f'it has no {CELLS_LAYER} layer', layers, 'layers')
    definition = cells.GetLayerDefn()
    names = [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())]
    if driver_name == 'Parquet':
        # GDAL serves the Parquet file's key-value metadata in this domain.
        raw_meta = cells.GetMetadataItem(PARQUET_META_KEY, '_PARQUET_METADATA_')
        if raw_meta:
            index.meta = {k: str(v) for k, v in json.loads(raw_meta).items()}
        elif not _holds_header_column(names):
            raise _not_an_index(path, 'it has neither the index metadata nor a header column', names, 'columns')
    for feature in cells:
        row = {}
        for i, name in enumerate(names):
            row[name] = _ogr_value(feature, i, COLUMNS_BY_NAME.get(name))
        cell = str(row.get('cell_id') or '').upper()
        if not cell:
            continue
        row['cell_id'] = cell
        _add_row(index, cell, row)
    outlines = source.GetLayerByName(SUBREGIONS_LAYER) if driver_name is None else None
    if outlines is not None:
        definition = outlines.GetLayerDefn()
        names = [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())]
        columns = {c.name: c for c in SUBREGION_COLUMNS}
        for feature in outlines:
            item = {name: _ogr_value(feature, i, columns.get(name)) for i, name in enumerate(names)}
            geometry = feature.GetGeometryRef()
            ring = parse_polygon_wkb(bytes(geometry.ExportToWkb())) if geometry is not None else []
            item['outline'] = [(lat, lon) for lon, lat in ring]
            cell = str(item.get('cell_id') or '').upper()
            item['cell_id'] = cell
            index.subregions.setdefault(cell, []).append(item)
    table = source.GetLayerByName(META_TABLE) if driver_name is None else None
    if table is not None:
        for feature in table:
            index.meta[feature.GetFieldAsString('key')] = feature.GetFieldAsString('value')
    source = None
    return index


# GeoParquet through pyarrow

def _parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return False
    return True


def _subregions_path(path: str) -> str:
    stem, extension = os.path.splitext(path)
    return f'{stem}_subregions{extension}'


def _geo_metadata(bbox: list[float]) -> dict:
    return {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {
            'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon'], 'bbox': bbox},
        },
    }


def _write_parquet(path: str, rows: dict[str, dict], subregions: dict[str, list[dict]], meta: dict[str, str],
                   columns: tuple[Column, ...] = INDEX_COLUMNS) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    types = {'str': pa.string(), 'int': pa.int32(), 'date': pa.date32()}
    extra_columns = sorted({name for row in rows.values() for name in row} - set(COLUMNS_BY_NAME))
    ordered = sorted(rows)
    arrays = {column.name: pa.array([rows[c].get(column.name) for c in ordered], type=types[column.type])
              for column in columns}
    for name in extra_columns:
        values = [None if rows[c].get(name) is None else str(rows[c][name]) for c in ordered]
        arrays[name] = pa.array(values, pa.string())
    rings = [cell_polygon(c) for c in ordered]
    arrays['geometry'] = pa.array([polygon_wkb(ring) for ring in rings], pa.binary())
    bbox = ([min(x for r in rings for x, _ in r), min(y for r in rings for _, y in r),
             max(x for r in rings for x, _ in r), max(y for r in rings for _, y in r)] if rings else [0, 0, 0, 0])
    columns_meta = dict(meta, columns=','.join(list(arrays)))
    table = pa.table(arrays).replace_schema_metadata({
        'geo': json.dumps(_geo_metadata(bbox)),
        PARQUET_META_KEY: json.dumps(columns_meta),
    })
    pq.write_table(table, path)

    sub_path = _subregions_path(path)
    if subregions:
        flat = [item for cell in sorted(subregions) for item in sorted(subregions[cell], key=lambda s: s['seq'])]
        sub_arrays = {column.name: pa.array([item.get(column.name) for item in flat], type=types[column.type])
                      for column in SUBREGION_COLUMNS}
        sub_rings = [[(lon, lat) for lat, lon in item['outline']] for item in flat]
        sub_arrays['geometry'] = pa.array([polygon_wkb(ring) for ring in sub_rings], pa.binary())
        sub_bbox = [min(x for r in sub_rings for x, _ in r), min(y for r in sub_rings for _, y in r),
                    max(x for r in sub_rings for x, _ in r), max(y for r in sub_rings for _, y in r)]
        sub_table = pa.table(sub_arrays).replace_schema_metadata({
            'geo': json.dumps(_geo_metadata(sub_bbox)), PARQUET_META_KEY: json.dumps(meta),
        })
        pq.write_table(sub_table, sub_path)
    elif os.path.exists(sub_path):
        os.remove(sub_path)


def _read_parquet(path: str) -> DtedIndex:
    import pyarrow.parquet as pq

    schema = pq.read_schema(path)
    index = DtedIndex(path=path)
    metadata = schema.metadata or {}
    raw_meta = metadata.get(PARQUET_META_KEY.encode())
    if raw_meta:
        index.meta = {k: str(v) for k, v in json.loads(raw_meta).items()}
    elif not _holds_header_column(schema.names):
        names = [name for name in schema.names if name != 'geometry']
        raise _not_an_index(path, 'it has neither the index metadata nor a header column', names, 'columns')
    for record in pq.read_table(path).to_pylist():
        record.pop('geometry', None)
        cell = str(record.get('cell_id') or '').upper()
        if not cell:
            continue
        for column in INDEX_COLUMNS:
            if column.name in record and column.type == 'date':
                record[column.name] = parse_date(record[column.name])
        record['cell_id'] = cell
        _add_row(index, cell, record)
    sub_path = _subregions_path(path)
    if os.path.isfile(sub_path):
        for record in pq.read_table(sub_path).to_pylist():
            blob = record.pop('geometry', None)
            ring = parse_polygon_wkb(blob) if blob else []
            record['outline'] = [(lat, lon) for lon, lat in ring]
            cell = str(record.get('cell_id') or '').upper()
            record['cell_id'] = cell
            index.subregions.setdefault(cell, []).append(record)
    return index


def write_index(
    path: str,
    rows: dict[str, dict] | list[dict],
    subregions: dict[str, list[dict]] | list[dict] | None = None,
    meta: dict | None = None,
    *,
    level: int | None = None,
) -> DtedIndex:
    """Write an index (.gpkg or .parquet) and return it as loaded.

    *rows* map cell ids to column values (missing columns are NULL); each
    subregion is a dict with ``cell_id``, ``seq``, the four accuracies and an
    ``outline`` of (lat, lon) pairs. A column that is NULL in every row is
    left out of the file (see :func:`columns_to_write`), and out of the
    returned index.
    """
    driver = _driver_for(path)
    prepared_rows = _prepare_rows(rows)
    prepared_subregions = _prepare_subregions(subregions)
    for cell in prepared_subregions:
        if cell not in prepared_rows:
            raise ValueError(f'Subregions for {cell}, which has no row in the index')
    table = _meta_for(prepared_rows, meta, level)
    columns = columns_to_write(prepared_rows)
    if driver == 'gpkg':
        _write_gpkg(path, prepared_rows, prepared_subregions, table, columns)
    elif _parquet_available():
        _write_parquet(path, prepared_rows, prepared_subregions, table, columns)
    else:
        raise RuntimeError(
            'Writing a GeoParquet index needs pyarrow (pip install pyarrow, or the egmtrans[index] extra)'
        )
    written = {column.name for column in columns}
    rows_as_written = {
        cell: {name: value for name, value in row.items() if name in written or name not in COLUMNS_BY_NAME}
        for cell, row in prepared_rows.items()
    }
    return DtedIndex(path=path, rows=rows_as_written, subregions=prepared_subregions, meta=table)


def read_index(path: str) -> DtedIndex:
    """Load an index from a .gpkg or .parquet file."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f'Index not found: {path}')
    driver = _driver_for(path)
    if driver == 'gpkg':
        return _read_ogr(path)
    if _parquet_available():
        return _read_parquet(path)
    from osgeo import ogr

    if ogr.GetDriverByName('Parquet') is not None:
        index = _read_ogr(path, 'Parquet')
        sub_path = _subregions_path(path)
        if os.path.isfile(sub_path):
            source = ogr.GetDriverByName('Parquet').Open(sub_path)
            layer = source.GetLayer(0)
            definition = layer.GetLayerDefn()
            names = [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())]
            columns = {c.name: c for c in SUBREGION_COLUMNS}
            for feature in layer:
                item = {name: _ogr_value(feature, i, columns.get(name)) for i, name in enumerate(names)}
                geometry = feature.GetGeometryRef()
                ring = parse_polygon_wkb(bytes(geometry.ExportToWkb())) if geometry is not None else []
                item['outline'] = [(lat, lon) for lon, lat in ring]
                cell = str(item.get('cell_id') or '').upper()
                item['cell_id'] = cell
                index.subregions.setdefault(cell, []).append(item)
            source = None
        return index
    raise RuntimeError('Reading a GeoParquet index needs pyarrow or a GDAL with the Parquet driver')


def validate_index(index: DtedIndex, *, level: int | None = None) -> list[Issue]:
    """Problems with an index: cells with several rows, bad keys, values out
    of range, level mismatch, subregion rules, missing required columns."""
    issues: list[Issue] = []
    if index.duplicates:
        shown = ', '.join(f'{cell} ({count} rows)' for cell, count in sorted(index.duplicates.items())[:5])
        more = f' and {len(index.duplicates) - 5} more' if len(index.duplicates) > 5 else ''
        issues.append(Issue('error', 'INDEX', 'cell_id', f'{len(index.duplicates)} cell(s) have several rows: '
                                                         f'{shown}{more}; an index holds one row per cell'))
    present = index.columns
    for column in INDEX_COLUMNS:
        if column.required and column.name not in present:
            issues.append(Issue('warning', 'INDEX', column.name,
                                'required column is missing; the profile must supply it for every cell'))
    if level is not None and index.level is not None and index.level != level:
        issues.append(Issue('error', 'INDEX', 'dted_level', f'the index is for level {index.level}, not {level}'))
    blank_required: dict[str, list[str]] = {}
    nation_warnings: dict[str, list[str]] = {}
    for cell, row in sorted(index.rows.items()):
        try:
            parse_cell_id(cell)
        except ValueError as e:
            issues.append(Issue('error', 'INDEX', cell, str(e)))
            continue
        for column in INDEX_COLUMNS:
            if column.name not in row:
                continue
            problem = check_value(column, row[column.name])
            if problem:
                issues.append(Issue('error', 'INDEX', f'{cell}.{column.name}', problem))
        row_level = row.get('dted_level')
        if row_level is not None and level is not None and int(row_level) != level:
            issues.append(Issue('error', 'INDEX', f'{cell}.dted_level',
                                f'{row_level} is not the requested level {level}'))
        for name in ACCURACY_COLUMNS:
            if name in row and row[name] is None:
                issues.append(Issue('info', 'INDEX', f'{cell}.{name}', 'NULL, so the header will say NA'))
        for column in HEADER_COLUMNS:
            if column.required and column.name not in ACCURACY_COLUMNS and column.name in row:
                value = row[column.name]
                if value is None or (isinstance(value, str) and not value.strip()):
                    blank_required.setdefault(column.name, []).append(cell)
        producer = row.get('producer_code')
        if isinstance(producer, str) and producer.strip():
            nation = producer_code_warning(producer)
            if nation:
                nation_warnings.setdefault(nation, []).append(cell)
    for name, cells in sorted(blank_required.items()):
        shown = ', '.join(cells[:5]) + (f' and {len(cells) - 5} more' if len(cells) > 5 else '')
        issues.append(Issue('warning', 'INDEX', name,
                            f'NULL or blank in {len(cells)} cell(s) ({shown}); the profile must supply it'))
    for message, cells in sorted(nation_warnings.items()):
        shown = ', '.join(cells[:5]) + (f' and {len(cells) - 5} more' if len(cells) > 5 else '')
        issues.append(Issue('warning', 'INDEX', 'producer_code', f'{message}; {len(cells)} cell(s): {shown}'))
    for cell, items in sorted(index.subregions.items()):
        if cell not in index.rows:
            issues.append(Issue('error', 'INDEX', cell, 'subregions for a cell that has no row'))
        sequence = sorted(item.get('seq') or 0 for item in items)
        if len(items) > MAX_SUBREGIONS:
            issues.append(Issue('error', 'INDEX', cell, f'{len(items)} subregions; the maximum is {MAX_SUBREGIONS}'))
        if len(items) == 1:
            issues.append(Issue('error', 'INDEX', cell, 'one subregion; the outline flag allows 00 or 02-09'))
        if sequence != list(range(1, len(items) + 1)):
            issues.append(Issue('error', 'INDEX', cell, f'subregion seq values {sequence} are not 1..{len(items)}'))
        try:
            lon0, lat0 = parse_cell_id(cell)
        except ValueError:
            continue
        for item in items:
            where = f'{cell} subregion {item.get("seq")}'
            outline = item.get('outline') or []
            if not MIN_COORDINATES <= len(outline) <= MAX_COORDINATES:
                issues.append(Issue('error', 'INDEX', where, f'{len(outline)} vertices; 3 to 14 are allowed'))
            if any(not (lat0 - 1e-9 <= lat <= lat0 + 1 + 1e-9 and lon0 - 1e-9 <= lon <= lon0 + 1 + 1e-9)
                   for lat, lon in outline):
                issues.append(Issue('warning', 'INDEX', where, 'the outline leaves the cell'))
            for column in SUBREGION_COLUMNS[2:]:
                problem = check_value(column, item.get(column.name))
                if problem:
                    issues.append(Issue('error', 'INDEX', f'{where}.{column.name}', problem))
    return issues


def new_row(cell_id: str, **values) -> dict:
    """A row with every column NULL except *values*."""
    row = {column.name: None for column in INDEX_COLUMNS}
    row['cell_id'] = make_cell_id(*parse_cell_id(cell_id))
    row.update(values)
    return row
