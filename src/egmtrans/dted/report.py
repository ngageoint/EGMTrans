"""Report the contents of a DTED header as a table (Start, End, Length, Title,
Value, Description) in plain text, Markdown, CSV or JSON, with the level
evidence, a summary and the validation findings."""

from __future__ import annotations

import csv
import io
import json
import os
import textwrap
from dataclasses import asdict, dataclass

from egmtrans.dted import schema
from egmtrans.dted.header import (
    DtedHeader,
    LevelDetection,
    cell_geometry_of,
    decode_accuracy,
    parse_dms,
    yymm_to_iso,
)
from egmtrans.dted.schema import (
    ACC_FIELDS,
    COORDINATE_FIELDS,
    COORDINATE_LENGTH,
    DSI_FIELDS,
    LEVELS,
    MAX_SUBREGIONS,
    RECORD_OFFSETS,
    RECORD_TITLES,
    SUBREGION_COORDS_START,
    SUBREGION_FIELDS,
    SUBREGION_LENGTH,
    SUBREGION_START,
    UHL_FIELDS,
    ZONE_NAMES,
    Field,
)
from egmtrans.dted.validate import Issue, count

FORMATS = ('text', 'json', 'csv', 'md')
COLUMNS = ('Start', 'End', 'Length', 'Title', 'Value', 'Description')


@dataclass
class Row:
    """One line of the report: a header field, or one part of a subregion."""

    record: str
    key: str
    start: int
    end: int
    length: int
    title: str
    value: str
    description: str
    raw: str
    decoded: str | int | float | None
    file_offset: int

    def columns(self) -> tuple:
        return (self.start, self.end, self.length, self.title, self.value, self.description)


def display_value(item: Field, raw: str) -> str:
    """How a raw field reads in the table: blank fill shown as (blank),
    padding of right-justified values kept visible with quotes, bytes that
    are not printable escaped."""
    if raw.strip() == '':
        return '(blank)'
    escaped = ''.join(ch if 32 <= ord(ch) <= 126 else f'\\x{ord(ch):02x}' for ch in raw)
    if item.justify == 'right' or item.kind == 'accuracy':
        return f'"{escaped}"' if escaped != escaped.strip() else escaped
    return escaped.rstrip()


def decode_value(item: Field, raw: str):
    """The typed value of a field for JSON: meters, degrees, a date, an int, or the stripped text."""
    kind = item.kind
    try:
        if kind == 'accuracy':
            return decode_accuracy(raw)
        if kind in ('uhl_lon', 'uhl_lat', 'dsi_lat', 'dsi_lon', 'corner_lat', 'corner_lon'):
            return parse_dms(raw, kind)
        if kind == 'date':
            return yymm_to_iso(raw)
        if kind in ('interval', 'count', 'edition', 'partial', 'outline_flag', 'flag1', 'coord_count', 'spec_amend'):
            return int(raw) if raw.strip().isdigit() else None
    except ValueError:
        return None
    text = raw.strip()
    return text if text else None


def describe(item: Field, raw: str) -> str:
    """The field's description, followed by the decoded value where that helps."""
    decoded = decode_value(item, raw)
    extra = ''
    if item.kind in ('uhl_lon', 'uhl_lat', 'dsi_lat', 'dsi_lon', 'corner_lat', 'corner_lon') and decoded is not None:
        extra = f'= {decoded:.6f} deg'
    elif item.kind == 'interval' and decoded is not None:
        extra = f'= {decoded / 10:g} arc seconds'
    elif item.kind == 'date' and decoded:
        extra = f'= {decoded}'
    elif item.kind == 'accuracy':
        if decoded is not None:
            extra = f'= {decoded} m'
        elif raw.strip() == 'NA':
            extra = 'not available'
    elif item.kind in ('security1', 'security3') and raw.strip() in schema.SECURITY_CODE_NAMES:
        extra = f'= {schema.SECURITY_CODE_NAMES[raw.strip()]}'
    return f'{item.description} {extra}'.rstrip() if extra else item.description


def _row(item: Field, raw: str, start: int, file_offset: int, zero_based: bool, title: str | None = None,
         key: str | None = None) -> Row:
    shift = 1 if zero_based else 0
    return Row(
        record=item.record, key=key or item.key, start=start - shift, end=start + len(raw) - 1 - shift,
        length=len(raw), title=title or item.title, value=display_value(item, raw),
        description=describe(item, raw), raw=raw, decoded=decode_value(item, raw), file_offset=file_offset,
    )


