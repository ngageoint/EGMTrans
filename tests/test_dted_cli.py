"""The dted-header and dted-index subcommands, and their routing from the
egmtrans command line."""

import json
import os
import sys

import numpy as np
import pytest

from egmtrans import cli, cli_dted
from egmtrans.dted.header import read_header, write_header
from egmtrans.dted.index import read_index
from tests.conftest import write_dted

PROFILE = os.path.join(os.path.dirname(__file__), 'data', 'dted_profile.toml')


@pytest.fixture
def dt0(tmp_dir):
    return write_dted(os.path.join(tmp_dir, 'n06e126.dt0'), np.full((121, 121), 3, dtype=np.int16), 126, 6)


def clean(path):
    """Make a GDAL-written header conform: blanks for its NUL bytes, a compilation date."""
    header = read_header(path)
    for key, raw in header.values.items():
        header.set_raw(key, raw.replace('\x00', ' '))
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.producer_code', 'USTEST')
    write_header(path, header)
    return header


def test_header_text_report(dt0, capsys):
    # GDAL's default header has a blank compilation date (an error) and NUL bytes (warnings).
    assert cli_dted.main(['dted-header', dt0]) == 1
    out, err = capsys.readouterr()
    assert 'UHL  User Header Label (80 bytes, file offset 0)' in out
    assert 'DTED0' in out and 'N06E126' in out
    assert 'ERROR' in out and 'not a YYMM date' in out and 'WARNING' in out and 'read as blanks' in out
    assert '1 error' in err or 'error(s)' in err


def test_header_formats_and_out_file(dt0, tmp_dir, capsys):
    clean(dt0)

    assert cli_dted.main(['dted-header', dt0, '--format', 'json', '--check-data']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['summary']['series'] == 'DTED0' and document['summary']['errors'] == 0
    assert any(issue['key'] == 'elevations' for issue in document['issues'])

    out_path = os.path.join(tmp_dir, 'report.csv')
    assert cli_dted.main(['dted-header', dt0, dt0, '--format', 'csv', '--out', out_path]) == 0
    with open(out_path) as handle:
        lines = handle.read().splitlines()
    assert lines[0].startswith('file,record,key,start,end,length,title,value,description')
    assert len(lines) == 1 + 2 * (13 + 42 + 12)

    assert cli_dted.main(['dted-header', dt0, '--format', 'md', '--zero-based']) == 0
    text = capsys.readouterr().out
    assert '| 0 | 2 | 3 | Recognition sentinel | UHL |' in text


def test_header_strict_and_missing_file(dt0, tmp_dir, capsys):
    header = clean(dt0)
    header.set_raw('acc.rel_horiz_acc', '  NA')
    write_header(dt0, header)
    assert cli_dted.main(['dted-header', dt0]) == 0
    assert cli_dted.main(['dted-header', dt0, '--strict']) == 1
    assert cli_dted.main(['dted-header', os.path.join(tmp_dir, 'missing.dt0')]) == 2
    assert 'Not a file' in capsys.readouterr().err


def test_index_build_and_validate(dt0, tmp_dir, capsys):
    out = os.path.join(tmp_dir, 'index.gpkg')
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-dted', tmp_dir, '--profile', PROFILE,
                          '--level', '0', '--product', 'test']) == 0
    assert '1 cell(s)' in capsys.readouterr().err
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '0']) == 0
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '2']) == 1
    # The example profile is for level 2, so it does not go with a level 0 index.
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '0', '--profile', PROFILE]) == 1
    assert cli_dted.main(['dted-index', 'validate', os.path.join(tmp_dir, 'none.gpkg')]) == 2
    assert cli_dted.main(['dted-index', 'build', '--out', out]) == 2


