"""Tests for egmtrans.tiling: seams, merging and the boundary report, without grids."""

import random

import numpy as np

from egmtrans.tiling import (
    DisjointSet,
    PatchStats,
    TableRow,
    TileAnalysis,
    WaterBody,
    WaterLevelTable,
    edge_patch_stats,
    edge_touching_labels,
    extract_edges,
    find_seams,
    format_boundary_report,
    merge_patches,
    single_tile_water_bodies,
)

# Unit-spaced 5x5 tiles: posts at x = 0..4 and y = 4..0.
GT_A = (-0.5, 1.0, 0.0, 4.5, 0.0, -1.0)
GT_SHARED_EAST = (3.5, 1.0, 0.0, 4.5, 0.0, -1.0)    # posts x = 4..8: column 4 is shared
GT_ABUTTING_EAST = (4.5, 1.0, 0.0, 4.5, 0.0, -1.0)  # posts x = 5..9: one spacing east
GT_GAP_EAST = (5.5, 1.0, 0.0, 4.5, 0.0, -1.0)       # posts x = 6..10: two spacings east


def _tile(tile_id, labeled, gt, patches, output=True, crs="EPSG:4326", is_dted=True):
    labeled = np.asarray(labeled, dtype=np.int32)
    rows, cols = labeled.shape
    tile = TileAnalysis(
        tile_id=tile_id,
        input_file=f"tile{tile_id}.dt0",
        output_file=f"out{tile_id}.dt0" if output else None,
        crs_key=crs,
        geotransform=gt,
        rows=rows,
        cols=cols,
        is_dted=is_dted,
    )
    tile.edges = extract_edges(labeled, gt)
    tile.patches = {label: PatchStats(*spec) for label, spec in patches.items()}
    return tile


def _band(label, rows=(1, 2), cols=slice(None)):
    """A 5x5 label array with *label* on the given rows and columns."""
    labeled = np.zeros((5, 5), dtype=np.int32)
    for r in rows:
        labeled[r, cols] = label
    return labeled


class TestEdgeRecords:
    def test_extract_edges_records_only_labels_above_one(self):
        labeled = np.array(
            [
                [1, 1, 2, 2, 0],
                [1, 0, 2, 2, 0],
                [0, 0, 0, 0, 3],
                [0, 0, 0, 0, 3],
            ]
        )
        gt = (-0.5, 1.0, 0.0, 4.5, 0.0, -1.0)
        edges = extract_edges(labeled, gt)

        assert edges["N"].idx.tolist() == [2, 3] and edges["N"].labels.tolist() == [2, 2]
        assert edges["N"].across == 4.0 and edges["N"].along0 == 0.0
        assert edges["N"].step == 1.0 and edges["N"].length == 5
        assert edges["W"].idx.size == 0, "ocean must not be recorded"
        assert edges["W"].across == 0.0 and edges["W"].along0 == 4.0 and edges["W"].step == -1.0
        assert edges["E"].idx.tolist() == [2, 3] and edges["E"].labels.tolist() == [3, 3]
        assert edges["E"].across == 4.0
        assert edges["S"].idx.tolist() == [4] and edges["S"].across == 1.0
        assert edge_touching_labels(edges).tolist() == [2, 3]

    def test_edge_patch_stats_reads_the_height_at_an_edge_post(self):
        labeled = _band(2, rows=(0,), cols=slice(1, 4))
        heights = np.full((5, 5), 7.0)
        heights[0, 1:4] = 150.004
        edges = extract_edges(labeled, GT_A)
        levels = np.array([np.nan, 0.0, 149.375])
        counts = np.array([0, 0, 3])
        stats = edge_patch_stats(heights, edges, levels, counts)
        assert stats == {2: PatchStats(height_cm=15000, min_value=149.375, count=3)}