def _subregion_rows(header: DtedHeader, zero_based: bool) -> list[Row]:
    """Rows for the ACC subregion block: every announced subregion field by
    field, then one row for whatever of the block is unused."""
    rows: list[Row] = []
    block_item = next(item for item in ACC_FIELDS if item.key == 'acc.subregions')
    block = header[block_item.key]
    announced = header.outline_count()
    shift = 1 if zero_based else 0
    acc_offset = RECORD_OFFSETS['ACC']
    for index in range(announced):
        base = SUBREGION_START + index * SUBREGION_LENGTH  # 1-based within ACC
        text = block[index * SUBREGION_LENGTH:(index + 1) * SUBREGION_LENGTH]
        for item in SUBREGION_FIELDS:
            raw = text[item.offset:item.offset + item.length]
            rows.append(_row(item, raw, base + item.offset, acc_offset + base + item.offset - 1, zero_based,
                             title=f'Subregion {index + 1}: {item.title[0].lower()}{item.title[1:]}',
                             key=f'acc.subregion_{index + 1}.{item.key}'))
        for pair in range(schema.MAX_COORDINATES):
            pair_start = base + SUBREGION_COORDS_START - 1 + pair * COORDINATE_LENGTH
            pair_text = text[SUBREGION_COORDS_START - 1 + pair * COORDINATE_LENGTH:
                             SUBREGION_COORDS_START - 1 + (pair + 1) * COORDINATE_LENGTH]
            if pair_text.strip() == '':
                used = SUBREGION_COORDS_START - 1 + pair * COORDINATE_LENGTH
                remaining = SUBREGION_LENGTH - used
                if remaining > 0:
                    rows.append(Row('ACC', f'acc.subregion_{index + 1}.unused', pair_start - shift,
                                    pair_start + remaining - 1 - shift, remaining,
                                    f'Subregion {index + 1}: unused coordinate pairs',
                                    '(blank)' if text[used:].strip() == '' else '(not blank)',
                                    'Blank filled after the last coordinate pair.', '', None,
                                    acc_offset + pair_start - 1))
                break
            for item in COORDINATE_FIELDS:
                raw = pair_text[item.offset:item.offset + item.length]
                title = f'Subregion {index + 1}, coordinate {pair + 1}: {item.title.lower()}'
                rows.append(_row(item, raw, pair_start + item.offset, acc_offset + pair_start + item.offset - 1,
                                 zero_based, title=title,
                                 key=f'acc.subregion_{index + 1}.coordinate_{pair + 1}.{item.key}'))
    used = announced * SUBREGION_LENGTH
    if used < len(block):
        start = SUBREGION_START + used
        rest = block[used:]
        title = ('Accuracy subregion descriptions (none announced)' if announced == 0
                 else f'Subregions {announced + 1}-{MAX_SUBREGIONS} (unused)')
        rows.append(Row('ACC', 'acc.subregions_unused', start - shift, start + len(rest) - 1 - shift, len(rest),
                        title, '(blank)' if rest.strip() == '' else display_value(block_item, rest[:60]) + '...',
                        block_item.description, '', None, acc_offset + start - 1))
    return rows


def header_rows(header: DtedHeader, *, zero_based: bool = False) -> dict[str, list[Row]]:
    """The report rows of each record, the ACC subregion block expanded."""
    rows: dict[str, list[Row]] = {}
    for record, fields in (('UHL', UHL_FIELDS), ('DSI', DSI_FIELDS), ('ACC', ACC_FIELDS)):
        record_rows = []
        for item in fields:
            if item.kind == 'subregions':
                record_rows.extend(_subregion_rows(header, zero_based))
                continue
            record_rows.append(_row(item, header[item.key], item.start, item.file_offset, zero_based))
        rows[record] = record_rows
    return rows


