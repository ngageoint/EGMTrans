"""The field tables of docs/dted_index.md are generated from the code, so the page
cannot drift: every header field is in exactly one table, and the page holds what
``egmtrans dted-index columns`` prints."""

import os

from egmtrans import cli_dted
from egmtrans.dted.index import FILLED_BY, INDEX_COLUMNS, column_table_rows, derived_table_rows, render_column_tables
from egmtrans.dted.schema import ALL_FIELDS

DOCS = os.path.join(os.path.dirname(__file__), '..', 'docs', 'dted_index.md')
START, END = '<!-- index-columns:start -->', '<!-- index-columns:end -->'


def test_every_header_field_is_in_exactly_one_table():
    indexed = {column.dted_key for column in INDEX_COLUMNS if column.dted_key}
    assert set(FILLED_BY) == {item.key for item in ALL_FIELDS} - indexed
    assert len(column_table_rows()) == len(INDEX_COLUMNS) == 37
    assert len(derived_table_rows()) == len(ALL_FIELDS) - len(indexed) == 41
    rows = {row['Column']: row for row in column_table_rows()}
    assert rows['`security_code`']['Characters'] == '4' and rows['`security_code`']['Record'] == 'DSI'
    assert rows['`dsi_free_text`']['Characters'] == '493-648' and rows['`abs_vert_acc`']['Required'] == 'required'
    assert rows['`vertical_datum`']['Status'] == 'check only' and rows['`source_id`']['Status'] == 'catalog'


def test_the_docs_tables_match_the_code():
    with open(DOCS, encoding='utf-8') as handle:
        text = handle.read()
    assert START in text and END in text
    held = text.split(START, 1)[1].split(END, 1)[0].strip()
    assert held == render_column_tables('markdown').strip(), (
        'docs/dted_index.md is out of step with the code: paste the output of '
        '"egmtrans dted-index columns --format markdown" between the markers'
    )


def test_the_columns_command_prints_the_tables(capsys):
    assert cli_dted.main(['dted-index', 'columns']) == 0
    out = capsys.readouterr().out
    assert '| 1 | `cell_id` |' in out and 'Recognition sentinel' in out and out.count('| UHL |') == 13
    assert cli_dted.main(['dted-index', 'columns', '--format', 'csv']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == '#,Column,Header field,Record,Characters,Status,Required,Index note'
    assert lines[1].startswith('1,cell_id,,,,key,required,') and len(lines) == 38
