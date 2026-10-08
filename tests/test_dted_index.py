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
    polygon_wkt,
    read_index,
    validate_index,
    write_index,
)
from egmtrans.dted.profile import load_profile
from tests.conftest import point_geotransform, write_dted, write_geotiff

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'srtm_n03e008_header.bin')
PROFILE = os.path.join(os.path.dirname(__file__), 'data', 'dted_profile.toml')


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
    assert row['compilation_date'] == dt.date(2024, 7, 15)
    # NULL (or NA) in every row: the column is not written, so the profile supplies the field.
    assert 'rel_horiz_acc' not in row and 'rel_horiz_acc' not in index.columns
    assert row['extra'] == 'kept'
    assert index.get('N03E008')['maintenance_date'] == dt.date(2009, 6, 1)
    subregions = index.subregions_of('N03E008')
    assert [s['seq'] for s in subregions] == [1, 2]
    assert subregions[0]['rel_horiz_acc'] is None and subregions[1]['abs_horiz_acc'] == 20
    assert subregions[1]['outline'] == [(3.0, 8.5), (4.0, 8.5), (4.0, 9.0), (3.0, 9.0)]
    assert index.subregions_of('S06E030') == []
    issues = validate_index(index, level=2)
    assert {issue.severity for issue in issues} <= {'info', 'warning'}
    assert [issue.key for issue in issues if issue.severity == 'warning'] == ['rel_horiz_acc']


def test_write_refuses_bad_values(tmp_dir):
    path = os.path.join(tmp_dir, 'index.gpkg')
    with pytest.raises(ValueError, match='security_code'):
        write_index(path, {'N03E008': new_row('N03E008', security_code='X')})
    with pytest.raises(ValueError, match='longer than'):
        write_index(path, {'N03E008': new_row('N03E008', producer_code='TOOLONGPRODUCER')})
    with pytest.raises(ValueError, match='not a date'):
        write_index(path, {'N03E008': new_row('N03E008', compilation_date='July 2024')})
    with pytest.raises(ValueError, match='Not a cell identifier'):
        write_index(path, [{'cell_id': 'X_N03E008'}])
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


def _plain_parquet(path, columns, metadata=None):
    """A GeoParquet file another tool wrote: the columns given, cell polygons, the geo metadata."""
    pa = pytest.importorskip('pyarrow')
    import json

    import pyarrow.parquet as pq

    cells = columns['cell_id']
    arrays = {name: pa.array(values) for name, values in columns.items()}
    arrays['geometry'] = pa.array([polygon_wkb(cell_polygon(cell)) for cell in cells], pa.binary())
    geo = {'version': '1.1.0', 'primary_column': 'geometry', 'columns': {'geometry': {'encoding': 'WKB'}}}
    pq.write_table(pa.table(arrays).replace_schema_metadata({'geo': json.dumps(geo), **(metadata or {})}), path)
    return path


@pytest.mark.parametrize('reader', ['pyarrow', 'gdal'])
def test_a_file_that_is_not_an_index_is_refused_with_what_it_holds(tmp_dir, monkeypatch, reader):
    from osgeo import ogr

    if reader == 'gdal' and ogr.GetDriverByName('Parquet') is None:
        pytest.skip('this GDAL has no Parquet driver')
    layers = _table_gpkg(os.path.join(tmp_dir, 'catalog.gpkg'), [{'Cell_ID': 'N06E126', 'version': 1}])
    # A catalog: one row per tile version, none of its columns a header field.
    catalog = _plain_parquet(os.path.join(tmp_dir, 'catalog.parquet'),
                             {'tile_id': ['T_N06E126_01'], 'cell_id': ['N06E126'], 'tile_version': [1]})
    table = _plain_parquet(os.path.join(tmp_dir, 'table.parquet'), {'cell_id': ['N06E126'], 'security_code': ['U']})
    footprints = os.path.join(tmp_dir, 'footprints.parquet')
    write_index(footprints, {'N06E126': new_row('N06E126', source_id='N06E126_DEM')}, level=2)
    if reader == 'gdal':
        monkeypatch.setattr('egmtrans.dted.index._parquet_available', lambda: False)

    with pytest.raises(ValueError, match=r'^catalog\.gpkg is not a DTED metadata index: it has no dted_cells layer '
                                         r'\(its layers: tiles\)\. "egmtrans dted-index build --from-table"'):
        read_index(layers)
    with pytest.raises(ValueError, match=r'^catalog\.parquet is not a DTED metadata index: it has neither the '
                                         r'index metadata nor a header column '
                                         r'\(its columns: tile_id, cell_id, tile_version\)'):
        read_index(catalog)
    # A table with header columns but no index metadata is read as before.
    assert read_index(table).get('N06E126')['security_code'] == 'U'
    # An index without header columns (built from a footprint layer) has the index metadata.
    index = read_index(footprints)
    assert len(index) == 1 and index.level == 2 and index.meta['generator'].startswith('EGMTrans')


