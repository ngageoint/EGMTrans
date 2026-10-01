"""The header assembler and the DTED-to-DTED header rewrite: precedence,
derived fields, required fields, subregions, and what changes in a file."""

import os

import numpy as np
import pytest

from egmtrans.dted.header import cell_geometry, encode_header, new_header, parse_header, read_header
from egmtrans.dted.index import new_row, write_index
from egmtrans.dted.profile import load_profile
from egmtrans.dted.validate import validate_header
from egmtrans.dted.writer import (
    DerivedFields,
    DtedMetadata,
    DtedMetadataSource,
    HeaderAssemblyError,
    assemble_header,
    describe_changes,
)
from egmtrans.io import update_dted_header
from tests.conftest import write_dted

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')
PROFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'samples', 'dted_profile_example.toml')


@pytest.fixture
def profile():
    return load_profile(PROFILE)


@pytest.fixture
def srtm():
    with open(FIXTURE, 'rb') as handle:
        return parse_header(handle.read())


def test_profile_fills_a_header_from_scratch(profile):
    header, sources = assemble_header(
        cell_geometry(-1, 50, 2), metadata=DtedMetadata(None, [], profile),
        derived=DerivedFields(vertical_datum='E96', partial_cell=0),
    )
    assert header['uhl.lon_interval'] == '0020' and header['uhl.lon_lines'] == '1801'
    assert header['dsi.security_code'] == 'U' and header['uhl.security_code'] == 'U  '
    assert header['dsi.security_handling'] == 'LIMITED DISTRIBUTION       '
    assert header['dsi.producer_code'] == 'USNGA   ' and header['dsi.digitizing_system'] == 'TandemXTDF'
    assert header['acc.abs_horiz_acc'] == '0014' and header['acc.rel_horiz_acc'] == 'NA  '
    assert header['acc.abs_vert_acc'] == '0010' == header['uhl.abs_vert_acc']
    assert header['dsi.compilation_date'] == '2407' and header['dsi.product_spec_date'] == '0005'
    assert header['dsi.vertical_datum'] == 'E96' and header['dsi.partial_cell'] == '00'
    assert sources['dsi.producer_code'] == 'profile' and sources['dsi.vertical_datum'] == 'derived'
    assert sources['uhl.abs_vert_acc'] == 'derived'
    assert [issue for issue in validate_header(header) if issue.severity == 'error'] == []


def test_index_row_overrides_profile_and_null_means_na(profile):
    row = new_row('N50W001', producer_code='GEBGIC', abs_horiz_acc=None, compilation_date='2025-01-10',
                  unique_ref_dsi='N50W001_01')
    header, sources = assemble_header(
        cell_geometry(-1, 50, 2), metadata=DtedMetadata(row, [], profile), derived=DerivedFields(vertical_datum='E96'),
    )
    assert header['dsi.producer_code'] == 'GEBGIC  ' and sources['dsi.producer_code'] == 'index'
    assert header['acc.abs_horiz_acc'] == 'NA  ' and sources['acc.abs_horiz_acc'] == 'index'
    assert header['dsi.compilation_date'] == '2501'
    assert header['dsi.unique_ref'] == 'N50W001_01     '
    assert header['dsi.digitizing_system'] == 'TandemXTDF' and sources['dsi.digitizing_system'] == 'profile'


def test_required_fields_and_conflicts(profile, log_lines):
    with pytest.raises(HeaderAssemblyError, match='security_code is required'):
        assemble_header(cell_geometry(8, 3, 2), derived=DerivedFields(vertical_datum='E96'))
    with pytest.raises(HeaderAssemblyError, match='vertical datum is unknown'):
        assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile))
    # A datum the profile or index states for another product is reported; the output keeps its own.
    profile.product['vertical_datum'] = 'E08'
    header, _ = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile),
                                derived=DerivedFields(vertical_datum='E96'))
    assert header['dsi.vertical_datum'] == 'E96'
    assert any("vertical_datum is 'E08' in the profile but the output is 'E96'" in line for line in log_lines)


def test_subregions_from_the_index_set_both_flags(profile):
    subregions = [
        {'cell_id': 'N03E008', 'seq': 1, 'abs_horiz_acc': 12, 'abs_vert_acc': 6, 'rel_horiz_acc': None,
         'rel_vert_acc': 8, 'outline': [(3.0, 8.0), (4.0, 8.0), (4.0, 8.5), (3.0, 8.5)]},
        {'cell_id': 'N03E008', 'seq': 2, 'abs_horiz_acc': 20, 'abs_vert_acc': 10, 'rel_horiz_acc': None,
         'rel_vert_acc': 12, 'outline': [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]},
    ]
    header, sources = assemble_header(
        cell_geometry(8, 3, 2), metadata=DtedMetadata(new_row('N03E008'), subregions, profile),
        derived=DerivedFields(vertical_datum='E96', partial_cell=99),
    )
    assert header['acc.outline_flag'] == '02' and header['uhl.multiple_accuracy'] == '1'
    assert sources['acc.subregions'] == 'index'
    decoded = parse_header(encode_header(header)).subregions()
    assert decoded[1].accuracies()['abs_horiz_acc'] == 20
    assert decoded[0].decoded_outline()[2] == (pytest.approx(4.0), pytest.approx(8.5))
    assert header['dsi.partial_cell'] == '99'


