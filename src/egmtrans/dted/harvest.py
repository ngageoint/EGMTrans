"""Build index rows from what is already at hand: the headers of existing DTED
files, the tags and XML sidecars of source rasters, or a footprint layer."""

from __future__ import annotations

import datetime as dt
import glob
import math
import os
import re
from collections.abc import Iterable

from egmtrans.dted.header import DtedHeader, read_header, yymm_to_iso
from egmtrans.dted.header import cell_id as make_cell_id
from egmtrans.dted.index import (
    ACCURACY_COLUMNS,
    COLUMNS_BY_NAME,
    DtedIndex,
    new_row,
    parse_date,
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


def _xml_values(path: str, namespaces: dict[str, str], fields: dict[str, dict]) -> dict[str, str | None]:
    """The first match of each XPath in the sidecar; lxml when available, ElementTree otherwise."""
    values: dict[str, str | None] = {}
    data = _safe_xml_bytes(path)
    try:
        from lxml import etree

        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
        tree = etree.ElementTree(etree.fromstring(data, parser))
        for column, mapping in fields.items():
            found = tree.xpath(mapping['xpath'], namespaces=namespaces)
            text = None
            if found:
                first = found[0]
                text = first if isinstance(first, str) else (first.text or '')
            values[column] = _extract(mapping, text)
        return values
    except ImportError:
        pass
    import xml.etree.ElementTree as ElementTree

    root = ElementTree.fromstring(data)
    for column, mapping in fields.items():
        xpath = mapping['xpath']
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
            raise ValueError(f'{column}: the XPath {mapping["xpath"]!r} needs lxml (pip install lxml): {e}') from e
        text = None
        if element is not None:
            text = element.get(attribute) if attribute else (element.text or '')
        values[column] = _extract(mapping, text)
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


def rows_from_footprints(path: str, cell_field: str | None = None, layer: str | None = None) -> dict[str, dict]:
    """A row per feature of a footprint layer: the cell id from *cell_field*,
    or from the feature's envelope when the field is not given."""
    from osgeo import ogr

    source = ogr.Open(path)
    if source is None:
        raise ValueError(f'Cannot open {path}')
    features = source.GetLayerByName(layer) if layer else source.GetLayer(0)
    if features is None:
        raise ValueError(f'{path} has no layer {layer!r}')
    rows: dict[str, dict] = {}
    for feature in features:
        if cell_field:
            text = feature.GetFieldAsString(cell_field)
            match = re.search(r'([NS]\d{2}[EW]\d{3})', text.upper())
            if not match:
                continue
            cell = match.group(1)
        else:
            geometry = feature.GetGeometryRef()
            if geometry is None:
                continue
            xmin, xmax, ymin, ymax = geometry.GetEnvelope()
            cell = make_cell_id(int(round(xmin)), int(round(ymin)))
        source_id = feature.GetFieldAsString(cell_field) if cell_field else None
        rows[cell] = new_row(cell, source_id=source_id, updated=_now())
    source = None
    return rows


def merge_rows(base: dict[str, dict], incoming: dict[str, dict]) -> dict[str, dict]:
    """*incoming* values that are not NULL override *base*; other base values stay."""
    merged = {cell: dict(row) for cell, row in base.items()}
    for cell, row in incoming.items():
        target = merged.setdefault(cell, new_row(cell))
        for name, value in row.items():
            if value is not None or name not in target:
                target[name] = value
    return merged


def build_index(
    out: str,
    *,
    from_dted: Iterable[str] = (),
    from_rasters: Iterable[str] = (),
    from_footprints: str | None = None,
    cell_field: str | None = None,
    profile: Profile | None = None,
    level: int | None = None,
    update: bool = False,
    product: str | None = None,
) -> DtedIndex:
    """Harvest rows from the given sources and write (or update) the index at *out*."""
    rows: dict[str, dict] = {}
    subregions: dict[str, list[dict]] = {}
    meta: dict = {}
    if update and os.path.isfile(out):
        existing = read_index(out)
        rows, subregions, meta = existing.rows, existing.subregions, dict(existing.meta)
        meta.pop('created', None)
        meta.pop('cells', None)
    if from_footprints:
        rows = merge_rows(rows, rows_from_footprints(from_footprints, cell_field))
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
    if product:
        meta['product'] = product
    if profile is not None:
        meta['profile'] = os.path.basename(profile.path)
    return write_index(out, rows, subregions, meta, level=level)
