"""The MIL-PRF-89020B schema tables: they tile each record exactly, the
subregions sit where the spec puts them, and the zone rule follows Tables I to III."""

import pytest

from egmtrans.dted import schema
from egmtrans.dted.schema import (
    ACC_FIELDS,
    DSI_FIELDS,
    HEADER_LENGTH,
    LEVELS,
    SUBREGION_FIELDS,
    SUBREGION_LENGTH,
    SUBREGION_START,
    UHL_FIELDS,
    field,
    subregion_offset,
    zone_for,
)


def test_fields_tile_each_record_exactly():
    schema.check_schema()
    assert sum(f.length for f in UHL_FIELDS) == 80
    assert sum(f.length for f in DSI_FIELDS) == 648
    assert sum(f.length for f in ACC_FIELDS) == 2700
    assert HEADER_LENGTH == 3428


def test_keys_are_unique_and_prefixed():
    fields = UHL_FIELDS + DSI_FIELDS + ACC_FIELDS
    keys = [f.key for f in fields]
    assert len(keys) == len(set(keys))
    assert all(k.split('.')[0] == f.record.lower() for k, f in zip(keys, fields, strict=True))


def test_spec_positions_of_well_known_fields():
    assert (field('uhl.abs_vert_acc').start, field('uhl.abs_vert_acc').length) == (29, 4)
    assert (field('uhl.security_code').start, field('uhl.security_code').length) == (33, 3)
    assert (field('uhl.multiple_accuracy').start, field('uhl.multiple_accuracy').length) == (56, 1)
    assert (field('dsi.series').start, field('dsi.series').length) == (60, 5)
    assert (field('dsi.producer_code').start, field('dsi.producer_code').length) == (103, 8)
    assert (field('dsi.vertical_datum').start, field('dsi.vertical_datum').length) == (142, 3)
    assert (field('dsi.partial_cell').start, field('dsi.partial_cell').length) == (290, 2)
    assert (field('dsi.free_text').start, field('dsi.free_text').length) == (493, 156)
    assert (field('acc.abs_horiz_acc').start, field('acc.abs_horiz_acc').length) == (4, 4)
    assert (field('acc.outline_flag').start, field('acc.outline_flag').length) == (56, 2)
    assert (field('acc.nima_trailer').start, field('acc.nima_trailer').length) == (2614, 18)
    assert (field('acc.reserved_trailer').start, field('acc.reserved_trailer').length) == (2632, 69)
    # File offsets: DSI starts at byte 80, ACC at 728, the vertical datum at file offset 221.
    assert field('dsi.vertical_datum').file_offset == 221
    assert field('acc.abs_horiz_acc').file_offset == 731


def test_subregions_follow_section_3_13_5_1():
    assert SUBREGION_START == 58 and SUBREGION_LENGTH == 284
    starts = [SUBREGION_START + i * SUBREGION_LENGTH for i in range(9)]
    assert starts == [58, 342, 626, 910, 1194, 1478, 1762, 2046, 2330]
    assert [subregion_offset(i) + 1 for i in range(9)] == starts
    assert [f.key for f in SUBREGION_FIELDS] == [
        'abs_horiz_acc', 'abs_vert_acc', 'rel_horiz_acc', 'rel_vert_acc', 'coord_count',
    ]
    assert SUBREGION_FIELDS[-1].start == 17 and schema.SUBREGION_COORDS_START == 19
    assert 16 + 2 + 14 * 19 == SUBREGION_LENGTH


def test_level_tables():
    assert {n: (lv.lat_interval_tenths, lv.lat_points) for n, lv in LEVELS.items()} == {
        0: (300, 121), 1: (30, 1201), 2: (10, 3601),
    }
    assert schema.LEVEL_BY_SERIES['DTED2'].number == 2
    assert schema.LEVEL_BY_EXTENSION['.dt1'].number == 1


@pytest.mark.parametrize(
    'lat0, zone, multiplier',
    [(0, 1, 1), (49, 1, 1), (50, 2, 2), (69, 2, 2), (70, 3, 3), (74, 3, 3), (75, 4, 4), (79, 4, 4),
     (80, 5, 6), (89, 5, 6), (-1, 1, 1), (-50, 1, 1), (-51, 2, 2), (-71, 3, 3), (-90, 5, 6)],
)
def test_zone_rule_uses_the_edge_nearer_the_equator(lat0, zone, multiplier):
    assert zone_for(lat0) == (zone, multiplier)


def test_accuracy_titles_name_the_statistic():
    assert 'LE90' in field('uhl.abs_vert_acc').title
    assert 'CE90' in field('acc.abs_horiz_acc').title
    assert 'LE90' in field('acc.abs_vert_acc').title
    assert field('acc.rel_horiz_acc').title.startswith('Point-to-point') and 'CE90' in field('acc.rel_horiz_acc').title
    assert field('acc.rel_vert_acc').title.startswith('Point-to-point') and 'LE90' in field('acc.rel_vert_acc').title
    assert schema.NA_VALUE == 'NA  '