@pytest.mark.parametrize('extension', FORMATS)
def test_several_rows_for_one_cell_are_an_error(tmp_dir, extension):
    path = os.path.join(tmp_dir, f'index.{extension}')
    write_index(path, sample_rows(), level=2)
    if extension == 'gpkg':
        from osgeo import ogr

        source = ogr.Open(path, 1)
        layer = source.GetLayerByName('dted_cells')
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField('cell_id', 'N03E008')
        feature.SetField('data_edition', 7)
        feature.SetGeometry(ogr.CreateGeometryFromWkt(polygon_wkt(cell_polygon('N03E008'))))
        layer.CreateFeature(feature)
        feature = source = None
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        pq.write_table(pa.concat_tables([table, table.slice(0, 1)]), path)
    index = read_index(path)
    assert index.duplicates == {'N03E008': 2} and len(index) == 2
    errors = [issue for issue in validate_index(index, level=2) if issue.severity == 'error']
    assert [(issue.key, issue.message) for issue in errors] == [
        ('cell_id', '1 cell(s) have several rows: N03E008 (2 rows); an index holds one row per cell')]
    with pytest.raises(ValueError, match=r'several rows for 1 cell\(s\) \(N03E008\)'):
        build_index(path, from_dted=[], update=True)


def test_profile_example_and_errors(tmp_dir):
    profile = load_profile(PROFILE)
    assert profile.level == 2 and profile.get('producer_code') == 'USCNIMA'
    assert profile.get('rel_horiz_acc') == 'NA' and profile.get('abs_horiz_acc') == 12
    assert profile.harvest.tag_fields['source_version']['pattern'] == r'([0-9]+(?:\.[0-9A-Za-z]+)+)'
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
    # A tile above 60 degrees: 0.8 arc second columns, 0.4 arc second rows, posts on the degree lines.
    tile = (7 - 0.4 / 3600, 0.8 / 3600, 0, 64 + 0.2 / 3600, 0, -0.4 / 3600)
    assert cells_covered(tile, 4501, 9001) == ['N63E007']
    assert cells_covered((7.0, 1 / 3600, 0, 64.0, 0, -1 / 3600), 3600, 3600) == ['N63E007']  # area registered
    assert cells_covered((7.0, 1 / 1800, 0, 64.0, 0, -1 / 3600), 3600, 3600) == ['N63E007', 'N63E008']
    assert cells_covered((-1.0, 1 / 3600, 0, 51.0, 0, -1 / 3600), 1800, 3600) == []
    two = cells_covered((10 - 0.5 / 3600, 1 / 3600, 0, 65 + 0.5 / 3600, 0, -1 / 3600), 7201, 3601)
    assert two == ['N64E010', 'N64E011']


