"""Integration tests for egmtrans.batch: water bodies levelled across tiles.

The tiles are synthetic DTED0 (121 x 121 posts at 30 arc seconds) and Float32
GeoTIFF tiles near 126-128 E, 6-7 N, where EGM2008 - EGM96 changes by meters
across a degree, so a lake split between tiles would get visibly different
levels if each tile were treated on its own. The terrain is a function of the
global post index, so the shared posts of neighboring tiles carry one value.
"""

import os
import shutil

import numpy as np
import pytest

from egmtrans.batch import read_water_levels, run_batch, write_water_levels
from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.file_utils import resolve_io_paths
from egmtrans.io import round_half_away
from egmtrans.transform import analyze_tile, create_datum_array, transform_vertical_datum
from tests.conftest import point_geotransform, read_band, write_dted, write_geotiff

POSTS = 121          # DTED0: the last column of a tile is the first of its eastern neighbor
LAKE_ROWS = slice(40, 80)


def _grids_available() -> bool:
    return all(
        os.path.isfile(os.path.join(BASE_PATH, "datums", DATUM_MAPPING[datum]["grid"]))
        for datum in ("EGM96", "EGM2008")
    )


requires_grids = pytest.mark.skipif(
    not _grids_available(),
    reason="Geoid grid files not present; run 'python download_grids.py' to fetch.",
)


def _terrain(posts, col_offset, dtype):
    """Rising terrain with no two neighboring posts alike, continuous across tiles."""
    i, j = np.mgrid[0:posts, 0:posts]
    return (10 + 3 * i + 5 * (j + col_offset)).astype(dtype)


def dted_tiles(folder, lake_to_east_edge=False):
    """n06e126.dt0 and n06e127.dt0 with a 150 m lake across their shared column."""
    a = _terrain(POSTS, 0, np.int16)
    a[LAKE_ROWS, 90:] = 150                                # to the east edge of the west tile
    b = _terrain(POSTS, POSTS - 1, np.int16)
    b[LAKE_ROWS, : (POSTS if lake_to_east_edge else 30)] = 150   # from the west edge of the east tile
    os.makedirs(folder, exist_ok=True)
    return (
        write_dted(os.path.join(folder, "n06e126.dt0"), a, 126, 6),
        write_dted(os.path.join(folder, "n06e127.dt0"), b, 127, 6),
    )


def geotiff_tiles(folder):
    """Two abutting 120 x 120 Float32 tiles (Copernicus style) with a lake across the seam."""
    n = POSTS - 1
    a = _terrain(n, 0, np.float32)
    a[LAKE_ROWS, 90:] = 150.0
    b = _terrain(n, n, np.float32)
    b[LAKE_ROWS, :30] = 150.0
    os.makedirs(folder, exist_ok=True)
    return (
        write_geotiff(os.path.join(folder, "N06E126_DEM.tif"), a, point_geotransform(126, 6, POSTS)),
        write_geotiff(os.path.join(folder, "N06E127_DEM.tif"), b, point_geotransform(127, 6, POSTS)),
    )


def _run(input_folder, output_folder, **kwargs):
    paths = resolve_io_paths(input_folder, output_folder)
    options = dict(flatten=True, create_mask=False, min_patch_size=16, algorithm="bilinear", assume_yes=True)
    options.update(kwargs)
    return run_batch(paths, "EGM2008", "EGM96", **options)


def _alone(src, out):
    """The level a tile's lake gets when the tile is transformed on its own."""
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)
    return read_band(out)


