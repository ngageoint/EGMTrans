"""The metadata index: round trips through GeoPackage and GeoParquet, the
schema checks, the profile, and harvesting from headers, rasters and footprints."""

import datetime as dt
import os
import textwrap

import numpy as np
import pytest

from egmtrans.dted.harvest import (
    build_index,
    cells_covered,
    merge_rows,
    row_from_header,
    rows_from_dted,
    rows_from_footprints,
    rows_from_rasters,
)
from egmtrans.dted.header import parse_header
from egmtrans.dted.index import (
    COLUMNS_BY_NAME,
    DtedIndex,
    cell_polygon,
    check_value,
    new_row,
    parse_polygon_wkb,
    polygon_wkb,
    read_index,
    validate_index,
    write_index,
)
from egmtrans.dted.profile import load_profile
from tests.conftest import point_geotransform, write_dted, write_geotiff

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')
PROFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'samples', 'dted_profile_example.toml')


def _parquet_possible():
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        from osgeo import ogr

        return ogr.GetDriverByName('Parquet') is not None


FORMATS = ['gpkg'] + (['parquet'] if _parquet_possible() else [])


def sample_rows():
    return {
        'N03E008': new_row('N03E008', dted_level=2, security_code='U', data_edition=99, match_merge_version='B',
                           producer_code='USCNIMA', compilation_date=dt.date(2000, 2, 1), abs_horiz_acc=12,
                           abs_vert_acc=6, rel_horiz_acc=None, rel_vert_acc=8, dsi_free_text='Voids remain',
                           maintenance_date='2009-06', source_id='srtm'),
        'S06E030': new_row('S06E030', dted_level=2, security_code='U', data_edition=1, match_merge_version='A',
                           producer_code='USNGA', compilation_date='2024-07-15', abs_horiz_acc=14, abs_vert_acc=10,
                           rel_horiz_acc='NA', rel_vert_acc=10, unique_ref_dsi='S06E030_02', extra='kept'),
    }


def sample_subregions():
    return [
        {'cell_id': 'N03E008', 'seq': 1, 'abs_horiz_acc': 12, 'abs_vert_acc': 6, 'rel_horiz_acc': None,
         'rel_vert_acc': 8, 'outline': [(3.0, 8.0), (4.0, 8.0), (4.0, 8.5), (3.0, 8.5)]},
        {'cell_id': 'N03E008', 'seq': 2, 'abs_horiz_acc': 20, 'abs_vert_acc': 10, 'rel_horiz_acc': 'NA',
         'rel_vert_acc': 12, 'outline': [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]},
    ]


@pytest.mark.parametrize('extension', FORMATS)
def test_round_trip(tmp_dir, extension):
    path = os.path.join(tmp_dir, f'index.{extension}')
    written = write_index(path, sample_rows(), sample_subregions(), {'product': 'test'}, level=2)
    index = read_index(path)
    assert len(index) == 2 and index.level == 2 and index.meta['product'] == 'test'
    assert index.meta['generator'].startswith('EGMTrans')
    for cell, row in written.rows.items():
        for column, value in row.items():
            assert index.rows[cell].get(column) == value, (cell, column)
    row = index.get('s06e030')
    assert row['compilation_date'] == dt.date(2024, 7, 15) and row['rel_horiz_acc'] is None
    assert row['extra'] == 'kept'
    assert index.get('N03E008')['maintenance_date'] == dt.date(2009, 6, 1)
    subregions = index.subregions_of('N03E008')
    assert [s['seq'] for s in subregions] == [1, 2]
    assert subregions[0]['rel_horiz_acc'] is None and subregions[1]['abs_horiz_acc'] == 20
    assert subregions[1]['outline'] == [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]
    assert index.subregions_of('S06E030') == []
    assert {issue.severity for issue in validate_index(index, level=2)} <= {'info'}