def test_harvest_from_rasters_with_tags_and_xml(tmp_dir):
    raster = os.path.join(tmp_dir, 'N06E126_DEM.tif')
    write_geotiff(raster, np.zeros((121, 121), dtype=np.float32), point_geotransform(126, 6, 121), nodata=-32767)
    from osgeo import gdal

    with gdal.Open(raster, gdal.GA_Update) as dataset:
        dataset.SetMetadataItem('TIFFTAG_SOFTWARE', 'DEM Editor 5.13.0 - optimized')
        dataset.SetMetadataItem('TIFFTAG_DATETIME', '2011:02:10 17:52:23')
    with open(os.path.join(tmp_dir, 'N06E126_DEM.xml'), 'w') as handle:
        handle.write(textwrap.dedent('''\
            <?xml version="1.0" encoding="UTF-8"?>
            <gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gco="http://www.isotc211.org/2005/gco">
              <gmd:fileIdentifier><gco:CharacterString>N06E126_DEM</gco:CharacterString></gmd:fileIdentifier>
              <gmd:identificationInfo><gmd:MD_DataIdentification><gmd:citation><gmd:CI_Citation>
                <gmd:date><gmd:CI_Date><gmd:date><gco:Date>2019-07-18</gco:Date></gmd:date></gmd:CI_Date></gmd:date>
              </gmd:CI_Citation></gmd:citation></gmd:MD_DataIdentification></gmd:identificationInfo>
            </gmd:MD_Metadata>
        '''))
    profile = load_profile(PROFILE)
    rows = rows_from_rasters([raster], profile)
    row = rows['N06E126']
    assert row['dted_level'] == 2
    assert row['source_version'] == '5.13.0'
    assert row['source_date'] == dt.date(2011, 2, 10)
    assert row['source_id'] == 'N06E126_DEM'
    assert row['compilation_date'] == dt.date(2019, 7, 18)
    assert row['source_metadata_file'].endswith('N06E126_DEM.xml')

    with open(os.path.join(tmp_dir, 'N06E126_DEM.xml'), 'w') as handle:
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
    for name, (lon0, lat0) in (('N06E126_DEM', (126, 6)), ('S06E030_DEM', (30, -6))):
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
    assert by_field['S06E030']['source_id'] == 'S06E030_DEM'

    out = os.path.join(tmp_dir, 'built.gpkg')
    index = build_index(out, from_footprints=footprints, cell_field='item_name', profile=load_profile(PROFILE),
                        product='DTED2')
    assert len(index) == 2 and index.level == 2 and index.meta['product'] == 'DTED2'
    assert index.meta['profile'] == 'dted_profile.toml'

    folder = os.path.join(tmp_dir, 'dted')
    os.makedirs(folder)
    write_dted(os.path.join(folder, 'n06e126.dt0'), np.zeros((121, 121), dtype=np.int16), 126, 6)
    updated = build_index(out, from_dted=[folder], update=True, level=0)
    assert len(updated) == 2
    assert updated.get('N06E126').get('producer_code') is None
    assert updated.get('N06E126')['source_id'] == 'N06E126_DEM'
    assert updated.get('N06E126')['source_file'].endswith('n06e126.dt0')
    assert read_index(out).level == 0


def _table_gpkg(path, records, layer='tiles'):
    """A GeoPackage layer whose fields come from the records' keys (text unless
    the name says otherwise) and whose polygons are the cells named in 'Cell_ID'."""
    from osgeo import ogr, osr

    from egmtrans.dted.harvest import CELL_PATTERN
    from egmtrans.dted.header import parse_cell_id

    driver = ogr.GetDriverByName('GPKG')
    source = driver.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    out = source.CreateLayer(layer, srs, ogr.wkbPolygon)
    kinds = {'ABS_VERT_ACC': ogr.OFTReal, 'data_edition': ogr.OFTInteger, 'version': ogr.OFTInteger,
             'made': ogr.OFTDateTime}
    names = list(records[0])
    for name in names:
        out.CreateField(ogr.FieldDefn(name, kinds.get(name, ogr.OFTString)))
    for record in records:
        feature = ogr.Feature(out.GetLayerDefn())
        for name in names:
            if record.get(name) is not None:
                feature.SetField(name, record[name])
        match = CELL_PATTERN.search(str(record.get('Cell_ID', '')).upper())
        if match:
            lon0, lat0 = parse_cell_id(match.group(1))
            ring = [(lon0, lat0), (lon0 + 1, lat0), (lon0 + 1, lat0 + 1), (lon0, lat0 + 1), (lon0, lat0)]
            feature.SetGeometry(ogr.CreateGeometryFromWkt(
                'POLYGON((' + ', '.join(f'{x} {y}' for x, y in ring) + '))'))
        out.CreateFeature(feature)
        feature = None
    source = None
    return path