@requires_grids
class TestOverlappingDted:
    def test_lake_across_overlapping_dted_tiles_gets_one_level(self, tmp_dir):
        src_a, src_b = dted_tiles(os.path.join(tmp_dir, "in"))
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"))
        assert result.exit_code == 0 and result.files_processed == 2 and result.seams == 1

        out_a = read_band(os.path.join(tmp_dir, "out", "n06e126.dt0"))
        out_b = read_band(os.path.join(tmp_dir, "out", "n06e127.dt0"))
        lake_a, lake_b = np.unique(out_a[LAKE_ROWS, 90:]), np.unique(out_b[LAKE_ROWS, :30])
        assert lake_a.size == 1 and lake_b.size == 1
        assert lake_a[0] == lake_b[0], "the lake has a different level on each side of the seam"

        # The level is the lowest transformed value over both parts of the lake,
        # rounded as the DTED write rounds it. (The two parts' own levels happen
        # to round to the same meter here; the GeoTIFF test shows the difference.)
        raw_a = 150 - (create_datum_array(src_a, "EGM96", "bilinear", tmp_dir, tmp_dir)
                       - create_datum_array(src_a, "EGM2008", "bilinear", tmp_dir, tmp_dir))
        raw_b = 150 - (create_datum_array(src_b, "EGM96", "bilinear", tmp_dir, tmp_dir)
                       - create_datum_array(src_b, "EGM2008", "bilinear", tmp_dir, tmp_dir))
        low_a, low_b = raw_a[LAKE_ROWS, 90:].min(), raw_b[LAKE_ROWS, :30].min()
        assert low_a != low_b
        assert lake_a[0] == round_half_away(np.array(min(low_a, low_b)))

        assert len(result.water_bodies) == 1
        body = result.water_bodies[0]
        assert body.height_cm == 15000 and body.tile_ids == [0, 1]
        assert body.posts == 40 * 31 + 40 * 30

    def test_shared_dted_column_is_bit_identical(self, tmp_dir):
        dted_tiles(os.path.join(tmp_dir, "in"))
        _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"))
        out_a = read_band(os.path.join(tmp_dir, "out", "n06e126.dt0"))
        out_b = read_band(os.path.join(tmp_dir, "out", "n06e127.dt0"))
        assert np.array_equal(out_a[:, -1], out_b[:, 0])
        # Land came through the plain per-post transform: 121 distinct inputs, no patch.
        assert np.unique(out_a[:30, -1]).size == 30

    def test_lake_touching_run_boundary_is_reported(self, tmp_dir, log_lines):
        dted_tiles(os.path.join(tmp_dir, "in"), lake_to_east_edge=True)
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"))
        assert result.water_bodies[0].open_edges == [(1, "E")]
        text = "\n".join(log_lines)
        assert "n06e127.dt0:E" in text and "1 touch the run boundary" in text
        assert "n06e126.dt0" not in text.split("open edges")[-1].split("\n")[1]

    def test_result_independent_of_folder_layout(self, tmp_dir):
        flat = os.path.join(tmp_dir, "flat")
        dted_tiles(flat)
        nested = os.path.join(tmp_dir, "nested")
        os.makedirs(os.path.join(nested, "b", "e127"))
        os.makedirs(os.path.join(nested, "a", "e126"))
        shutil.copy(os.path.join(flat, "n06e126.dt0"), os.path.join(nested, "b", "e127", "n06e126.dt0"))
        shutil.copy(os.path.join(flat, "n06e127.dt0"), os.path.join(nested, "a", "e126", "n06e127.dt0"))

        _run(flat, os.path.join(tmp_dir, "out_flat"))
        _run(nested, os.path.join(tmp_dir, "out_nested"))

        for name, nested_path in (("n06e126.dt0", "b/e127"), ("n06e127.dt0", "a/e126")):
            with open(os.path.join(tmp_dir, "out_flat", name), "rb") as f:
                flat_bytes = f.read()
            with open(os.path.join(tmp_dir, "out_nested", nested_path, name), "rb") as f:
                nested_bytes = f.read()
            assert flat_bytes == nested_bytes, f"{name} differs with the folder layout"

    def test_context_folder_is_analysed_but_not_written(self, tmp_dir):
        src_a, src_b = dted_tiles(os.path.join(tmp_dir, "both"))
        run_folder = os.path.join(tmp_dir, "run")
        context = os.path.join(tmp_dir, "context")
        os.makedirs(run_folder)
        os.makedirs(context)
        shutil.copy(src_a, run_folder)
        shutil.copy(src_b, context)

        result = _run(run_folder, os.path.join(tmp_dir, "out"), context_folders=[context])
        assert result.files_processed == 1 and result.seams == 1
        assert sorted(os.listdir(os.path.join(tmp_dir, "out"))) == ["n06e126.dt0"]

        _run(os.path.join(tmp_dir, "both"), os.path.join(tmp_dir, "out_both"))
        with_context = read_band(os.path.join(tmp_dir, "out", "n06e126.dt0"))
        together = read_band(os.path.join(tmp_dir, "out_both", "n06e126.dt0"))
        assert np.array_equal(with_context, together)

    def test_failed_tile_does_not_stop_the_batch(self, tmp_dir, log_lines):
        """A tile whose header reads but whose data does not fails in pass 1.

        (A file GDAL cannot open at all is skipped at discovery, like any
        non-DEM, and never enters the run.)
        """
        folder = os.path.join(tmp_dir, "in")
        dted_tiles(folder, lake_to_east_edge=True)
        broken = write_dted(os.path.join(folder, "n06e128.dt0"), _terrain(POSTS, 2 * (POSTS - 1), np.int16), 128, 6)
        os.truncate(broken, 4000)  # the DTED header is intact, the data records are gone
        with open(os.path.join(folder, "garbage.dt0"), "wb") as f:
            f.write(b"not a DTED file")

        result = _run(folder, os.path.join(tmp_dir, "out"))
        assert result.exit_code == 1 and result.files_processed == 2
        assert [os.path.basename(f) for f, _ in result.failed] == ["n06e128.dt0"]
        outputs = sorted(f for f in os.listdir(os.path.join(tmp_dir, "out")) if not f.endswith(".aux.xml"))
        assert outputs == ["garbage.dt0", "n06e126.dt0", "n06e127.dt0"], "the failed tile's copy must go"
        assert result.water_bodies[0].open_edges == [(1, "E")], "the lake next to the failed tile must be open"
        assert any("Skipping garbage.dt0" in line for line in log_lines)

    def test_analysis_labels_match_transform_labels(self, tmp_dir):
        src_a, _ = dted_tiles(os.path.join(tmp_dir, "in"))
        tile = analyze_tile(src_a, "EGM2008", "EGM96", "bilinear", 16, tmp_dir, 0, "unused.dt0")
        out = os.path.join(tmp_dir, "a.dt0")
        transform_vertical_datum(src_a, out, "EGM2008", "EGM96", True, True, 16, "bilinear", save_log=False)
        mask = read_band(os.path.join(tmp_dir, "a_mask.tif"))
        east = tile.edges["E"]
        assert np.array_equal(np.flatnonzero(mask[:, -1] > 1), east.idx)
        assert np.array_equal(mask[:, -1][east.idx], east.labels)
        assert tile.patches[east.labels[0]].count == int((mask == east.labels[0]).sum())


