"""The producer nation code: the FIPS 10-4 table, the specification's own
example codes, and the ISO codes that are other countries in FIPS 10-4."""

import pytest

from egmtrans.dted.fips import COLLISIONS, FIPS_10_4, SPEC_EXAMPLE_CODES, nation_name, producer_code_warning


def test_the_table_is_well_formed():
    assert len(FIPS_10_4) > 250
    for code, name in FIPS_10_4.items():
        assert len(code) == 2 and code.isalpha() and code.isupper() and name.strip(), code
    assert FIPS_10_4['GM'] == 'Germany' and FIPS_10_4['GG'] == 'Georgia' and FIPS_10_4['UK'] == 'United Kingdom'
    assert FIPS_10_4['AS'] == 'Australia' and FIPS_10_4['SW'] == 'Sweden' and FIPS_10_4['SZ'] == 'Switzerland'
    assert 'GE' not in FIPS_10_4 and 'DE' not in FIPS_10_4, 'GE and DE are not FIPS 10-4 codes'
    assert set(SPEC_EXAMPLE_CODES) == {'BE', 'FR', 'GE', 'IT', 'NL', 'NO', 'SP', 'UK', 'US'}
    assert set(COLLISIONS) == {'AU', 'GB', 'SE', 'CH', 'DE'}


@pytest.mark.parametrize(
    'code', ['US', 'USNGA', 'UK', 'GM', 'FR', 'NL', 'NO', 'SP', 'BE', 'IT', 'GE', 'SW', 'SZ', 'AS']
)
def test_known_codes_are_silent(code):
    assert producer_code_warning(code.ljust(8)) is None


def test_collisions_warn_loudly_with_both_readings():
    assert 'Gabon' in producer_code_warning('GBDGC   ') and 'United Kingdom is UK' in producer_code_warning('GB')
    assert 'Austria' in producer_code_warning('AU') and 'Australia is AS' in producer_code_warning('AU')
    assert 'Seychelles' in producer_code_warning('SE') and 'Sweden is SW' in producer_code_warning('SE')
    assert 'China' in producer_code_warning('CH') and 'Switzerland is SZ' in producer_code_warning('CH')
    germany = producer_code_warning('DE')
    assert 'not a FIPS 10-4 country code' in germany and 'Germany is GM' in germany
    assert 'GE' in producer_code_warning('DE'), 'the specification example is mentioned'


def test_unknown_codes_warn_and_shapes_are_left_to_the_validator():
    assert 'XX is not a FIPS 10-4 country code' in producer_code_warning('XXNGA')
    assert producer_code_warning('') is None and producer_code_warning('1A') is None
    assert nation_name('USNGA') == 'United States' and nation_name('GE') == 'United Germany'
    assert nation_name('XX') is None


def test_the_header_validator_the_index_and_the_profile_carry_the_warning(tmp_dir):
    from egmtrans.dted.header import CellGeometry, new_header
    from egmtrans.dted.index import new_row, read_index, validate_index, write_index
    from egmtrans.dted.profile import HarvestConfig, Profile
    from egmtrans.dted.validate import validate_header
    from egmtrans.dted.writer import DtedMetadataSource

    header = new_header(CellGeometry(2, 6, 49))
    header.set('dsi.producer_code', 'GBNGA')
    assert any('Gabon' in issue.message for issue in validate_header(header))
    header.set('dsi.producer_code', 'UKOS')
    assert not any('FIPS' in issue.message for issue in validate_header(header) if issue.severity == 'warning')

    import os

    path = os.path.join(tmp_dir, 'index.gpkg')
    write_index(path, {
        'N49E006': new_row('N49E006', security_code='U', data_edition=1, match_merge_version='A',
                           compilation_date='2024-07', producer_code='AU', abs_horiz_acc=3, abs_vert_acc=3,
                           rel_horiz_acc=3, rel_vert_acc=3),
    }, level=2)
    issues = validate_index(read_index(path))
    assert any(issue.severity == 'warning' and 'Austria' in issue.message and 'N49E006' in issue.message
               for issue in issues)

    profile = Profile(path='<test>', product={'dted_level': 2, 'producer_code': 'SE'}, harvest=HarvestConfig())
    issues = DtedMetadataSource(None, profile).validate(2)
    assert any(issue.record == 'PROFILE' and 'Seychelles' in issue.message for issue in issues)
    source = DtedMetadataSource(None, None, {'producer_code': 'CHSWISS'})
    assert any(issue.record == 'OVERRIDE' and 'China' in issue.message for issue in source.validate(2))