def test_index_validate_takes_the_overrides_of_the_run(tmp_dir, capsys):
    """An index and a profile that leave the compilation date to the run (--dted-set
    compilation_date=today) check clean when validate is given the same override."""
    from egmtrans.dted.index import new_row, write_index

    out = os.path.join(tmp_dir, 'index.gpkg')
    write_index(out, [new_row('N06E126', security_code='U', abs_vert_acc=5)], level=2)
    with open(PROFILE, encoding='utf-8') as handle:
        product = [line for line in handle.read().split('[harvest')[0].splitlines()
                   if not line.startswith('compilation_date')]
    profile = os.path.join(tmp_dir, 'no_date.toml')
    with open(profile, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(product) + '\n')
    validate = ['dted-index', 'validate', out, '--level', '2', '--profile', profile]

    assert cli_dted.main(validate) == 1
    assert 'Neither the index, the profile nor --dted-set supplies: compilation_date' in capsys.readouterr().err
    assert cli_dted.main(validate + ['--dted-set', 'compilation_date=today']) == 0
    err = capsys.readouterr().err
    assert 'compilation_date' not in err and '0 error(s)' in err
    # The value is checked as in a run, and a producer code as in a run too.
    assert cli_dted.main(validate + ['--dted-set', 'compilation_date=1975-06']) == 2
    assert '--dted-set: compilation_date: a DTED YYMM date must fall in 1980-2079' in capsys.readouterr().err
    assert cli_dted.main(validate + ['--dted-set', 'compilation_date=today', '--dted-set', 'producer_code=AUTEST']) == 0
    assert 'WARNING OVERRIDE.producer_code: AU is Austria in FIPS 10-4' in capsys.readouterr().err


def test_routing_from_the_main_command(dt0, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['egmtrans', 'dted-header', dt0, '--format', 'json'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert json.loads(capsys.readouterr().out)['summary']['cell_id'] == 'N06E126'


def test_transform_flags_check_the_files(dt0, tmp_dir, monkeypatch, capsys):
    out = os.path.join(tmp_dir, 'out.dt0')
    monkeypatch.setattr(sys, 'argv', ['egmtrans', '-i', dt0, '-o', out, '-s', 'EGM2008', '-t', 'EGM96',
                                      '--dted-index', os.path.join(tmp_dir, 'missing.gpkg')])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert '--dted-index file does not exist' in capsys.readouterr().err
    bad = os.path.join(tmp_dir, 'bad.toml')
    with open(bad, 'w') as handle:
        handle.write('[product]\nsecurity_code = "X"\n')
    monkeypatch.setattr(sys, 'argv', ['egmtrans', '-i', dt0, '-o', out, '-s', 'EGM2008', '-t', 'EGM96',
                                      '--dted-profile', bad])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert 'security_code' in capsys.readouterr().err


def test_a_cell_the_codec_writes_is_clean_and_so_are_its_companions(tmp_dir, capsys):
    from egmtrans.dted.header import CellGeometry, new_header
    from egmtrans.dted.records import write_dted_file

    cell = CellGeometry(0, 8, 3)
    header = new_header(cell)
    header.set('dsi.security_code', 'U')
    header.set_raw('uhl.security_code', 'U  ')
    header.set('dsi.data_edition', 1)
    header.set('dsi.match_merge_version', 'A')
    header.set('dsi.producer_code', 'USNGA')
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.vertical_datum', 'E96')
    for key in ('acc.abs_horiz_acc', 'acc.abs_vert_acc', 'acc.rel_horiz_acc', 'acc.rel_vert_acc', 'uhl.abs_vert_acc'):
        header.set(key, 5)
    values = np.full((cell.lat_points, cell.lon_lines), 40, dtype=np.int32)
    values[:10, :10] = -32767
    header.set('dsi.partial_cell', 99)
    paths = [os.path.join(tmp_dir, 'N03' + extension) for extension in ('.dt0', '.avg', '.min', '.max')]
    for path in paths:
        write_dted_file(path, header, values)
    assert cli_dted.main(['dted-header', *paths, '--format', 'json', '--check-data']) == 0
    documents = json.loads(capsys.readouterr().out)
    for document in documents:
        assert document['summary']['errors'] == 0 and document['summary']['warnings'] == 0, document['summary']
        assert document['summary']['cell_id'] == 'N03E008' and document['summary']['partial_cell'] == '99'
        assert document['summary']['series'] == 'DTED0'


def test_index_build_from_a_table(tmp_dir, capsys):
    from tests.test_dted_index import _table_gpkg

    table = _table_gpkg(os.path.join(tmp_dir, 'catalog.gpkg'), [
        {'Cell_ID': 'N06E126', 'ABS_VERT_ACC': 3.0, 'data_edition': 2, 'made': '2024/07/15 10:00:00', 'ref': 'A',
         'version': 1},
        {'Cell_ID': 'N06E126', 'ABS_VERT_ACC': 6.0, 'data_edition': 3, 'made': '2025/01/10 00:00:00', 'ref': 'B',
         'version': 2},
        {'Cell_ID': 'S06E030', 'ABS_VERT_ACC': 12.4, 'data_edition': 1, 'made': '2024/01/01 00:00:00', 'ref': 'C',
         'version': 1},
    ])
    out = os.path.join(tmp_dir, 'index.gpkg')
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-table', table, '--map', 'compilation_date=made',
                          '--set', 'security_code=U', '--prefer', 'version', '--profile', PROFILE, '--level', '2']) == 0
    err = capsys.readouterr().err
    assert 'Table catalog.gpkg (layer tiles): 3 row(s) read, 2 cell(s), the cell from column Cell_ID' in err
    assert '1 cell(s) had several rows; the preferred row was kept' in err
    assert '  abs_vert_acc <- ABS_VERT_ACC' in err and '  compilation_date <- made' in err
    assert "  security_code = 'U' (constant)" in err
    assert 'dropped (no index column of that name): ref, version' in err
    assert ('Required header fields the profile supplies: match_merge_version, producer_code, abs_horiz_acc, '
            'rel_horiz_acc, rel_vert_acc') in err
    index = read_index(out)
    assert 'abs_horiz_acc' not in index.columns and index.get('N06E126')['abs_vert_acc'] == 6
    assert index.get('S06E030')['abs_vert_acc'] == 13 and index.get('S06E030')['security_code'] == 'U'

    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-table', table, '--prefer', 'version']) == 0
    assert 'nothing supplies yet' in capsys.readouterr().err
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-table', table]) == 1
    assert 'share a cell' in capsys.readouterr().err
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-dted', tmp_dir, '--map', 'x=y']) == 2
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-table', table, '--set', 'cell_id=X']) == 2