@requires_grids
class TestContainmentAcrossTiles:
    def _tiles(self, folder):
        """A lake whose part in the east tile is a sliver against a lower step.

        On its own that sliver is bounded mostly by lower posts and would be
        taken for a slope band; over the whole body 81% of the boundary lies
        above the water, so the merge keeps it and both sides of the seam agree.
        """
        a = _terrain(POSTS, 0, np.int16)
        a[LAKE_ROWS, 60:] = 150
        b = _terrain(POSTS, POSTS - 1, np.int16)
        b[LAKE_ROWS, :3] = 150
        b[LAKE_ROWS, 3:10] = 100
        os.makedirs(folder, exist_ok=True)
        return (
            write_dted(os.path.join(folder, "n06e126.dt0"), a, 126, 6),
            write_dted(os.path.join(folder, "n06e127.dt0"), b, 127, 6),
        )

    def test_water_body_classified_across_tiles(self, tmp_dir):
        src_a, src_b = self._tiles(os.path.join(tmp_dir, "in"))
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"), create_mask=True)
        body = next(b for b in result.water_bodies if b.height_cm == 15000)
        assert body.water and abs(body.containment - 168 / 208) < 1e-12

        out_a = read_band(os.path.join(tmp_dir, "out", "n06e126.dt0"))
        out_b = read_band(os.path.join(tmp_dir, "out", "n06e127.dt0"))
        assert np.array_equal(out_a[:, -1], out_b[:, 0])
        assert np.unique(out_b[LAKE_ROWS, :3]).size == 1 and out_b[40, 0] == out_a[40, 60]
        mask_b = read_band(os.path.join(tmp_dir, "out", "n06e127_mask.tif"))
        assert np.all(mask_b[LAKE_ROWS, :3] > 1)

        alone = os.path.join(tmp_dir, "alone.dt0")
        transform_vertical_datum(src_b, alone, "EGM2008", "EGM96", True, True, 16, "bilinear", save_log=False)
        mask_alone = read_band(os.path.join(tmp_dir, "alone_mask.tif"))
        assert np.all(mask_alone[LAKE_ROWS, :3] == 0), "the sliver alone should be taken for a slope band"

    def test_threshold_is_a_parameter(self, tmp_dir):
        self._tiles(os.path.join(tmp_dir, "in"))
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"), min_containment=0.9)
        body = next(b for b in result.water_bodies if b.height_cm == 15000)
        assert body.water is False
        assert not [b for b in result.water_bodies if b.water and b.height_cm == 15000]


