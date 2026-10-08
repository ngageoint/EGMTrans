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
PROFILE = os.path.join(os.path.dirname(__file__), 'data', 'dted_profile.toml')


def level_0_profile(folder: str) -> str:
    """A copy of the sample profile for DTED0, which the test tiles are."""
    with open(PROFILE) as handle:
        text = handle.read().replace('dted_level = 2', 'dted_level = 0')
    path = os.path.join(folder, 'profile_level0.toml')
    with open(path, 'w') as handle:
        handle.write(text)
    return path


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
    assert header['dsi.security_handling'] == 'PUBLIC SALE/NO RESTRICTION '
    assert header['dsi.producer_code'] == 'USCNIMA ' and header['dsi.digitizing_system'] == 'SRTM      '
    assert header['acc.abs_horiz_acc'] == '0012' and header['acc.rel_horiz_acc'] == 'NA  '
    assert header['acc.abs_vert_acc'] == '0006' == header['uhl.abs_vert_acc']
    assert header['dsi.compilation_date'] == '0002' and header['dsi.product_spec_date'] == '0005'
    assert header['dsi.vertical_datum'] == 'E96' and header['dsi.partial_cell'] == '00'
    assert sources['dsi.producer_code'] == 'profile' and sources['dsi.vertical_datum'] == 'derived'
    assert sources['uhl.abs_vert_acc'] == 'derived'
    assert [issue for issue in validate_header(header) if issue.severity == 'error'] == []


def test_index_row_overrides_profile_and_null_means_na(profile):
    row = new_row('N50W001', producer_code='USTEST', abs_horiz_acc=None, compilation_date='2025-01-10',
                  unique_ref_dsi='N50W001_01')
    header, sources = assemble_header(
        cell_geometry(-1, 50, 2), metadata=DtedMetadata(row, [], profile), derived=DerivedFields(vertical_datum='E96'),
    )
    assert header['dsi.producer_code'] == 'USTEST  ' and sources['dsi.producer_code'] == 'index'
    assert header['acc.abs_horiz_acc'] == 'NA  ' and sources['acc.abs_horiz_acc'] == 'index'
    assert header['dsi.compilation_date'] == '2501'
    assert header['dsi.unique_ref'] == 'N50W001_01     '
    assert header['dsi.digitizing_system'] == 'SRTM      ' and sources['dsi.digitizing_system'] == 'profile'


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


REQUIRED = ('security_code', 'data_edition', 'match_merge_version', 'producer_code', 'compilation_date',
            'abs_horiz_acc', 'abs_vert_acc', 'rel_horiz_acc', 'rel_vert_acc')


@pytest.mark.parametrize('column', REQUIRED)
def test_every_required_field_needs_a_source_from_scratch(profile, column):
    """The spec fill (NA, 0000) is not a value for a required field: without a
    source the write stops, naming the field."""
    del profile.product[column]
    with pytest.raises(HeaderAssemblyError, match=f'{column} is required and nothing supplies it'):
        assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile),
                        derived=DerivedFields(vertical_datum='E96'))
    # An explicit NA from the index counts, and so does the command line for the horizontal accuracy.
    if column in ('abs_horiz_acc', 'abs_vert_acc', 'rel_horiz_acc', 'rel_vert_acc'):
        row = new_row('N03E008', **{column: None})
        header, sources = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(row, [], profile),
                                          derived=DerivedFields(vertical_datum='E96'))
        assert header[f'acc.{column}'] == 'NA  ' and sources[f'acc.{column}'] == 'index'
    if column == 'abs_horiz_acc':
        header, sources = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile),
                                          derived=DerivedFields(vertical_datum='E96'), cli_abs_horiz_accuracy=30)
        assert header['acc.abs_horiz_acc'] == '0030' and sources['acc.abs_horiz_acc'] == 'command line'
    # With a base header the input is the authority, gaps included.
    base = new_header(cell_geometry(8, 3, 2))
    base.set('dsi.vertical_datum', 'MSL')
    assemble_header(cell_geometry(8, 3, 2), base=base, derived=DerivedFields(vertical_datum='E96'))


def test_another_level_in_the_row_or_the_profile_is_refused(profile):
    row = new_row('N03E008', dted_level=1)
    with pytest.raises(HeaderAssemblyError, match='index row is for DTED level 1; cell N03E008 .* at level 2'):
        assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(row, [], profile),
                        derived=DerivedFields(vertical_datum='E96'))
    with pytest.raises(HeaderAssemblyError, match='profile is for DTED level 2; cell N03E008 .* at level 0'):
        assemble_header(cell_geometry(8, 3, 0), metadata=DtedMetadata(None, [], profile),
                        derived=DerivedFields(vertical_datum='E96'))
    # A row that does not say, and a profile that does not say, pass.
    assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(new_row('N03E008'), [], profile),
                    derived=DerivedFields(vertical_datum='E96'))
    del profile.product['dted_level']
    assemble_header(cell_geometry(8, 3, 0), metadata=DtedMetadata(None, [], profile),
                    derived=DerivedFields(vertical_datum='E96'))