class TestSeams:
    def test_seam_shared_column_matches_posts_by_coordinate(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3, rows=(1, 2, 3)), GT_SHARED_EAST, {3: (15000, 149.4, 15)})
        seams = find_seams([a, b])
        assert len(seams) == 1
        seam = seams[0]
        assert (seam.a, seam.a_side, seam.b, seam.b_side) == (1, "E", 2, "W")
        assert seam.a_idx.tolist() == [1, 2] and seam.b_idx.tolist() == [1, 2]
        assert seam.a_cover == (0, 4) and seam.b_cover == (0, 4)

    def test_seam_abutting_column_one_post_apart(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3), GT_ABUTTING_EAST, {3: (15000, 149.4, 10)})
        seams = find_seams([a, b])
        assert len(seams) == 1
        assert seams[0].a_idx.tolist() == [1, 2] and seams[0].b_idx.tolist() == [1, 2]

    def test_no_seam_two_posts_apart(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3), GT_GAP_EAST, {3: (15000, 149.4, 10)})
        assert find_seams([a, b]) == []

    def test_no_seam_across_different_crs(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3), GT_SHARED_EAST, {3: (15000, 149.4, 10)}, crs="EPSG:32633")
        assert find_seams([a, b]) == []

    def test_seam_matches_half_rate_row(self):
        """A coarser southern neighbor (every second column) matches every second post."""
        north = np.zeros((3, 7), dtype=np.int32)
        north[2, :] = 2
        south = np.zeros((3, 4), dtype=np.int32)
        south[0, :] = 2
        a = _tile(1, north, (-0.5, 1.0, 0.0, 2.5, 0.0, -1.0), {2: (15000, 149.6, 7)})
        b = _tile(2, south, (-1.0, 2.0, 0.0, 0.5, 0.0, -1.0), {2: (15000, 149.4, 4)})
        seams = find_seams([a, b])
        assert len(seams) == 1
        seam = seams[0]
        assert (seam.a_side, seam.b_side) == ("S", "N")
        assert list(zip(seam.a_idx.tolist(), seam.b_idx.tolist())) == [(0, 0), (2, 1), (4, 2), (6, 3)]
        assert seam.a_cover == (0, 6) and seam.b_cover == (0, 3)

    def test_seam_between_north_and_south_shared_row(self):
        north = _band(2, rows=(4,), cols=slice(1, 3))
        south = _band(5, rows=(0,), cols=slice(0, 4))
        a = _tile(1, north, GT_A, {2: (15000, 149.6, 2)})
        b = _tile(2, south, (-0.5, 1.0, 0.0, 0.5, 0.0, -1.0), {5: (15000, 149.4, 4)})  # posts y = 0..-4
        seams = find_seams([a, b])
        assert len(seams) == 1
        assert seams[0].a_idx.tolist() == [1, 2] and seams[0].b_idx.tolist() == [1, 2]

    def test_failed_tile_takes_part_in_no_seam(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3), GT_SHARED_EAST, {3: (15000, 149.4, 10)})
        b.error = "could not open"
        assert find_seams([a, b]) == []