def test_base_header_is_cleaned_and_kept(srtm):
    srtm.set_raw('acc.rel_horiz_acc', '  NA')
    srtm.set_raw('acc.rel_vert_acc', 'x   ')
    srtm.set_raw('dsi.security_control', '\x00 ')
    header, sources = assemble_header(cell_geometry(8, 3, 2), base=srtm, derived=DerivedFields(vertical_datum='E08'),
                                      cli_abs_horiz_accuracy=99)
    assert header['acc.rel_horiz_acc'] == 'NA  ' and 'left justified' in sources['acc.rel_horiz_acc']
    assert header['acc.rel_vert_acc'] == 'NA  ' and 'set to NA' in sources['acc.rel_vert_acc']
    assert header['dsi.security_control'] == '  ' and 'blanked' in sources['dsi.security_control']
    assert header['dsi.vertical_datum'] == 'E08'
    # The command line accuracy is a fallback: 0012 is there, so it stays.
    assert header['acc.abs_horiz_acc'] == '0012'
    assert header['dsi.producer_code'] == 'USCNIMA ' and sources['dsi.producer_code'] == 'base'
    assert header['dsi.free_text'].strip() == 'Voids have not been filled or interpolated'
    srtm.set_raw('acc.abs_horiz_acc', 'NA  ')
    header, sources = assemble_header(cell_geometry(8, 3, 2), base=srtm, derived=DerivedFields(vertical_datum='E96'),
                                      cli_abs_horiz_accuracy=99)
    assert header['acc.abs_horiz_acc'] == '0099' and sources['acc.abs_horiz_acc'] == 'command line'
    with pytest.raises(HeaderAssemblyError):
        assemble_header(cell_geometry(8, 3, 2), base=srtm, derived=DerivedFields(vertical_datum='E96'),
                        cli_abs_horiz_accuracy=10000)
    changes = describe_changes(srtm, header, sources)
    assert any('acc.abs_horiz_acc' in line and 'command line' in line for line in changes)


def test_base_gaps_are_carried_not_refused():
    header = new_header(cell_geometry(8, 3, 2))  # blank security code, edition, version
    header.set('dsi.vertical_datum', 'MSL')
    assembled, _ = assemble_header(cell_geometry(8, 3, 2), base=header, derived=DerivedFields(vertical_datum='E96'))
    assert assembled['dsi.data_edition'] == '  ' and assembled['dsi.vertical_datum'] == 'E96'


def test_update_dted_header_in_a_file(tmp_dir, profile, log_lines):
    path = write_dted(os.path.join(tmp_dir, 'n06e126.dt0'), np.full((121, 121), 5, dtype=np.int16), 126, 6)
    before = read_header(path)
    assert before['dsi.vertical_datum'] == 'MSL' and '\x00' in before['acc.abs_horiz_acc']

    update_dted_header(path, 'EGM96', 25)
    after = read_header(path)
    assert after['dsi.vertical_datum'] == 'E96'
    assert after['acc.abs_horiz_acc'] == '0025'  # GDAL's 'NA\x00 ' was NA, so the fallback applies
    assert after['acc.abs_vert_acc'] == 'NA  ' == after['uhl.abs_vert_acc']
    assert '\x00' not in encode_header(after).decode('latin-1')
    assert after['dsi.data_edition'] == '01' and after['dsi.match_merge_version'] == 'A'
    assert any('dsi.vertical_datum' in line and "'MSL' -> 'E96'" in line for line in log_lines)
    with open(path, 'rb') as handle:
        assert handle.read()[3428] == 0xAA

    index_path = os.path.join(tmp_dir, 'index.gpkg')
    write_index(index_path, {'N06E126': new_row('N06E126', data_edition=3, abs_vert_acc=7, rel_vert_acc=None)}, level=0)
    source = DtedMetadataSource.load(index_path, PROFILE)
    update_dted_header(path, 'EGM2008', metadata=source.for_cell('N06E126'))
    final = read_header(path)
    assert final['dsi.vertical_datum'] == 'E08'
    assert final['dsi.data_edition'] == '03' and final['acc.abs_vert_acc'] == '0007' == final['uhl.abs_vert_acc']
    # An index row's NULL accuracy means NA, whatever the profile says: the
    # profile's accuracies serve runs without an index.
    assert final['acc.rel_vert_acc'] == 'NA  ' and final['acc.abs_horiz_acc'] == 'NA  '
    assert final['dsi.producer_code'] == 'USNGA   ' and final['dsi.security_handling'].strip() == 'LIMITED DISTRIBUTION'
    assert final['dsi.series'] == 'DTED0' and final['uhl.lat_points'] == '0121'
    with pytest.raises(LookupError):
        source.for_cell('N07E126')
    with pytest.raises(ValueError, match='Unsupported target datum'):
        update_dted_header(path, 'WGS84')