def test_bad_values_are_reported_with_their_column(profile):
    profile.product['data_edition'] = 'x'
    with pytest.raises(HeaderAssemblyError, match=r"data_edition \(profile\): 'x' cannot be written"):
        assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile),
                        derived=DerivedFields(vertical_datum='E96'))


def test_validator_warnings_on_a_new_header_are_logged(profile, log_lines):
    profile.product['producer_code'] = '12NGA'  # no FIPS country code
    profile.product['product_spec'] = 'PRF89020A'
    header, _ = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(None, [], profile),
                                derived=DerivedFields(vertical_datum='E08'))
    warnings = [issue for issue in validate_header(header) if issue.severity == 'warning']
    assert warnings, 'the test profile makes no warning'
    for issue in warnings:
        assert any(line == f'DTED header of cell N03E008: {issue}' for line in log_lines)


def test_subregion_rings_are_normalized(profile):
    from egmtrans.dted.writer import normalize_ring

    clockwise = [(3.0, 8.0), (4.0, 8.0), (4.0, 8.5), (3.0, 8.5)]
    assert normalize_ring(clockwise) == clockwise
    assert normalize_ring(clockwise + [clockwise[0]]) == clockwise, 'the closing vertex is dropped'
    assert normalize_ring(list(reversed(clockwise))) == clockwise, 'a counterclockwise ring is reversed'
    assert normalize_ring(clockwise[2:] + clockwise[:2]) == clockwise, 'the ring starts at the southwest'
    assert normalize_ring([(3.0, 8.5), (3.0, 8.0), (4.0, 8.0)]) == [(3.0, 8.0), (4.0, 8.0), (3.0, 8.5)]
    subregions = [
        {'cell_id': 'N03E008', 'seq': 1, 'abs_horiz_acc': 12, 'abs_vert_acc': 6, 'rel_horiz_acc': None,
         'rel_vert_acc': 8, 'outline': [(4.0, 8.5), (4.0, 8.0), (3.0, 8.0), (3.0, 8.5), (4.0, 8.5)]},
        {'cell_id': 'N03E008', 'seq': 2, 'abs_horiz_acc': 20, 'abs_vert_acc': 10, 'rel_horiz_acc': None,
         'rel_vert_acc': 12, 'outline': [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]},
    ]
    header, _ = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(new_row('N03E008'), subregions, profile),
                                derived=DerivedFields(vertical_datum='E96'))
    outline = parse_header(encode_header(header)).subregions()[0].decoded_outline()
    assert [(pytest.approx(lat), pytest.approx(lon)) for lat, lon in clockwise] == outline


def test_describe_a_header_built_from_scratch(profile):
    row = new_row('N03E008', producer_code='USTEST', abs_horiz_acc=None)
    header, sources = assemble_header(cell_geometry(8, 3, 2), metadata=DtedMetadata(row, [], profile),
                                      derived=DerivedFields(vertical_datum='E96'), cli_abs_horiz_accuracy=5)
    lines = describe_changes(None, header, sources)
    assert lines[0].startswith('Header fields by source: ')
    assert "    dsi.producer_code: 'USTEST  ' (index)" in lines
    assert "    acc.abs_horiz_acc: 'NA  ' (index)" in lines
    assert "    dsi.digitizing_system: 'SRTM      ' (profile)" in lines
    assert not any('(derived)' in line or '(default)' in line for line in lines[1:])


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
    # The sample profile is for level 2; a profile for another level is refused.
    source = DtedMetadataSource.load(index_path, PROFILE)
    with pytest.raises(HeaderAssemblyError, match='profile is for DTED level 2'):
        update_dted_header(path, 'EGM96', metadata=source.for_cell('N06E126'))
    source = DtedMetadataSource.load(index_path, level_0_profile(tmp_dir))
    update_dted_header(path, 'EGM96', metadata=source.for_cell('N06E126'))
    final = read_header(path)
    assert final['dsi.vertical_datum'] == 'E96'
    assert final['dsi.data_edition'] == '03' and final['acc.abs_vert_acc'] == '0007' == final['uhl.abs_vert_acc']
    # Accuracy columns that are NULL in every row are not in the index, so the
    # profile supplies them; a NULL among values means NA (see
    # test_index_row_overrides_profile_and_null_means_na).
    assert final['acc.rel_vert_acc'] == '0008' and final['acc.abs_horiz_acc'] == '0012'
    assert final['dsi.producer_code'] == 'USCNIMA '
    assert final['dsi.security_handling'].strip() == 'PUBLIC SALE/NO RESTRICTION'
    assert final['dsi.series'] == 'DTED0' and final['uhl.lat_points'] == '0121'
    with pytest.raises(LookupError):
        source.for_cell('N07E126')
    for target in ('WGS84', 'EGM2008'):
        with pytest.raises(ValueError, match='EGM96 only'):
            update_dted_header(path, target)