class TestMerge:
    def _pair(self, height_b=15000, output_b=True):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3), GT_SHARED_EAST, {3: (height_b, 149.4, 15)}, output=output_b)
        return a, b

    def test_merge_takes_minimum_over_tiles(self):
        a, b = self._pair()
        levels, bodies = merge_patches([a, b], find_seams([a, b]))
        assert len(bodies) == 1
        body = bodies[0]
        assert body.level == 149.4 and body.posts == 25 and body.members == [(1, 2), (2, 3)]
        assert levels[1].levels == {2: 149.4} and levels[2].levels == {3: 149.4}
        assert levels[1].expected_counts == {2: 10} and levels[2].expected_counts == {3: 15}

    def test_merge_requires_equal_input_height(self):
        a, b = self._pair(height_b=15100)
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        assert len(bodies) == 2
        assert any("kept separate" in note for body in bodies for note in body.notes)

        a, b = self._pair(height_b=15001)  # within the 1 cm labeling tolerance
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        assert len(bodies) == 1

    def test_merge_is_independent_of_tile_order(self):
        def run(order):
            tiles = [
                _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)}),
                _tile(2, _band(3), GT_SHARED_EAST, {3: (15000, 149.4, 15)}),
                _tile(3, _band(4), (7.5, 1.0, 0.0, 4.5, 0.0, -1.0), {4: (15000, 149.2, 12)}),  # x = 8..12
            ]
            tiles = [tiles[i] for i in order]
            levels, bodies = merge_patches(tiles, find_seams(tiles))
            return (
                {tid: sorted(tl.levels.items()) for tid, tl in levels.items()},
                [(b.height_cm, b.level, b.posts, b.members, sorted(b.open_edges)) for b in bodies],
            )

        orders = [[0, 1, 2], [2, 1, 0], [1, 0, 2], [2, 0, 1]]
        random.Random(1).shuffle(orders)
        results = [run(order) for order in orders]
        assert all(r == results[0] for r in results)
        assert results[0][1][0][1] == 149.2 and results[0][1][0][2] == 37

    def test_open_edges_only_where_no_neighbor(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        b = _tile(2, _band(3, cols=slice(0, 3)), GT_SHARED_EAST, {3: (15000, 149.4, 6)})
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        assert bodies[0].open_edges == [(1, "W")]

    def test_context_tile_gets_no_tile_levels(self):
        a, b = self._pair(output_b=False)
        levels, bodies = merge_patches([a, b], find_seams([a, b]))
        assert set(levels) == {1}
        assert levels[1].levels == {2: 149.4}, "the context tile must still lower the level"
        assert bodies[0].posts == 25

    def test_single_tile_bodies_have_all_touched_sides_open(self):
        labeled = _band(2)
        labeled[1:3, 0] = 0  # the band no longer reaches the west edge
        tile = _tile(1, labeled, GT_A, {2: (15000, 149.6, 8)})
        bodies = single_tile_water_bodies(tile)
        assert len(bodies) == 1 and bodies[0].open_edges == [(1, "E")]
        assert [(c.side, c.start, c.end) for c in bodies[0].crossings] == [("E", 2.0, 3.0)]

    def test_crossings_split_into_runs(self):
        labeled = np.zeros((5, 5), dtype=np.int32)
        labeled[0, 1] = 2   # a U-shaped patch touching the north edge twice
        labeled[0, 3] = 2
        labeled[1, 1:4] = 2
        tile = _tile(1, labeled, GT_A, {2: (15000, 149.6, 5)})
        bodies = single_tile_water_bodies(tile)
        assert [(c.side, c.start, c.end) for c in bodies[0].crossings] == [("N", 1.0, 1.0), ("N", 3.0, 3.0)]


class TestContainment:
    """Whether a flat area is water is decided for the whole merged body."""

    def _pair(self, above_b=6, below_b=40):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 100, 100, 0)})
        b = _tile(2, _band(3), GT_SHARED_EAST, {3: (15000, 149.4, 20, above_b, below_b)})
        return a, b

    def test_body_classified_across_tiles(self):
        a, b = self._pair()
        # 106 of 146 boundary posts above: below the 80% default, water at 70%.
        levels, bodies = merge_patches([a, b], find_seams([a, b]), None, 0.8)
        assert len(bodies) == 1 and bodies[0].water is False
        assert abs(bodies[0].containment - 106 / 146) < 1e-12
        assert levels[1].levels == {} and levels[1].not_water == {2}
        assert levels[2].levels == {} and levels[2].not_water == {3}
        assert levels[1].expected_counts == {2: 100}

        levels, bodies = merge_patches([a, b], find_seams([a, b]), None, 0.7)
        assert bodies[0].water is True
        assert levels[1].levels == {2: 149.4} and levels[1].not_water == set()

    def test_fragment_alone_can_be_terrain_while_the_body_is_water(self):
        a, b = self._pair()
        assert single_tile_water_bodies(b, 0.8) == []
        assert len(single_tile_water_bodies(a, 0.8)) == 1

    def test_report_skips_non_water(self):
        a, b = self._pair()
        _, bodies = merge_patches([a, b], find_seams([a, b]), None, 0.8)
        lines = format_boundary_report(bodies, [a, b])
        assert lines == [
            "0 water bodies span more than one tile and were set to one level each; 0 touch the run boundary."
        ]

    def test_no_boundary_means_water(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 100, 0, 0)})
        _, bodies = merge_patches([a], [], None, 0.8)
        assert bodies[0].water is True


class TestGeometry:
    def test_geometry_edges_find_seams_without_labels(self):
        from egmtrans.tiling import TileAnalysis, connected_tiles, geometry_edges

        def geometry(tile_id, gt):
            return TileAnalysis(tile_id, f"t{tile_id}.dt0", None, "EPSG:4326", gt, 5, 5, True,
                                edges=geometry_edges(gt, 5, 5))

        a, b, far = geometry(1, GT_A), geometry(2, GT_SHARED_EAST), geometry(3, (20.5, 1.0, 0.0, 4.5, 0.0, -1.0))
        seams = find_seams([a, b, far])
        assert [(s.a, s.b) for s in seams] == [(1, 2)]
        assert seams[0].a_idx.size == 0 and seams[0].a_cover == (0, 4)
        assert connected_tiles(seams, {1}) == {1, 2}
        assert connected_tiles(seams, {3}) == {3}

    def test_geometry_edges_match_extract_edges(self):
        from egmtrans.tiling import geometry_edges

        labeled = np.zeros((4, 6), dtype=np.int32)
        gt = (-0.5, 1.0, 0.0, 3.5, 0.0, -1.0)
        for side, edge in extract_edges(labeled, gt).items():
            frame = geometry_edges(gt, 4, 6)[side]
            assert (edge.across, edge.along0, edge.step, edge.length) == (
                frame.across, frame.along0, frame.step, frame.length
            )