def test_rows_from_table_maps_names_coerces_and_prefers(tmp_dir):
    from egmtrans.dted.harvest import parse_column_map, parse_constants, rows_from_table

    path = _table_gpkg(os.path.join(tmp_dir, 'catalog.gpkg'), [
        {'Cell_ID': 'tile N06E126 v1', 'ABS_VERT_ACC': 3.0, 'data_edition': 2, 'made': '2024/07/15 10:00:00',
         'ref': 'A', 'version': 1},
        {'Cell_ID': 'tile N06E126 v2', 'ABS_VERT_ACC': 6.0, 'data_edition': 3, 'made': '2025/01/10 00:00:00',
         'ref': 'B', 'version': 2},
        {'Cell_ID': 'tile S06E030 v1', 'ABS_VERT_ACC': 12.4, 'data_edition': 1, 'made': None, 'ref': 'C', 'version': 1},
        {'Cell_ID': 'no cell here', 'ABS_VERT_ACC': 1.0, 'data_edition': 1, 'made': None, 'ref': 'D', 'version': 1},
    ])
    with pytest.raises(ValueError, match='share a cell.*--prefer'):
        rows_from_table(path)
    with pytest.raises(ValueError, match='--cell-field'):
        rows_from_table(path, column_map={'cell_id': 'Cell_ID'})
    with pytest.raises(ValueError, match='no column'):
        rows_from_table(path, column_map=parse_column_map(['notes=missing']), prefer='version')
    with pytest.raises(ValueError, match='--prefer nope'):
        rows_from_table(path, prefer='nope')

    imported = rows_from_table(
        path, column_map=parse_column_map(['compilation_date=made', 'unique_ref_dsi=ref']),
        constants=parse_constants(['security_code=U', 'producer_code=USTEST', 'rel_horiz_acc=NA']), prefer='version',
    )
    assert imported.cell_source == 'column Cell_ID' and imported.layer == 'tiles' and imported.rows_read == 4
    assert imported.rows_without_cell == 1 and imported.duplicates_resolved == 1
    assert imported.mapped == {'data_edition': 'data_edition', 'abs_vert_acc': 'ABS_VERT_ACC',
                               'compilation_date': 'made', 'unique_ref_dsi': 'ref'}
    assert imported.dropped == ['version'] and imported.constants['rel_horiz_acc'] is None
    row = imported.rows['N06E126']
    assert row['abs_vert_acc'] == 6 and row['data_edition'] == 3 and row['compilation_date'] == dt.date(2025, 1, 10)
    assert row['unique_ref_dsi'] == 'B' and row['security_code'] == 'U' and row['producer_code'] == 'USTEST'
    other = imported.rows['S06E030']
    assert other['abs_vert_acc'] == 13 and other['compilation_date'] is None and other['unique_ref_dsi'] == 'C'

    # A constant beats a mapped column of the same name; the cell field's text lands in source_id.
    by_field = rows_from_table(path, cell_field='Cell_ID', constants={'data_edition': 7}, prefer='version')
    assert by_field.rows['N06E126']['data_edition'] == 7 and by_field.rows['N06E126']['source_id'] == 'tile N06E126 v2'
    assert 'data_edition' not in by_field.mapped

    bad = _table_gpkg(os.path.join(tmp_dir, 'bad.gpkg'), [
        {'Cell_ID': 'N06E126', 'security_code': 'X', 'ABS_VERT_ACC': 1.0, 'data_edition': 1, 'made': None,
         'ref': 'A', 'version': 1},
    ])
    with pytest.raises(ValueError, match=r'row 1 \(N06E126\): security_code <- security_code: .*not one of'):
        rows_from_table(bad)
    with pytest.raises(ValueError, match='not an index column'):
        parse_constants(['cell_id=N06E126'])
    with pytest.raises(ValueError, match='INDEX_COLUMN=VALUE'):
        parse_constants(['security_code'])
    assert parse_constants(['compilation_date=2024-07', 'abs_vert_acc=7.2']) == {
        'compilation_date': dt.date(2024, 7, 1), 'abs_vert_acc': 8}


def test_rows_from_table_reads_parquet_and_the_geometry(tmp_dir):
    pa = pytest.importorskip('pyarrow')
    import json

    import pyarrow.parquet as pq

    from egmtrans.dted.harvest import rows_from_table

    rings = [cell_polygon('N06E126'), cell_polygon('S06E030')]
    table = pa.table({
        'abs_vert_acc': pa.array([7.2, None], pa.float64()),
        'data_edition': pa.array([1, 2], pa.int64()),
        'compilation_date': pa.array([dt.datetime(2024, 7, 15, 10), dt.datetime(2025, 1, 1)], pa.timestamp('s')),
        'geometry': pa.array([polygon_wkb(ring) for ring in rings], pa.binary()),
    }).replace_schema_metadata({'geo': json.dumps({
        'version': '1.1.0', 'primary_column': 'geometry', 'columns': {'geometry': {'encoding': 'WKB'}}})})
    path = os.path.join(tmp_dir, 'cells.parquet')
    pq.write_table(table, path)
    imported = rows_from_table(path)
    assert imported.cell_source == 'the geometry' and sorted(imported.rows) == ['N06E126', 'S06E030']
    assert imported.rows['N06E126']['abs_vert_acc'] == 8
    assert imported.rows['N06E126']['compilation_date'] == dt.date(2024, 7, 15)
    assert imported.rows['S06E030']['abs_vert_acc'] is None and imported.rows['S06E030']['data_edition'] == 2
    assert imported.dropped == [] and imported.layer is None