def test_parse_overrides():
    import datetime as dt

    from egmtrans.dted.writer import parse_overrides

    got = parse_overrides(
        ['producer_code=USNGA', 'compilation_date=today', 'abs_horiz_acc=NA', 'data_edition=3',
         ('dsi_free_text', 'from a pair')],
        today=dt.date(2026, 10, 5),
    )
    assert got == {'producer_code': 'USNGA', 'compilation_date': dt.date(2026, 10, 5), 'abs_horiz_acc': None,
                   'data_edition': 3, 'dsi_free_text': 'from a pair'}
    assert parse_overrides(['compilation_date=2026-10'])['compilation_date'] == dt.date(2026, 10, 1)
    assert parse_overrides([]) == {}
    for bad, reason in [
        ('dted_level=2', 'use --dted-level'), ('source_id=x', 'catalog column'), ('vertical_datum=E96', 'derived'),
        ('abs_vert_acc=12.5', 'not an integer'), ('security_code=X', 'not one of'), ('notes', 'FIELD=VALUE'),
        ('bogus=1', 'not an index column'), ('compilation_date=yesterday', 'not a date'),
        ('producer_code=TOOLONGPRODUCER', 'longer than'),
    ]:
        with pytest.raises(ValueError, match=reason):
            parse_overrides([bad])
    with pytest.raises(ValueError, match='given twice'):
        parse_overrides(['data_edition=1', 'data_edition=2'])


def test_overrides_beat_the_index_and_the_profile(profile):
    import datetime as dt

    from egmtrans.dted.writer import parse_overrides

    row = new_row('N50W001', producer_code='USTEST', abs_horiz_acc=None, compilation_date='2025-01-10')
    overrides = parse_overrides(
        ['producer_code=USNGA', 'abs_horiz_acc=9', 'compilation_date=today', 'rel_vert_acc=NA'],
        today=dt.date(2026, 10, 5),
    )
    source = DtedMetadataSource(None, profile, overrides)
    assert not source.empty and '4 override(s): producer_code=USNGA' in source.describe()
    assert DtedMetadataSource(None, None, overrides).empty is False and DtedMetadataSource().empty is True
    header, sources = assemble_header(
        cell_geometry(-1, 50, 2), metadata=DtedMetadata(row, [], profile, overrides),
        derived=DerivedFields(vertical_datum='E96'),
    )
    assert header['dsi.producer_code'] == 'USNGA   ' and sources['dsi.producer_code'] == 'override'
    assert header['acc.abs_horiz_acc'] == '0009' and sources['acc.abs_horiz_acc'] == 'override'
    assert header['dsi.compilation_date'] == '2610' and sources['dsi.compilation_date'] == 'override'
    assert header['acc.rel_vert_acc'] == 'NA  ' and sources['acc.rel_vert_acc'] == 'override'
    assert header['dsi.digitizing_system'] == 'SRTM      ' and sources['dsi.digitizing_system'] == 'profile'
    # Overrides alone do not complete a header: the other required fields still need a source.
    with pytest.raises(HeaderAssemblyError, match='security_code is required'):
        assemble_header(cell_geometry(-1, 50, 2), metadata=DtedMetadata(None, [], None, {'producer_code': 'USNGA'}),
                        derived=DerivedFields(vertical_datum='E96'))


def test_overrides_apply_to_a_dted_file(tmp_dir):
    from egmtrans.dted.writer import parse_overrides

    path = write_dted(os.path.join(tmp_dir, 'n06e126.dt0'), np.full((121, 121), 5, dtype=np.int16), 126, 6)
    source = DtedMetadataSource(None, None, parse_overrides(['producer_code=USNGA', 'dsi_free_text=rewritten']))
    update_dted_header(path, 'EGM96', metadata=source.for_cell('N06E126'))
    after = read_header(path)
    assert after['dsi.producer_code'] == 'USNGA   ' and after['dsi.free_text'].strip() == 'rewritten'
    assert after['dsi.vertical_datum'] == 'E96'