@requires_grids
class TestContextDiscovery:
    def test_context_parent_folder_analyzes_only_adjoining_tiles(self, tmp_dir, log_lines):
        src_a, src_b = dted_tiles(os.path.join(tmp_dir, "both"))
        run_folder = os.path.join(tmp_dir, "run")
        os.makedirs(run_folder)
        shutil.copy(src_a, run_folder)
        context = os.path.join(tmp_dir, "context", "delivery", "e127")
        os.makedirs(context)
        shutil.copy(src_b, context)
        far = _terrain(POSTS, 0, np.int16)
        write_dted(os.path.join(tmp_dir, "context", "n10e140.dt0"), far, 140, 10)

        result = _run(run_folder, os.path.join(tmp_dir, "out"), context_folders=[os.path.join(tmp_dir, "context")])
        analyzed = sorted(os.path.basename(t.input_file) for t in result.tiles)
        assert analyzed == ["n06e126.dt0", "n06e127.dt0"], "the far tile must not be analyzed"
        assert any("2 DEM(s) found; 1 adjoin the run" in line for line in log_lines)
        assert result.seams == 1


@requires_grids
class TestAbuttingGeotiff:
    def test_lake_across_abutting_geotiff_tiles_gets_one_level(self, tmp_dir):
        src_a, src_b = geotiff_tiles(os.path.join(tmp_dir, "in"))
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"))
        assert result.exit_code == 0 and result.seams == 1 and len(result.water_bodies) == 1

        out_a = read_band(os.path.join(tmp_dir, "out", "N06E126_DEM.tif"))
        out_b = read_band(os.path.join(tmp_dir, "out", "N06E127_DEM.tif"))
        lake_a, lake_b = np.unique(out_a[LAKE_ROWS, 90:]), np.unique(out_b[LAKE_ROWS, :30])
        assert lake_a.size == 1 and lake_b.size == 1 and lake_a[0] == lake_b[0]

        alone_a = _alone(src_a, os.path.join(tmp_dir, "alone_a.tif"))[LAKE_ROWS, 90:]
        alone_b = _alone(src_b, os.path.join(tmp_dir, "alone_b.tif"))[LAKE_ROWS, :30]
        assert abs(alone_a[0, 0] - alone_b[0, 0]) > 0.05, "the test tiles do not disagree on their own"
        assert lake_a[0] == min(alone_a[0, 0], alone_b[0, 0])


