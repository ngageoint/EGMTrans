"""The report: six columns in every format, subregions expanded, positions in
the spec's one-based form or zero-based on request."""

import csv
import io
import json
import os

import pytest

from egmtrans.dted.header import AccSubregion, parse_header
from egmtrans.dted.report import COLUMNS, build_report, display_value, header_rows, render_report
from egmtrans.dted.schema import field
from egmtrans.dted.validate import validate_header

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')


@pytest.fixture
def srtm():
    with open(FIXTURE, 'rb') as handle:
        return parse_header(handle.read())


@pytest.fixture
def report(srtm):
    return build_report(FIXTURE, srtm, validate_header(srtm, extension='.bin'), extension='.dt2')


def test_rows_cover_every_field(srtm):
    rows = header_rows(srtm)
    assert [len(rows[r]) for r in ('UHL', 'DSI', 'ACC')] == [13, 42, 12]
    uhl = rows['UHL']
    assert (uhl[0].start, uhl[0].end, uhl[0].length) == (1, 3, 3)
    assert uhl[-1].end == 80
    assert rows['DSI'][-1].end == 648 and rows['ACC'][-1].end == 2700
    unused = [row for row in rows['ACC'] if row.key == 'acc.subregions_unused'][0]
    assert (unused.start, unused.end, unused.value) == (58, 2613, '(blank)')
    assert all(len(row.columns()) == len(COLUMNS) for record in rows.values() for row in record)
    zero = header_rows(srtm, zero_based=True)
    assert (zero['UHL'][0].start, zero['UHL'][0].end) == (0, 2)
    assert zero['DSI'][0].file_offset == 80


def test_values_and_decoding(srtm):
    rows = {row.key: row for record in header_rows(srtm).values() for row in record}
    assert rows['acc.rel_horiz_acc'].value == '"NA  "' and rows['acc.rel_horiz_acc'].decoded is None
    assert rows['acc.abs_horiz_acc'].value == '0012' and rows['acc.abs_horiz_acc'].decoded == 12
    assert rows['uhl.origin_lon'].decoded == 8.0 and '= 8.000000 deg' in rows['uhl.origin_lon'].description
    assert rows['dsi.compilation_date'].decoded == '2000-02'
    assert rows['dsi.reserved_1'].value == '(blank)'
    assert rows['uhl.security_code'].value == 'U' and 'Unclassified' in rows['uhl.security_code'].description
    assert rows['dsi.free_text'].value == 'Voids have not been filled or interpolated'
    assert display_value(field('uhl.reserved'), '\x00' + ' ' * 23) == '\\x00'
    assert display_value(field('acc.abs_horiz_acc'), 'NA\x00 ') == '"NA\\x00 "'


def test_subregion_rows(srtm):
    srtm.set_subregions([
        AccSubregion.from_values(12, 6, None, 8, [(3.0, 8.0), (4.0, 8.0), (4.0, 8.5), (3.0, 8.5)]),
        AccSubregion.from_values(20, 10, 'NA', 12, [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]),
    ])
    rows = header_rows(srtm)['ACC']
    titles = [row.title for row in rows]
    assert 'Subregion 1: absolute horizontal accuracy of subregion (CE90, m)' in titles
    assert 'Subregion 2, coordinate 4: longitude' in titles
    assert 'Subregions 3-9 (unused)' in titles
    first = [row for row in rows if row.key == 'acc.subregion_1.abs_horiz_acc'][0]
    assert (first.start, first.end, first.value) == (58, 61, '0012')
    unused = [row for row in rows if row.key == 'acc.subregion_1.unused'][0]
    assert (unused.start, unused.end) == (58 + 18 + 4 * 19, 58 + 283)
    assert rows[-1].end == 2700


def test_text_report(report):
    text = render_report(report, 'text')
    assert 'DTED2 (25,981,042 bytes)' in text or 'DTED2 (3,428 bytes)' in text
    assert 'Start   End  Len  Title' in text
    assert 'UHL  User Header Label (80 bytes, file offset 0)' in text
    assert 'Absolute horizontal accuracy (CE90): 12 m' in text
    assert 'Findings: none' in text
    assert text.count('Recognition sentinel') == 3


def test_markdown_report(report):
    text = render_report(report, 'md')
    assert text.startswith('# srtm_n03e008_header.bin')
    assert '| ' + ' | '.join(COLUMNS) + ' |' in text
    assert '| 1 | 3 | 3 | Recognition sentinel | UHL | Always "UHL". |' in text
    assert '## Findings' in text


def test_csv_report(report):
    text = render_report(report, 'csv')
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ['file', 'record', 'key'] + [c.lower() for c in COLUMNS] + ['raw']
    assert len(rows) - 1 == 13 + 42 + 12
    rel = [row for row in rows if row[2] == 'acc.rel_horiz_acc'][0]
    assert rel[-1] == 'NA  ' and rel[7] == '"NA  "'


def test_json_report(report):
    document = json.loads(render_report(report, 'json'))
    assert document['level'] == {'detected': 2, 'consensus': True,
                                 'votes': {'extension': 2, 'DSI series': 2, 'UHL latitude interval': 2,
                                           'UHL latitude points': 2}}
    assert document['summary']['cell_id'] == 'N03E008' and document['summary']['zone'] == 'I'
    assert document['summary']['rel_horiz_acc'] is None and document['summary']['abs_vert_acc'] == 6
    assert len(document['records']['UHL']) == 13
    assert document['records']['UHL'][2]['decoded'] == 8.0
    assert document['issues'] == []
    assert '_rows' not in document


def test_unknown_format(report):
    with pytest.raises(ValueError):
        render_report(report, 'xml')