def test_coverage_and_plan_lines(tmp_dir, profile):
    from egmtrans.dted.writer import header_plan_lines, parse_overrides

    index_path = os.path.join(tmp_dir, 'index.gpkg')
    write_index(index_path, {
        'N03E008': new_row('N03E008', security_code='U', data_edition=2, abs_vert_acc=7, compilation_date='2024-07'),
        'N04E008': new_row('N04E008', security_code='U', data_edition=3, abs_vert_acc=None, compilation_date='2024-08'),
    }, level=2)
    source = DtedMetadataSource.load(index_path, PROFILE, parse_overrides(['producer_code=USNGA']))
    coverage = source.coverage(2)
    assert coverage.cells == 2 and coverage.level == 2
    assert coverage.from_index == ['security_code', 'data_edition', 'compilation_date']
    assert coverage.partly_from_index == {'abs_vert_acc': 1} and coverage.null_accuracy_cells == 1
    assert coverage.overrides == ['producer_code']
    assert coverage.from_profile == ['security_handling', 'match_merge_version', 'product_spec', 'product_spec_amend',
                                     'product_spec_date', 'digitizing_system', 'abs_horiz_acc', 'rel_horiz_acc',
                                     'rel_vert_acc', 'dsi_free_text']
    assert coverage.missing_required == {}
    summary = coverage.summary()
    assert summary.startswith(
        '2 cell(s); level 2; from the index: security_code, data_edition, compilation_date, abs_vert_acc (NULL in 1)')
    assert 'overridden: producer_code' in summary and summary.endswith('missing: none')
    # Without the profile, the required fields the index and the overrides do not cover are missing.
    bare = DtedMetadataSource(source.index, None, source.overrides).coverage(2, cli_abs_horiz_accuracy=5)
    assert bare.missing_required == {'match_merge_version': 2, 'rel_horiz_acc': 2, 'rel_vert_acc': 2}
    assert bare.summary().endswith('missing: match_merge_version, rel_horiz_acc, rel_vert_acc')

    header, sources = assemble_header(cell_geometry(8, 3, 2), metadata=source.for_cell('N03E008'),
                                      derived=DerivedFields(vertical_datum='E96'))
    lines = header_plan_lines(source.describe(), 'N03E008', 'in/tile.tif', 'out/N03E008.dt2', header, sources, more=1)
    assert lines[0] == 'DTED header plan'
    assert lines[1] == ('  metadata: index index.gpkg (2 cells), profile dted_profile.toml, '
                        '1 override(s): producer_code=USNGA')
    assert lines[2] == '  example: cell N03E008, tile.tif -> N03E008.dt2 (and 1 more)'
    assert lines[3].startswith('Header fields by source: ')
    assert "    dsi.producer_code: 'USNGA   ' (override)" in lines and "    acc.abs_vert_acc: '0007' (index)" in lines


def test_a_blank_required_value_is_not_a_source(tmp_dir):
    from egmtrans.dted.profile import HarvestConfig, Profile

    product = {
        'dted_level': 2, 'security_code': 'U', 'data_edition': 1, 'match_merge_version': 'A',
        'producer_code': '  ', 'compilation_date': '2026-01', 'abs_horiz_acc': 10, 'abs_vert_acc': 5,
        'rel_horiz_acc': 'NA', 'rel_vert_acc': 3, 'dsi_free_text': '',
    }
    profile = Profile(path='<test>', product=product, harvest=HarvestConfig())
    with pytest.raises(HeaderAssemblyError, match='producer_code is required and nothing supplies it'):
        assemble_header(cell_geometry(6, 49, 2), metadata=DtedMetadata(None, [], profile),
                        derived=DerivedFields(vertical_datum='E96', partial_cell=0))
    coverage = DtedMetadataSource(None, profile).coverage(2)
    assert 'producer_code' in coverage.missing_required and 'producer_code' not in coverage.from_profile
    assert 'dsi_free_text' in coverage.from_profile, 'an optional text field may be blanked on purpose'
    product['producer_code'] = 'USNGA'
    header, sources = assemble_header(cell_geometry(6, 49, 2), metadata=DtedMetadata(None, [], profile),
                                      derived=DerivedFields(vertical_datum='E96', partial_cell=0))
    assert header['dsi.free_text'].strip() == '' and sources['dsi.free_text'] == 'profile'
    # An index row with a blank producer code falls through to the profile.
    row = new_row('N49E006', producer_code='   ')
    header, sources = assemble_header(cell_geometry(6, 49, 2), metadata=DtedMetadata(row, [], profile),
                                      derived=DerivedFields(vertical_datum='E96', partial_cell=0))
    assert header['dsi.producer_code'] == 'USNGA   ' and sources['dsi.producer_code'] == 'profile'