# -- 1.10.1: the complete index, one rule for supplied, the report's warnings ---

def _cells_csv(path, rows):
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write('cell_id,abs_vert_acc\n')
        for cell, accuracy in rows:
            handle.write(f'{cell},{"" if accuracy is None else accuracy}\n')
    return path


def test_build_all_columns_fills_the_profile_constants(tmp_dir, capsys):
    import datetime as dt

    from egmtrans.dted.index import INDEX_COLUMNS

    cells = _cells_csv(os.path.join(tmp_dir, 'cells.csv'), [('N06E126', 5), ('N06E127', None)])
    out = os.path.join(tmp_dir, 'complete.gpkg')
    assert cli_dted.main(['dted-index', 'build', '--out', out, '--from-table', cells, '--all-columns',
                          '--profile', PROFILE, '--level', '2']) == 0
    err = capsys.readouterr().err
    assert 'filled from the profile: security_code (2 cell(s)), security_handling (2 cell(s))' in err
    index = read_index(out)
    assert index.columns == {column.name for column in INDEX_COLUMNS}
    row = index.get('N06E127')
    assert row['producer_code'] == 'USCNIMA' and row['digitizing_system'] == 'SRTM' and row['abs_horiz_acc'] == 12
    assert row['compilation_date'] == dt.date(2000, 2, 1) and row['rel_horiz_acc'] is None, '"NA" arrives as NULL'
    assert row['abs_vert_acc'] is None and index.get('N06E126')['abs_vert_acc'] == 5, 'the table\'s values are kept'
    assert row['vertical_datum'] is None, 'the test profile has no datum check'
    assert row['dted_level'] == 2 and row['maintenance_date'] is None and row['source_id'] is None

    # A header from the complete index alone equals the one from the sparse index plus the profile.
    from egmtrans.dted.header import cell_geometry
    from egmtrans.dted.writer import DerivedFields, DtedMetadataSource, assemble_header

    sparse = os.path.join(tmp_dir, 'sparse.gpkg')
    assert cli_dted.main(['dted-index', 'build', '--out', sparse, '--from-table', cells, '--level', '2']) == 0
    complete_source = DtedMetadataSource.load(out, None)
    sparse_source = DtedMetadataSource.load(sparse, PROFILE)
    for cell_id, lon in (('N06E126', 126), ('N06E127', 127)):
        derived = DerivedFields(vertical_datum='E96', partial_cell=0)
        cell = cell_geometry(lon, 6, 2)
        alone, _ = assemble_header(cell, metadata=complete_source.for_cell(cell_id), derived=derived)
        paired, _ = assemble_header(cell, metadata=sparse_source.for_cell(cell_id), derived=derived)
        assert alone.values == paired.values, cell_id

    # Without a profile the constants stay NULL; without --all-columns the sparse layout is kept.
    bare = os.path.join(tmp_dir, 'bare.gpkg')
    assert cli_dted.main(['dted-index', 'build', '--out', bare, '--from-table', cells, '--all-columns',
                          '--level', '2']) == 0
    assert read_index(bare).get('N06E127')['producer_code'] is None
    assert 'filled from the profile' not in capsys.readouterr().err
    assert 'producer_code' not in read_index(sparse).columns


