"""The dted-header and dted-index subcommands, and their routing from the
egmtrans command line."""

import json
import os
import sys

import numpy as np
import pytest

from egmtrans import cli, cli_dted
from egmtrans.dted.header import read_header, write_header
from tests.conftest import write_dted

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'samples')
SRTM = os.path.join(SAMPLES, '03n008e_SRTM.dt2')
PROFILE = os.path.join(SAMPLES, 'dted_profile_example.toml')
requires_srtm = pytest.mark.skipif(not os.path.isfile(SRTM), reason='SRTM sample not present')


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
    assert cli_dted.main(['dted-header', dt0]) == 1  # GDAL's default header has NUL bytes
    out, err = capsys.readouterr()
    assert 'UHL  User Header Label (80 bytes, file offset 0)' in out
    assert 'DTED0' in out and 'N06E126' in out
    assert 'ERROR' in out and 'non-printable' in out
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


def test_routing_from_the_main_command(dt0, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['egmtrans', 'dted-header', dt0, '--format', 'json'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert json.loads(capsys.readouterr().out)['summary']['cell_id'] == 'N06E126'


def test_transform_flags_check_the_files(dt0, tmp_dir, monkeypatch, capsys):
    out = os.path.join(tmp_dir, 'out.dt0')
    monkeypatch.setattr(sys, 'argv', ['egmtrans', '-i', dt0, '-o', out, '-s', 'EGM96', '-t', 'EGM2008',
                                      '--dted-index', os.path.join(tmp_dir, 'missing.gpkg')])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert '--dted-index file does not exist' in capsys.readouterr().err
    bad = os.path.join(tmp_dir, 'bad.toml')
    with open(bad, 'w') as handle:
        handle.write('[product]\nsecurity_code = "X"\n')
    monkeypatch.setattr(sys, 'argv', ['egmtrans', '-i', dt0, '-o', out, '-s', 'EGM96', '-t', 'EGM2008',
                                      '--dted-profile', bad])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert 'security_code' in capsys.readouterr().err


@requires_srtm
def test_srtm_sample_is_clean(capsys):
    assert cli_dted.main(['dted-header', SRTM, '--format', 'json']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['summary']['errors'] == 0 and document['summary']['warnings'] == 0
    assert document['summary']['cell_id'] == 'N03E008' and document['summary']['partial_cell'] == '99'