def test_write_refuses_bad_values(tmp_dir):
    path = os.path.join(tmp_dir, 'index.gpkg')
    with pytest.raises(ValueError, match='security_code'):
        write_index(path, {'N03E008': new_row('N03E008', security_code='X')})
    with pytest.raises(ValueError, match='longer than'):
        write_index(path, {'N03E008': new_row('N03E008', producer_code='TOOLONGPRODUCER')})
    with pytest.raises(ValueError, match='not a date'):
        write_index(path, {'N03E008': new_row('N03E008', compilation_date='July 2024')})
    with pytest.raises(ValueError, match='Not a cell identifier'):
        write_index(path, [{'cell_id': 'TDF_N03E008'}])
    with pytest.raises(ValueError, match='no row'):
        write_index(path, {'N03E008': new_row('N03E008')}, sample_subregions()[:1] + [
            dict(sample_subregions()[0], cell_id='N04E008', seq=1)])
    with pytest.raises(ValueError):
        write_index(os.path.join(tmp_dir, 'index.csv'), sample_rows())


def test_check_value():
    assert check_value(COLUMNS_BY_NAME['abs_horiz_acc'], 'NA') is None
    assert check_value(COLUMNS_BY_NAME['abs_horiz_acc'], 10000)
    assert check_value(COLUMNS_BY_NAME['data_edition'], 0)
    assert check_value(COLUMNS_BY_NAME['match_merge_version'], 'AA')
    assert check_value(COLUMNS_BY_NAME['vertical_datum'], 'EGM')
    assert check_value(COLUMNS_BY_NAME['dsi_free_text'], 'café')
    assert check_value(COLUMNS_BY_NAME['maintenance_code'], 'A123') is None


def test_validate_index_reports_problems():
    index = DtedIndex('x.gpkg', rows={
        'N03E008': new_row('N03E008', dted_level=1, data_edition=0, abs_vert_acc=None),
    }, subregions={
        'N03E008': [{'cell_id': 'N03E008', 'seq': 1, 'outline': [(3.0, 8.0), (5.0, 8.0), (5.0, 9.0)],
                     'abs_horiz_acc': 1, 'abs_vert_acc': 1, 'rel_horiz_acc': 1, 'rel_vert_acc': 1}],
        'N04E008': [{'cell_id': 'N04E008', 'seq': 2, 'outline': [(4.0, 8.0), (5.0, 8.0)]}],
    }, meta={'dted_level': '2'})
    messages = [str(issue) for issue in validate_index(index, level=2)]
    assert any('data_edition' in m and 'outside 1-99' in m for m in messages)
    assert any('dted_level' in m and 'requested level 2' in m for m in messages)
    assert any('one subregion' in m for m in messages)
    assert any('leaves the cell' in m for m in messages)
    assert any('no row' in m for m in messages)
    assert any('2 vertices' in m for m in messages)
    assert any('NULL, so the header will say NA' in m for m in messages)


def test_polygon_helpers():
    ring = cell_polygon('S06E030')
    assert ring == [(30, -6), (31, -6), (31, -5), (30, -5), (30, -6)]
    assert parse_polygon_wkb(polygon_wkb(ring)) == ring[:-1]


def test_profile_example_and_errors(tmp_dir):
    profile = load_profile(PROFILE)
    assert profile.level == 2 and profile.get('producer_code') == 'USNGA'
    assert profile.get('rel_horiz_acc') == 'NA' and profile.get('abs_horiz_acc') == 14
    assert profile.harvest.tag_fields['source_version']['pattern'].startswith('DEMES')
    assert profile.harvest.xml_fields['source_id']['xpath'].startswith('//gmd:')

    def write(text):
        path = os.path.join(tmp_dir, 'profile.toml')
        with open(path, 'w') as handle:
            handle.write(textwrap.dedent(text))
        return path

    with pytest.raises(ValueError, match='not an index column'):
        load_profile(write('[product]\nproducer = "USNGA"\n'))
    with pytest.raises(ValueError, match='not a header field'):
        load_profile(write('[product]\nsource_id = "x"\n'))
    with pytest.raises(ValueError, match='not one of U, R, C, S'):
        load_profile(write('[product]\nsecurity_code = "X"\n'))
    with pytest.raises(ValueError, match='not valid TOML'):
        load_profile(write('[product\n'))
    with pytest.raises(ValueError, match='not an index column'):
        load_profile(write('[harvest.tags.fields]\nnope = "TIFFTAG_DATETIME"\n'))
    with pytest.raises(ValueError, match='unknown keys'):
        load_profile(write('[harvest.xml.fields]\nsource_id = { xpath = "//a", regex = "x" }\n'))
    loaded = load_profile(write('[product]\ncompilation_date = 2024-07-15\n'))
    assert loaded.get('compilation_date') == dt.date(2024, 7, 15)