@pytest.mark.parametrize('with_lxml', [True, False])
def test_harvest_xml_tries_xpaths_in_order(tmp_dir, monkeypatch, with_lxml):
    import sys

    if with_lxml:
        pytest.importorskip('lxml')
    else:
        monkeypatch.setitem(sys.modules, 'lxml', None)
    profile_path = os.path.join(tmp_dir, 'profile.toml')
    with open(profile_path, 'w') as handle:
        handle.write(textwrap.dedent('''\
            schema = 1
            [product]
            dted_level = 2
            [harvest.xml]
            sidecar = "{stem}.xml"
            [harvest.xml.namespaces]
            q = "urn:example:quality"
            [harvest.xml.fields]
            abs_vert_acc = ["//q:measured/q:le90", "//q:estimated/q:le90"]
            source_id = { xpath = ["//q:id", "//q:name"], pattern = "tile-(.*)" }
            notes = { xpath = ["//q:note"] }
        '''))
    profile = load_profile(profile_path)
    assert profile.harvest.xml_fields['abs_vert_acc']['xpath'] == ['//q:measured/q:le90', '//q:estimated/q:le90']
    assert profile.harvest.xml_fields['notes']['xpath'] == '//q:note'
    cases = (
        ('a', '<q:estimated><q:le90>3.2</q:le90></q:estimated><q:name>tile-A</q:name>', (4, 'A')),
        ('b', '<q:measured><q:le90>2.0</q:le90></q:measured><q:estimated><q:le90>9</q:le90></q:estimated>'
              '<q:id>tile-B</q:id>', (2, 'B')),
        ('c', '<q:measured><q:le90></q:le90></q:measured><q:estimated><q:le90>5</q:le90></q:estimated>', (5, None)),
    )
    for name, body, expected in cases:
        raster = os.path.join(tmp_dir, f'{name}_N06E126.tif')
        write_geotiff(raster, np.zeros((121, 121), dtype=np.float32), point_geotransform(126, 6, 121), nodata=-32767)
        with open(os.path.join(tmp_dir, f'{name}_N06E126.xml'), 'w') as handle:
            handle.write(f'<q:root xmlns:q="urn:example:quality">{body}</q:root>')
        row = rows_from_rasters([raster], profile)['N06E126']
        assert (row['abs_vert_acc'], row['source_id']) == expected, name

    with open(profile_path, 'w') as handle:
        handle.write('schema = 1\n[harvest.xml.fields]\nabs_vert_acc = ["//q:a", ""]\n')
    with pytest.raises(ValueError, match='abs_vert_acc: xpath must be'):
        load_profile(profile_path)


def test_header_dates_keep_to_the_readers_century_but_catalog_dates_do_not():
    assert '1980-2079' in check_value(COLUMNS_BY_NAME['compilation_date'], '1975-06')
    assert '1980-2079' in check_value(COLUMNS_BY_NAME['maintenance_date'], dt.date(2080, 1, 1))
    assert check_value(COLUMNS_BY_NAME['compilation_date'], '2024-07') is None
    assert check_value(COLUMNS_BY_NAME['source_date'], '1975-06') is None, 'a catalog date may hold any year'


def test_a_blank_required_value_is_null_and_reported(tmp_dir):
    from egmtrans.dted.index import normalize_value

    column = COLUMNS_BY_NAME['producer_code']
    assert normalize_value(column, '   ') is None and normalize_value(column, 'USNGA') == 'USNGA'
    assert normalize_value(COLUMNS_BY_NAME['dsi_free_text'], '   ') == '   ', 'optional text keeps its blanks'
    index = DtedIndex(path='<memory>', rows={
        'N06E126': new_row('N06E126', security_code='U', data_edition=1, match_merge_version='A',
                           compilation_date='2024-07', producer_code='  ', abs_horiz_acc=3, abs_vert_acc=3,
                           rel_horiz_acc=3, rel_vert_acc=3),
        'N06E127': new_row('N06E127', security_code='U', data_edition=1, match_merge_version='A',
                           compilation_date='2024-07', producer_code=None, abs_horiz_acc=3, abs_vert_acc=3,
                           rel_horiz_acc=3, rel_vert_acc=3),
    })
    issues = validate_index(index)
    blank = [issue for issue in issues if issue.key == 'producer_code' and 'NULL or blank' in issue.message]
    assert len(blank) == 1 and blank[0].severity == 'warning'
    assert 'N06E126' in blank[0].message and 'N06E127' in blank[0].message
    assert 'the profile must supply it' in blank[0].message