def summarize(header: DtedHeader, detection: LevelDetection, issues: list[Issue]) -> dict:
    """The key facts of a header for the summary block and the JSON report."""
    cell = cell_geometry_of(header)

    def acc(key: str):
        try:
            return header.accuracy(key)
        except ValueError:
            return header[key]

    level = detection.level
    return {
        'level': level,
        'series': LEVELS[level].series if level in LEVELS else None,
        'level_consensus': detection.consensus is not None,
        'cell_id': header.cell_id,
        'zone': ZONE_NAMES[cell.zone - 1] if cell else None,
        'security_code': header.stripped('dsi.security_code') or None,
        'security_control': header.stripped('dsi.security_control') or None,
        'security_handling': header.stripped('dsi.security_handling') or None,
        'vertical_datum': header.stripped('dsi.vertical_datum') or None,
        'horizontal_datum': header.stripped('dsi.horizontal_datum') or None,
        'abs_horiz_acc': acc('acc.abs_horiz_acc'),
        'abs_vert_acc': acc('acc.abs_vert_acc'),
        'rel_horiz_acc': acc('acc.rel_horiz_acc'),
        'rel_vert_acc': acc('acc.rel_vert_acc'),
        'data_edition': header.stripped('dsi.data_edition') or None,
        'match_merge_version': header.stripped('dsi.match_merge_version') or None,
        'compilation_date': yymm_to_iso(header['dsi.compilation_date']),
        'maintenance_date': yymm_to_iso(header['dsi.maintenance_date']),
        'producer_code': header.stripped('dsi.producer_code') or None,
        'digitizing_system': header.stripped('dsi.digitizing_system') or None,
        'partial_cell': header.stripped('dsi.partial_cell') or None,
        'subregions': header.outline_count(),
        'errors': count(issues, 'error'),
        'warnings': count(issues, 'warning'),
    }


def build_report(
    path: str,
    header: DtedHeader,
    issues: list[Issue],
    *,
    zero_based: bool = False,
    extension: str | None = None,
) -> dict:
    """Everything the renderers need, as plain data (also the JSON document)."""
    detection = header.detect_level(extension if extension is not None else os.path.splitext(path)[1])
    rows = header_rows(header, zero_based=zero_based)
    return {
        'file': path,
        'size': os.path.getsize(path) if os.path.isfile(path) else None,
        'positions': 'zero-based' if zero_based else 'one-based',
        'level': {
            'detected': detection.level,
            'consensus': detection.consensus is not None,
            'votes': detection.votes,
        },
        'summary': summarize(header, detection, issues),
        'records': {record: [asdict(row) for row in record_rows] for record, record_rows in rows.items()},
        'issues': [issue.to_dict() for issue in issues],
        '_rows': rows,
    }


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False) or ['']


def _text_table(rows: list[Row], widths: tuple[int, int, int]) -> list[str]:
    """Aligned columns; the title, value and description columns wrap onto
    continuation lines that leave the position columns blank."""
    title_w, value_w, desc_w = widths
    head = f'{"Start":>5} {"End":>5} {"Len":>4}  {"Title":<{title_w}}  {"Value":<{value_w}}  Description'
    lines = [head, '-' * len(head)]
    for row in rows:
        titles = _wrap(row.title, title_w)
        values = _wrap(row.value, value_w)
        descriptions = _wrap(row.description, desc_w)
        height = max(len(titles), len(values), len(descriptions))
        for i in range(height):
            position = f'{row.start:>5} {row.end:>5} {row.length:>4}' if i == 0 else ' ' * 16
            title = titles[i] if i < len(titles) else ''
            value = values[i] if i < len(values) else ''
            description = descriptions[i] if i < len(descriptions) else ''
            lines.append(f'{position}  {title:<{title_w}}  {value:<{value_w}}  {description}'.rstrip())
    return lines