def test_row_from_srtm_header():
    with open(FIXTURE, 'rb') as handle:
        header = parse_header(handle.read())
    row, subregions = row_from_header(header, '/x/n03e008.dt2')
    assert row['cell_id'] == 'N03E008' and row['dted_level'] == 2
    assert row['producer_code'] == 'USCNIMA' and row['digitizing_system'] == 'SRTM'
    assert row['data_edition'] == 99 and row['match_merge_version'] == 'B'
    assert row['compilation_date'] == dt.date(2000, 2, 1) and row['match_merge_date'] == dt.date(2009, 6, 1)
    assert row['maintenance_date'] is None
    assert (row['abs_horiz_acc'], row['abs_vert_acc'], row['rel_horiz_acc'], row['rel_vert_acc']) == (12, 6, None, 8)
    assert row['partial_cell'] == 99 and row['acc_nima_reserved'] == 'X'
    assert row['dsi_free_text'] == 'Voids have not been filled or interpolated'
    assert subregions == []


def test_harvest_from_dted_files_and_merge(tmp_dir):
    folder = os.path.join(tmp_dir, 'dted')
    os.makedirs(folder)
    write_dted(os.path.join(folder, 'n06e126.dt0'), np.zeros((121, 121), dtype=np.int16), 126, 6)
    write_dted(os.path.join(folder, 'n06e127.dt0'), np.zeros((121, 121), dtype=np.int16), 127, 6)
    rows, subregions = rows_from_dted([folder])
    assert sorted(rows) == ['N06E126', 'N06E127'] and subregions == {}
    assert rows['N06E126']['dted_level'] == 0 and rows['N06E126']['vertical_datum'] == 'MSL'
    # GDAL's NUL bytes harvest as blanks, not as text.
    assert rows['N06E126']['security_control'] is None
    merged = merge_rows({'N06E126': new_row('N06E126', notes='keep', data_edition=5)}, rows)
    assert merged['N06E126']['notes'] == 'keep' and merged['N06E126']['data_edition'] == 1
    assert 'N06E127' in merged


def test_cells_covered():
    assert cells_covered(point_geotransform(8, 3, 3601), 3601, 3601) == ['N03E008']
    # A TDF tile above 60 degrees: 0.8 arc second columns, 0.4 arc second rows, posts on the degree lines.
    tdf = (7 - 0.4 / 3600, 0.8 / 3600, 0, 64 + 0.2 / 3600, 0, -0.4 / 3600)
    assert cells_covered(tdf, 4501, 9001) == ['N63E007']
    assert cells_covered((7.0, 1 / 3600, 0, 64.0, 0, -1 / 3600), 3600, 3600) == ['N63E007']  # area registered
    assert cells_covered((7.0, 1 / 1800, 0, 64.0, 0, -1 / 3600), 3600, 3600) == ['N63E007', 'N63E008']
    assert cells_covered((-1.0, 1 / 3600, 0, 51.0, 0, -1 / 3600), 1800, 3600) == []
    two = cells_covered((10 - 0.5 / 3600, 1 / 3600, 0, 65 + 0.5 / 3600, 0, -1 / 3600), 7201, 3601)
    assert two == ['N64E010', 'N64E011']