@requires_grids
class TestWaterLevelTable:
    def test_export_lists_edge_crossings(self, tmp_dir):
        dted_tiles(os.path.join(tmp_dir, "in"))
        table_path = os.path.join(tmp_dir, "levels.csv")
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"), export_water_levels=table_path)

        table = read_water_levels(table_path, "EGM2008", "EGM96")
        sides = sorted((row.side, round(row.line, 6)) for row in table.rows)
        assert sides == [("E", 127.0), ("W", 127.0)]
        assert all(row.height_cm == 15000 and row.level == result.water_bodies[0].level for row in table.rows)
        assert all(row.tiles == 2 for row in table.rows)
        with open(table_path) as f:
            head = f.read(300)
        assert "# source: EGM2008" in head and "# target: EGM96" in head

    def test_two_runs_with_table_agree(self, tmp_dir):
        """A later run over one tile, given the table, writes the same tile as the full run.

        Float32 tiles, where the difference between the lake's own level and the
        merged level is visible at the centimeter.
        """
        src_a, src_b = geotiff_tiles(os.path.join(tmp_dir, "in"))
        table_path = os.path.join(tmp_dir, "levels.csv")
        _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out_full"), export_water_levels=table_path)

        # Tile A: its own part of the lake is the higher one, so the table lowers it.
        only_a = os.path.join(tmp_dir, "only_a")
        os.makedirs(only_a)
        shutil.copy(src_a, only_a)
        result = _run(only_a, os.path.join(tmp_dir, "out_a"), water_levels=table_path)
        assert result.water_bodies[0].table_level is not None

        full = read_band(os.path.join(tmp_dir, "out_full", "N06E126_DEM.tif"))
        partial = read_band(os.path.join(tmp_dir, "out_a", "N06E126_DEM.tif"))
        assert np.array_equal(full, partial)

        alone = _alone(src_a, os.path.join(tmp_dir, "alone_a.tif"))
        assert not np.array_equal(alone, full), "the table made no difference, so the test proves nothing"
        assert alone[LAKE_ROWS, 90:][0, 0] > full[LAKE_ROWS, 90:][0, 0]

    def test_import_never_raises_above_local_min(self, tmp_dir, log_lines):
        src_a, src_b = geotiff_tiles(os.path.join(tmp_dir, "in"))
        only_b = os.path.join(tmp_dir, "only_b")
        os.makedirs(only_b)
        shutil.copy(src_b, only_b)
        result = _run(only_b, os.path.join(tmp_dir, "out_alone"), export_water_levels=os.path.join(tmp_dir, "own.csv"))
        own_level = result.water_bodies[0].level

        table_path = os.path.join(tmp_dir, "levels.csv")
        for body in result.water_bodies:
            body.level = own_level + 2.0
        write_water_levels(table_path, result.water_bodies, "EGM2008", "EGM96")

        result = _run(only_b, os.path.join(tmp_dir, "out_table"), water_levels=table_path)
        assert result.water_bodies[0].level == own_level
        assert any("stale" in note for note in result.water_bodies[0].notes)
        assert any("stale" in line for line in log_lines)

    def test_import_rejects_datum_mismatch(self, tmp_dir, log_lines):
        dted_tiles(os.path.join(tmp_dir, "in"))
        table_path = os.path.join(tmp_dir, "levels.csv")
        write_water_levels(table_path, [], "EGM96", "EGM2008")
        result = _run(os.path.join(tmp_dir, "in"), os.path.join(tmp_dir, "out"), water_levels=table_path)
        assert result.exit_code == 1 and result.files_processed == 0
        assert not os.path.exists(os.path.join(tmp_dir, "out", "n06e126.dt0")), "nothing may be copied"
        assert any("is for EGM96 to EGM2008" in line for line in log_lines)


class TestBatchChecks:
    def test_dted_with_other_algorithm_is_refused_before_copying(self, tmp_dir, log_lines, monkeypatch):
        from egmtrans import cli

        monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
        folder = os.path.join(tmp_dir, "in")
        os.makedirs(folder)
        write_dted(os.path.join(folder, "n03e008.dt0"), np.full((POSTS, POSTS), 40, dtype=np.int16), 8, 3)
        result = _run(folder, os.path.join(tmp_dir, "out"), algorithm="spline")
        assert result.exit_code == 1
        assert not os.path.exists(os.path.join(tmp_dir, "out", "n03e008.dt0"))
        assert any("DTED output requires the bilinear algorithm" in line for line in log_lines)

    def test_no_dem_is_an_error(self, tmp_dir, log_lines, monkeypatch):
        from egmtrans import cli

        monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
        folder = os.path.join(tmp_dir, "in")
        os.makedirs(folder)
        with open(os.path.join(folder, "readme.txt"), "w") as f:
            f.write("no tiles here")
        result = _run(folder, os.path.join(tmp_dir, "out"))
        assert result.exit_code == 1 and result.files_processed == 0