def test_validate_counts_null_rows_as_the_run_does(tmp_dir, capsys):
    """A required column that is NULL in some rows and that the profile does not supply
    stops validate as it stops the run at those cells; a blank profile value is no source."""
    from egmtrans.dted.index import new_row, write_index

    out = os.path.join(tmp_dir, 'index.gpkg')
    write_index(out, [
        new_row('N06E126', security_code='U', abs_vert_acc=5, compilation_date='2024-07'),
        new_row('N06E127', security_code='U', abs_vert_acc=5, compilation_date=None),
    ], level=2)
    with open(PROFILE, encoding='utf-8') as handle:
        product = [line for line in handle.read().split('[harvest')[0].splitlines()
                   if not line.startswith('compilation_date')]
    profile = os.path.join(tmp_dir, 'no_date.toml')
    with open(profile, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(product) + '\n')
    validate = ['dted-index', 'validate', out, '--level', '2', '--profile', profile]
    assert cli_dted.main(validate) == 1
    err = capsys.readouterr().err
    assert 'Neither the index, the profile nor --dted-set supplies: compilation_date (NULL in 1 of 2 cells)' in err
    assert cli_dted.main(validate + ['--dted-set', 'compilation_date=today']) == 0

    blank = os.path.join(tmp_dir, 'blank_producer.toml')
    with open(blank, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(line if not line.startswith('producer_code') else 'producer_code = "  "'
                               for line in product) + '\n')
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '2', '--profile', blank,
                          '--dted-set', 'compilation_date=today']) == 1
    assert 'supplies: producer_code' in capsys.readouterr().err


def test_null_accuracy_lines_come_once_per_column(tmp_dir, capsys):
    from egmtrans.dted.index import new_row, write_index

    out = os.path.join(tmp_dir, 'index.gpkg')
    # The complete table keeps the four accuracy columns although every value is NULL.
    write_index(out, [new_row(f'N0{i}E126', security_code='U') for i in range(7)], level=2, all_columns=True)
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '2']) == 0
    err = capsys.readouterr().err
    assert err.count('the header will say NA') == 4, 'one line per accuracy column, not 28 lines'
    assert 'INFO INDEX.abs_vert_acc: NULL in 7 cell(s) (N00E126, N01E126, N02E126, N03E126, N04E126 and 2 more)' in err
    # With a profile that gives an accuracy a number, that column's line is a warning.
    assert cli_dted.main(['dted-index', 'validate', out, '--level', '2', '--profile', PROFILE]) == 0
    err = capsys.readouterr().err
    assert err.count('the header will say NA') == 4
    assert 'WARNING INDEX.abs_horiz_acc: NULL in 7 cell(s)' in err and "not the profile's 12 m" in err
    assert 'WARNING INDEX.abs_vert_acc: NULL in 7 cell(s)' in err and "not the profile's 6 m" in err
    assert 'INFO INDEX.rel_horiz_acc: NULL in 7 cell(s)' in err, 'the profile says NA here too'


def test_header_report_reads_non_printable_bytes_as_blanks(dt0, capsys):
    """A GDAL-written header has NUL bytes where the specification blank-fills: a warning
    with a plain explanation, not an error, once its real problems are fixed."""
    header = read_header(dt0)
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.producer_code', 'USTEST')
    write_header(dt0, header)
    assert cli_dted.main(['dted-header', dt0]) == 0
    out, err = capsys.readouterr()
    assert 'WARNING' in out and 'read as blanks' in out and 'the elevations are not affected' in out
    assert 'ERROR' not in out and '0 error(s)' in err
    assert cli_dted.main(['dted-header', dt0, '--strict']) == 1