def test_harvest_from_rasters_with_tags_and_xml(tmp_dir):
    raster = os.path.join(tmp_dir, 'TDF_N06E126_01_DEM.tif')
    write_geotiff(raster, np.zeros((121, 121), dtype=np.float32), point_geotransform(126, 6, 121), nodata=-32767)
    from osgeo import gdal

    with gdal.Open(raster, gdal.GA_Update) as dataset:
        dataset.SetMetadataItem('TIFFTAG_SOFTWARE', 'DEMES 5.13.0.4690A - optimized')
        dataset.SetMetadataItem('TIFFTAG_DATETIME', '2011:02:10 17:52:23')
    with open(os.path.join(tmp_dir, 'TDF_N06E126_01_DEM.xml'), 'w') as handle:
        handle.write(textwrap.dedent('''\
            <?xml version="1.0" encoding="UTF-8"?>
            <gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gco="http://www.isotc211.org/2005/gco">
              <gmd:fileIdentifier><gco:CharacterString>TDF_N06E126_01.xml</gco:CharacterString></gmd:fileIdentifier>
              <gmd:identificationInfo><gmd:MD_DataIdentification><gmd:citation><gmd:CI_Citation>
                <gmd:date><gmd:CI_Date><gmd:date><gco:Date>2019-07-18</gco:Date></gmd:date></gmd:CI_Date></gmd:date>
              </gmd:CI_Citation></gmd:citation></gmd:MD_DataIdentification></gmd:identificationInfo>
            </gmd:MD_Metadata>
        '''))
    profile = load_profile(PROFILE)
    rows = rows_from_rasters([raster], profile)
    row = rows['N06E126']
    assert row['dted_level'] == 2
    assert row['source_version'] == '5.13.0.4690A'
    assert row['source_date'] == dt.date(2011, 2, 10)
    assert row['source_id'] == 'TDF_N06E126_01.xml'
    assert row['compilation_date'] == dt.date(2019, 7, 18)
    assert row['source_metadata_file'].endswith('TDF_N06E126_01_DEM.xml')

    with open(os.path.join(tmp_dir, 'TDF_N06E126_01_DEM.xml'), 'w') as handle:
        handle.write('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
    with pytest.raises(ValueError, match='DOCTYPE'):
        rows_from_rasters([raster], profile)


def test_harvest_from_footprints_and_build(tmp_dir):
    from osgeo import ogr, osr

    footprints = os.path.join(tmp_dir, 'footprints.gpkg')
    driver = ogr.GetDriverByName('GPKG')
    source = driver.CreateDataSource(footprints)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layer = source.CreateLayer('tiles', srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('item_name', ogr.OFTString))
    for name, (lon0, lat0) in (('TDF_N06E126_01', (126, 6)), ('TDF_S06E030_02', (30, -6))):
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField('item_name', name)
        ring = [(lon0, lat0), (lon0 + 1, lat0), (lon0 + 1, lat0 + 1), (lon0, lat0 + 1), (lon0, lat0)]
        feature.SetGeometry(ogr.CreateGeometryFromWkt(
            'POLYGON((' + ', '.join(f'{x} {y}' for x, y in ring) + '))'))
        layer.CreateFeature(feature)
        feature = None
    source = None

    assert sorted(rows_from_footprints(footprints)) == ['N06E126', 'S06E030']
    by_field = rows_from_footprints(footprints, cell_field='item_name')
    assert by_field['S06E030']['source_id'] == 'TDF_S06E030_02'

    out = os.path.join(tmp_dir, 'built.gpkg')
    index = build_index(out, from_footprints=footprints, cell_field='item_name', profile=load_profile(PROFILE),
                        product='TDF-DTED2')
    assert len(index) == 2 and index.level == 2 and index.meta['product'] == 'TDF-DTED2'
    assert index.meta['profile'] == 'dted_profile_example.toml'

    folder = os.path.join(tmp_dir, 'dted')
    os.makedirs(folder)
    write_dted(os.path.join(folder, 'n06e126.dt0'), np.zeros((121, 121), dtype=np.int16), 126, 6)
    updated = build_index(out, from_dted=[folder], update=True, level=0)
    assert len(updated) == 2
    assert updated.get('N06E126')['producer_code'] is None
    assert updated.get('N06E126')['source_id'] == 'TDF_N06E126_01'
    assert updated.get('N06E126')['source_file'].endswith('n06e126.dt0')
    assert read_index(out).level == 0