def render_text(report: dict) -> str:
    summary = report['summary']
    lines = []
    name = os.path.basename(report['file'])
    size = f'{report["size"]:,} bytes' if report['size'] is not None else 'size unknown'
    level = report['level']
    series = summary['series'] or 'unknown level'
    votes = ', '.join(f'{k} {v}' for k, v in level['votes'].items() if v is not None) or 'no evidence'
    agreement = 'agree' if level['consensus'] else 'DISAGREE'
    lines.append(f'{name}: {series} ({size}); level by {votes}: {agreement}')
    lines.append(
        f'Cell {summary["cell_id"] or "?"}, zone {summary["zone"] or "?"}; security {summary["security_code"] or "?"}; '
        f'vertical datum {summary["vertical_datum"] or "blank"}; '
        f'horizontal datum {summary["horizontal_datum"] or "blank"}'
    )
    lines.append(f'Byte positions are {report["positions"]}.')
    for record, rows in report['_rows'].items():
        lines.append('')
        offset = RECORD_OFFSETS[record]
        length = schema.RECORD_LENGTHS[record]
        lines.append(f'{record}  {RECORD_TITLES[record]} ({length} bytes, file offset {offset})')
        lines.extend(_text_table(rows, (44, 28, 60)))
    lines.append('')
    lines.append('Summary')
    lines.append('-------')

    def meters(value):
        return 'NA' if value is None else (f'{value} m' if isinstance(value, int) else repr(value))

    lines.append(f'Absolute horizontal accuracy (CE90): {meters(summary["abs_horiz_acc"])}; '
                 f'absolute vertical accuracy (LE90): {meters(summary["abs_vert_acc"])}')
    lines.append(f'Point-to-point horizontal accuracy (CE90): {meters(summary["rel_horiz_acc"])}; '
                 f'point-to-point vertical accuracy (LE90): {meters(summary["rel_vert_acc"])}')
    lines.append(f'Edition {summary["data_edition"] or "?"} version {summary["match_merge_version"] or "?"}; '
                 f'compiled {summary["compilation_date"] or "unknown"}; '
                 f'maintained {summary["maintenance_date"] or "never"}; '
                 f'producer {summary["producer_code"] or "blank"}; system {summary["digitizing_system"] or "blank"}')
    lines.append(f'Partial cell indicator {summary["partial_cell"] or "?"}; '
                 f'{summary["subregions"]} accuracy subregion(s)')
    lines.append('')
    issues = report['issues']
    if issues:
        lines.append(f'Findings ({summary["errors"]} error(s), {summary["warnings"]} warning(s))')
        lines.append('--------')
        for issue in issues:
            lines.append(str(Issue(**issue)))
    else:
        lines.append('Findings: none; the header conforms to MIL-PRF-89020B.')
    return '\n'.join(lines) + '\n'


def _md_cell(text) -> str:
    return str(text).replace('|', '\\|').replace('\n', ' ')


def render_markdown(report: dict) -> str:
    summary = report['summary']
    lines = [f'# {os.path.basename(report["file"])}', '']
    headline = (f'{summary["series"] or "Unknown level"}, cell {summary["cell_id"] or "?"}, '
                f'zone {summary["zone"] or "?"}')
    if report['size'] is not None:
        headline += f', {report["size"]:,} bytes; byte positions are {report["positions"]}.'
    else:
        headline += '.'
    lines.append(headline)
    for record, rows in report['_rows'].items():
        lines += ['', f'## {record}: {RECORD_TITLES[record]} ({schema.RECORD_LENGTHS[record]} bytes)', '',
                  '| ' + ' | '.join(COLUMNS) + ' |', '|' + '---:|' * 3 + '---|' * 3]
        for row in rows:
            lines.append('| ' + ' | '.join(_md_cell(c) for c in row.columns()) + ' |')
    lines += ['', '## Summary', '']
    for key, value in summary.items():
        lines.append(f'- {key}: {value if value is not None else "NA"}')
    lines += ['', '## Findings', '']
    if report['issues']:
        for issue in report['issues']:
            lines.append(f'- {Issue(**issue)}')
    else:
        lines.append('- none; the header conforms to MIL-PRF-89020B.')
    return '\n'.join(lines) + '\n'


def render_csv(report: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['file', 'record', 'key'] + [c.lower() for c in COLUMNS] + ['raw'])
    for record, rows in report['_rows'].items():
        for row in rows:
            writer.writerow([report['file'], record, row.key, *row.columns(), row.raw])
    return buffer.getvalue()


def render_json(report: dict) -> str:
    document = {k: v for k, v in report.items() if not k.startswith('_')}
    return json.dumps(document, indent=2) + '\n'


def render_report(report: dict, fmt: str) -> str:
    """The report in one of the :data:`FORMATS`."""
    renderers = {'text': render_text, 'md': render_markdown, 'csv': render_csv, 'json': render_json}
    if fmt not in renderers:
        raise ValueError(f'Unknown report format {fmt!r}; choose one of {", ".join(FORMATS)}')
    return renderers[fmt](report)