class TestWaterLevelTable:
    def _body_and_tile(self):
        tile = _tile(1, _band(2), GT_A, {2: (15000, 149.6, 10)})
        body = single_tile_water_bodies(tile)[0]
        return tile, body

    def test_table_lowers_the_level(self):
        tile, _ = self._body_and_tile()
        # The band's east crossing is on x = 4 (side E) from y = 2 to y = 3.
        table = WaterLevelTable([TableRow(15000, 149.0, 40, 3, "E", 4.0, 1.0, 3.5)], "EGM2008", "EGM96")
        _, bodies = merge_patches([tile], [], table)
        assert bodies[0].level == 149.0 and bodies[0].table_level == 149.0

    def test_table_never_raises_the_level(self):
        tile, _ = self._body_and_tile()
        table = WaterLevelTable([TableRow(15000, 150.0, 40, 3, "E", 4.0, 1.0, 3.5)], "EGM2008", "EGM96")
        _, bodies = merge_patches([tile], [], table)
        assert bodies[0].level == 149.6 and bodies[0].table_level is None
        assert any("stale" in note for note in bodies[0].notes)

    def test_table_row_must_overlap_and_match_height(self):
        tile, _ = self._body_and_tile()
        rows = [
            TableRow(15000, 149.0, 40, 3, "E", 4.0, -3.0, -1.0),   # elsewhere on the same line
            TableRow(15200, 148.0, 40, 3, "E", 4.0, 1.0, 3.5),     # a different water body
            TableRow(15001, 149.1, 40, 3, "W", 4.0, 1.0, 3.5),     # wrong side
        ]
        table = WaterLevelTable(rows, "EGM2008", "EGM96")
        _, bodies = merge_patches([tile], [], table)
        assert bodies[0].level == 149.6

    def test_lookup_tolerates_a_centimeter_and_a_line_rounding(self):
        table = WaterLevelTable([TableRow(15000, 149.0, 1, 1, "E", 4.0, 1.0, 3.5)], "EGM2008", "EGM96")
        assert table.lookup("E", 4.0 + 5e-7, 15001)
        assert not table.lookup("E", 4.01, 15000)
        assert not table.lookup("E", 4.0, 15003)


class TestDisjointSet:
    def test_union_find_groups(self):
        sets = DisjointSet()
        for item in [(1, 2), (1, 3), (2, 2), (3, 5)]:
            sets.add(item)
        sets.union((1, 2), (2, 2))
        sets.union((2, 2), (3, 5))
        assert sets.find((3, 5)) == (1, 2)
        assert sets.groups() == {(1, 2): [(1, 2), (2, 2), (3, 5)], (1, 3): [(1, 3)]}


class TestReport:
    def test_boundary_report_lists_height_level_and_tile_edges(self):
        a = _tile(1, _band(2), GT_A, {2: (15000, 149.41, 10)})
        b = _tile(2, _band(3, cols=slice(0, 3)), GT_SHARED_EAST, {3: (15000, 149.55, 6)}, output=False)
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        lines = format_boundary_report(bodies, [a, b])
        text = "\n".join(lines)
        assert "150.00 m" in text and "149.41 m (149 m)" in text
        assert "tile1.dt0:W" in text
        assert "1 water body spans more than one tile" in text and "1 touch the run boundary" in text

    def test_context_tile_is_marked(self):
        a = _tile(1, _band(2, cols=slice(2, 5)), GT_A, {2: (15000, 149.41, 6)})
        b = _tile(2, _band(3), GT_SHARED_EAST, {3: (15000, 149.55, 10)}, output=False)
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        text = "\n".join(format_boundary_report(bodies, [a, b]))
        assert "tile2.dt0:E (context)" in text

    def test_boundary_report_is_capped(self):
        tile = _tile(1, np.zeros((5, 5)), GT_A, {})
        bodies = [
            WaterBody(height_cm=100 * i, level=float(i), posts=10, members=[(1, i + 2)], open_edges=[(1, "N")])
            for i in range(150)
        ]
        lines = format_boundary_report(bodies, [tile], limit=100)
        assert sum(1 for line in lines if line.strip().endswith("tile1.dt0:N")) == 100
        assert any(line.strip() == "... and 50 more" for line in lines)

    def test_no_open_bodies_gives_the_summary_only(self):
        a = _tile(1, _band(2, cols=slice(2, 5)), GT_A, {2: (15000, 149.41, 6)})
        b = _tile(2, _band(3, cols=slice(0, 3)), GT_SHARED_EAST, {3: (15000, 149.55, 6)})
        _, bodies = merge_patches([a, b], find_seams([a, b]))
        lines = format_boundary_report(bodies, [a, b])
        assert lines == [
            "1 water body spans more than one tile and was set to one level each; 0 touch the run boundary."
        ]
