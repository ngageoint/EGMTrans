"""Integration tests for egmtrans.batch: water bodies leveled across tiles.

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
        write_dted(os.path.join(folder, "n06e126.dt0"), a, 126, 6, datum_code="E08"),
        write_dted(os.path.join(folder, "n06e127.dt0"), b, 127, 6, datum_code="E08"),
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

    def test_context_folder_is_analyzed_but_not_written(self, tmp_dir):
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
        write_dted(os.path.join(tmp_dir, "context", "n10e140.dt0"), far, 140, 10, datum_code="E08")

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


# ---------------------------------------------------------------------------
# DTED cells made from GeoTIFF
# ---------------------------------------------------------------------------

CELL_PER_DEGREE = 300   # source posts per degree of the lattice tiles
CELL_LON0, CELL_LAT0 = 30, 85   # zone V: a level-2 cell is 3601 x 601 posts


def _profile(folder, level=2):
    """A profile file for the fictional product, at *level*."""
    path = os.path.join(folder, f'profile_{level}.toml')
    with open(path, 'w') as handle:
        handle.write(
            f'schema = 1\n[product]\ndted_level = {level}\nsecurity_code = "U"\ndata_edition = 1\n'
            f'match_merge_version = "A"\nproducer_code = "USNGA"\ncompilation_date = "2026-01"\n'
            f'abs_horiz_acc = 10\nabs_vert_acc = 5\nrel_horiz_acc = "NA"\nrel_vert_acc = 3\n'
        )
    return path


def lattice_tiles(folder, cells=((0, 0),), lake_across=False):
    """Lattice tiles of one cell each at (CELL_LON0 + dx, CELL_LAT0 + dy), with a
    lake at 350 m that, when *lake_across*, spans the shared column of (0, 0) and (1, 0)."""
    from tests.conftest import lattice_geotransform, synthetic_cell

    os.makedirs(folder, exist_ok=True)
    paths = []
    for dx, dy in cells:
        heights = synthetic_cell(CELL_PER_DEGREE, seed_offset=100 * dx + 7 * dy, base_cm=40000)
        if lake_across and (dx, dy) == (0, 0):
            heights[100:200, 250:] = 350.0     # to the east edge
        elif lake_across and (dx, dy) == (1, 0):
            heights[100:200, :60] = 350.0      # from the west edge
        else:
            heights[100:200, 120:220] = 350.0
        heights[200, 170] = 349.6
        heights[201, 170] = 350.5
        name = f'tile_{CELL_LAT0 + dy}_{CELL_LON0 + dx}.tif'
        paths.append(write_geotiff(
            os.path.join(folder, name), heights,
            lattice_geotransform(CELL_LON0 + dx, CELL_LAT0 + dy, CELL_PER_DEGREE, CELL_PER_DEGREE), nodata=-32767.0,
        ))
    return paths


def _convert(input_folder, output_folder, profile, level=2, naming='stem', **kwargs):
    from egmtrans.dted.writer import DtedMetadataSource

    paths = resolve_io_paths(input_folder, output_folder, dted_level=level)
    options = dict(flatten=True, create_mask=False, min_patch_size=400, algorithm='bilinear', assume_yes=True,
                   dted_metadata=DtedMetadataSource.load(None, profile), dted_level=level, dted_naming=naming)
    options.update(kwargs)
    return run_batch(paths, 'EGM96', 'EGM96', **options)


def _sha(path):
    import hashlib

    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _shifted(input_folder, output_folder, profile, context=(), table=None, export=None):
    """A conversion batch from EGM2008 to EGM96 with the cell naming preset."""
    from egmtrans.dted.writer import DtedMetadataSource

    return run_batch(
        resolve_io_paths(input_folder, output_folder, dted_level=2), 'EGM2008', 'EGM96', True, False, 400,
        'bilinear', assume_yes=True, dted_metadata=DtedMetadataSource.load(None, profile), dted_level=2,
        dted_naming='cell', context_folders=context, water_levels=table, export_water_levels=export,
    )


@requires_grids
class TestConvertedCells:
    def test_naming_presets_and_template_without_a_tree_copy(self, tmp_dir):
        src = os.path.join(tmp_dir, 'in', 'sub')
        lattice_tiles(src)
        with open(os.path.join(tmp_dir, 'in', 'readme.txt'), 'w') as handle:
            handle.write('not a DEM')
        profile = _profile(tmp_dir)
        expected = {
            'stem': 'sub/tile_85_30.dt2', 'cell': 'N85E030.dt2', 'dted': 'DTED/E030/N85.dt2',
            'DTED{level}_{lon}{lat}': 'DTED2_E030N85.dt2',
        }
        for naming, name in expected.items():
            out = os.path.join(tmp_dir, 'out_' + naming.replace('{', '').replace('}', '').replace('-', '_'))
            result = _convert(os.path.join(tmp_dir, 'in'), out, profile, naming=naming, create_mask=True)
            assert result.exit_code == 0 and result.files_processed == 1, naming
            assert os.path.isfile(os.path.join(out, name)), naming
            # The mask sits beside its cell under the source tile's name, unique outside the tree.
            assert os.path.isfile(os.path.join(out, os.path.dirname(name), 'tile_85_30_mask.tif')), naming
            assert not os.path.exists(os.path.join(out, 'readme.txt')), 'the input tree was copied'
            assert not os.path.exists(os.path.join(out, 'sub', 'tile_85_30.tif'))
        hashes = {_sha(os.path.join(tmp_dir, folder, name)) for folder, name in (
            ('out_stem', 'sub/tile_85_30.dt2'), ('out_cell', 'N85E030.dt2'), ('out_dted', 'DTED/E030/N85.dt2'))}
        assert len(hashes) == 1, 'the name changed the bytes'
        # The default is the standard tree, and an output folder named DTED gets no second root.
        from egmtrans.file_utils import DEFAULT_DTED_NAMING

        assert DEFAULT_DTED_NAMING == 'dted'
        default_out = os.path.join(tmp_dir, 'out_default')
        result = _convert(os.path.join(tmp_dir, 'in'), default_out, profile, naming='dted')
        assert result.exit_code == 0 and os.path.isfile(os.path.join(default_out, 'DTED', 'E030', 'N85.dt2'))
        dted_out = os.path.join(tmp_dir, 'delivery', 'DTED')
        result = _convert(os.path.join(tmp_dir, 'in'), dted_out, profile, naming='dted')
        assert result.exit_code == 0 and os.path.isfile(os.path.join(dted_out, 'E030', 'N85.dt2'))
        assert not os.path.exists(os.path.join(tmp_dir, 'delivery', 'DTED', 'DTED'))

    def test_collisions_and_duplicate_cells_stop_the_run(self, tmp_dir, log_lines):
        from tests.conftest import lattice_geotransform, synthetic_cell

        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder)
        # A second raster covering the same cell.
        shutil.copy(os.path.join(folder, 'tile_85_30.tif'), os.path.join(folder, 'again.tif'))
        result = _convert(folder, os.path.join(tmp_dir, 'out'), _profile(tmp_dir), naming='cell')
        assert result.exit_code == 1 and not os.path.exists(os.path.join(tmp_dir, 'out', 'N85E030.dt2'))
        assert any('both be written to' in line for line in log_lines)
        os.remove(os.path.join(folder, 'again.tif'))
        # A raster of two cells under stem naming.
        wide = np.concatenate([synthetic_cell(CELL_PER_DEGREE), synthetic_cell(CELL_PER_DEGREE)[:, 1:]], axis=1)
        write_geotiff(os.path.join(folder, 'wide.tif'), wide,
                      lattice_geotransform(CELL_LON0 + 2, CELL_LAT0, CELL_PER_DEGREE, CELL_PER_DEGREE, extent_x=2))
        result = _convert(folder, os.path.join(tmp_dir, 'out2'), _profile(tmp_dir))
        assert result.exit_code == 1
        assert any('--dted-naming cell' in line for line in log_lines)
        assert not os.path.exists(os.path.join(tmp_dir, 'out2'))

    def test_a_wide_raster_gives_two_cells_that_share_a_column(self, tmp_dir):
        from egmtrans.dted.records import read_records
        from tests.conftest import lattice_geotransform, synthetic_cell

        folder = os.path.join(tmp_dir, 'in')
        os.makedirs(folder)
        left = synthetic_cell(CELL_PER_DEGREE, base_cm=40000)
        right = synthetic_cell(CELL_PER_DEGREE, seed_offset=1, base_cm=40000)
        right[:, 0] = left[:, -1]
        left[100:200, 250:] = 350.0
        right[100:200, :60] = 350.0
        wide = np.concatenate([left, right[:, 1:]], axis=1)
        write_geotiff(os.path.join(folder, 'wide.tif'), wide,
                      lattice_geotransform(CELL_LON0, CELL_LAT0, CELL_PER_DEGREE, CELL_PER_DEGREE, extent_x=2))
        out = os.path.join(tmp_dir, 'out')
        result = _shifted(folder, out, _profile(tmp_dir))
        assert result.exit_code == 0 and result.files_processed == 2 and len(result.units) == 2
        a = read_records(os.path.join(out, 'N85E030.dt2')).values
        b = read_records(os.path.join(out, 'N85E031.dt2')).values
        assert np.array_equal(a[:, -1], b[:, 0]), 'the shared column differs'
        lake_a, lake_b = np.unique(a[1200:2389, 500:]), np.unique(b[1200:2389, :119])
        assert lake_a.size == 1 and lake_b.size == 1 and lake_a[0] == lake_b[0]
        assert result.seam_checks and all(check.clean for check in result.seam_checks)

    def test_band_boundary_pair_shares_its_row_and_the_seam_report_sees_a_bad_copy(self, tmp_dir, log_lines):
        from egmtrans.dted.records import read_edges
        from egmtrans.dted.selftest import COARSE, FINE, LAT0_SOUTH, LON0, northern_tile, southern_tile, write_tile

        folder = os.path.join(tmp_dir, 'in')
        os.makedirs(folder)
        north = northern_tile()
        south = southern_tile(north)
        write_tile(os.path.join(folder, 'south.tif'), south, LON0, LAT0_SOUTH, FINE, FINE)
        write_tile(os.path.join(folder, 'north.tif'), north, LON0, LAT0_SOUTH + 1, COARSE, FINE)
        out = os.path.join(tmp_dir, 'out')
        result = _convert(folder, out, _profile(tmp_dir), naming='cell')
        assert result.exit_code == 0 and result.files_processed == 2
        assert len(result.seam_checks) == 1 and result.seam_checks[0].clean
        assert result.seam_checks[0].shared == 1801
        south_edge = read_edges(os.path.join(out, 'N49E006.dt2'))['north']
        north_edge = read_edges(os.path.join(out, 'N50E006.dt2'))['south']
        assert np.array_equal(south_edge[::2], north_edge)
        assert any('1 seam(s), 1,801 shared posts, 0 seam(s) with posts that differ' in line for line in log_lines)

        # An edge row that is not a copy of the coarser row is resampled as it is, and the report says so.
        south[0] = north[-1][np.arange(FINE + 1) * COARSE // FINE]  # a nearest-neighbor copy rounded down: not the rule
        south[0, 1] += 1.0
        write_tile(os.path.join(folder, 'south.tif'), south, LON0, LAT0_SOUTH, FINE, FINE)
        log_lines.clear()
        result = _convert(folder, os.path.join(tmp_dir, 'out2'), _profile(tmp_dir), naming='cell')
        assert result.exit_code == 0
        assert len(result.seam_checks) == 1 and not result.seam_checks[0].clean
        assert any(line.startswith('  Seam ') and 'differ' in line for line in log_lines)

    def test_context_cells_and_a_table_reproduce_the_full_run(self, tmp_dir):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder, cells=((0, 0), (1, 0)), lake_across=True)
        profile = _profile(tmp_dir)
        full = _shifted(folder, os.path.join(tmp_dir, 'full'), profile, export=os.path.join(tmp_dir, 'levels.csv'))
        assert full.exit_code == 0 and full.files_processed == 2
        reference = _sha(os.path.join(tmp_dir, 'full', 'N85E030.dt2'))

        west = os.path.join(tmp_dir, 'west')
        os.makedirs(west)
        shutil.copy(os.path.join(folder, 'tile_85_30.tif'), west)
        with_context = _shifted(west, os.path.join(tmp_dir, 'ctx'), profile, context=[folder])
        assert with_context.exit_code == 0
        assert _sha(os.path.join(tmp_dir, 'ctx', 'N85E030.dt2')) == reference
        table = os.path.join(tmp_dir, 'levels.csv')
        with_table = _shifted(west, os.path.join(tmp_dir, 'tab'), profile, table=table)
        assert with_table.exit_code == 0
        assert _sha(os.path.join(tmp_dir, 'tab', 'N85E030.dt2')) == reference
        # Alone, the lake's level is the minimum over its western part only.
        from egmtrans.dted.records import read_records

        alone = _shifted(west, os.path.join(tmp_dir, 'alone'), profile)
        assert alone.exit_code == 0
        assert len(full.water_bodies) == 1 and full.water_bodies[0].tile_ids == [0, 1]
        level_full = read_records(os.path.join(tmp_dir, 'full', 'N85E030.dt2')).values[1800, 550]
        level_alone = read_records(os.path.join(tmp_dir, 'alone', 'N85E030.dt2')).values[1800, 550]
        assert level_alone >= level_full

    def test_tile_order_does_not_change_the_bytes(self, tmp_dir):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder, cells=((0, 0), (1, 0)), lake_across=True)
        profile = _profile(tmp_dir)
        first = _shifted(folder, os.path.join(tmp_dir, 'first'), profile)
        reversed_folder = os.path.join(tmp_dir, 'reversed')
        os.makedirs(reversed_folder)
        shutil.copy(os.path.join(folder, 'tile_85_30.tif'), os.path.join(reversed_folder, 'z_east_first.tif'))
        shutil.copy(os.path.join(folder, 'tile_85_31.tif'), os.path.join(reversed_folder, 'a_west_last.tif'))
        second = _shifted(reversed_folder, os.path.join(tmp_dir, 'second'), profile)
        assert first.exit_code == 0 and second.exit_code == 0
        for name in ('N85E030.dt2', 'N85E031.dt2'):
            assert _sha(os.path.join(tmp_dir, 'first', name)) == _sha(os.path.join(tmp_dir, 'second', name))

    def test_a_failed_cell_leaves_nothing_and_the_run_goes_on(self, tmp_dir, monkeypatch):
        from egmtrans import io as egm_io

        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder, cells=((0, 0), (1, 0)))
        original = egm_io._verify_dted

        def flaky(path, cell, posts, **kwargs):
            if cell.cell_id == 'N85E031':
                raise RuntimeError('verification failed on purpose')
            original(path, cell, posts, **kwargs)

        monkeypatch.setattr(egm_io, '_verify_dted', flaky)
        out = os.path.join(tmp_dir, 'out')
        result = _convert(folder, out, _profile(tmp_dir), naming='cell', create_mask=True)
        assert result.exit_code == 1 and result.files_processed == 1
        assert sorted(os.listdir(out)) == ['N85E030.dt2', 'tile_85_30_mask.tif']
        assert result.failed == [('tile_85_31.tif [N85E031]', 'transformation failed')]

    def test_header_problems_stop_the_run_before_anything_is_written(self, tmp_dir, log_lines):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder)
        out = os.path.join(tmp_dir, 'out')
        result = _convert(folder, out, _profile(tmp_dir, level=1))
        assert result.exit_code == 1 and not os.path.exists(out)
        assert any('profile is for level 1, not 2' in line for line in log_lines)
        from egmtrans.dted.writer import DtedMetadataSource

        result = run_batch(resolve_io_paths(folder, out, dted_level=2), 'EGM96', 'EGM96', True, False, 400,
                           'bilinear', assume_yes=True, dted_metadata=DtedMetadataSource(), dted_level=2)
        assert result.exit_code == 1 and not os.path.exists(out)
        assert any('needs --dted-profile and/or --dted-index' in line for line in log_lines)


def test_folder_within_tells_nested_folders_apart(tmp_dir):
    from egmtrans.file_utils import folder_within

    folder = os.path.join(tmp_dir, 'data')
    os.makedirs(os.path.join(folder, 'sub'))
    assert folder_within(folder, folder)
    assert folder_within(os.path.join(folder, 'sub'), folder)
    assert not folder_within(folder, os.path.join(folder, 'sub'))
    assert not folder_within(os.path.join(tmp_dir, 'out'), folder)


@requires_grids
class TestHeaderPlan:
    def test_the_plan_is_shown_once_and_a_no_ends_the_run(self, tmp_dir, monkeypatch, log_lines):
        src = os.path.join(tmp_dir, 'in')
        lattice_tiles(src)
        profile = _profile(tmp_dir)
        out = os.path.join(tmp_dir, 'out')
        monkeypatch.setattr('builtins.input', lambda *_: 'no')
        result = _convert(src, out, profile, assume_yes=False)
        assert result.exit_code == 1 and result.files_processed == 0
        written = [name for _, _, files in os.walk(out) for name in files] if os.path.isdir(out) else []
        assert not any(name.endswith('.dt2') for name in written)
        assert log_lines.count('DTED header plan') == 1
        assert '  example: cell N85E030, tile_85_30.tif -> tile_85_30.dt2' in log_lines
        assert "    dsi.producer_code: 'USNGA   ' (profile)" in log_lines

        monkeypatch.setattr('builtins.input', lambda *_: 'yes')
        result = _convert(src, out, profile, assume_yes=False)
        assert result.exit_code == 0 and result.files_processed == 1
        assert os.path.isfile(os.path.join(out, 'tile_85_30.dt2'))
        assert log_lines.count('DTED header plan') == 2, 'the plan is shown once per run, not once per unit'


def _e08_tile(folder, name, lon0, lat0, datum_code='E08'):
    os.makedirs(folder, exist_ok=True)
    return write_dted(os.path.join(folder, name), _terrain(POSTS, 0, np.int16), lon0, lat0, datum_code=datum_code)


@requires_grids
class TestDatumChecksBeforeWriting:
    def test_every_input_is_checked_and_a_no_writes_nothing(self, tmp_dir, monkeypatch, log_lines):
        folder = os.path.join(tmp_dir, 'in')
        _e08_tile(folder, 'n06e126.dt0', 126, 6)
        _e08_tile(folder, 'n06e127.dt0', 127, 6, datum_code='E96')  # already in the target datum
        out = os.path.join(tmp_dir, 'out')
        monkeypatch.setattr('builtins.input', lambda *_: 'no')
        result = _run(folder, out, assume_yes=False)
        assert result.exit_code == 1 and result.files_processed == 0
        assert not any(name.endswith('.dt0') for _, _, files in os.walk(out) for name in files)
        assert any('1 of the 2 input(s) declare another vertical datum' in line and 'n06e127.dt0 (EGM96)' in line
                   for line in log_lines)
        monkeypatch.setattr('builtins.input', lambda *_: 'yes')
        result = _run(folder, out, assume_yes=False)
        assert result.exit_code == 0 and result.files_processed == 2

    def test_a_context_tile_in_another_datum_is_left_out(self, tmp_dir, log_lines):
        run_folder = os.path.join(tmp_dir, 'run')
        context = os.path.join(tmp_dir, 'context')
        _e08_tile(run_folder, 'n06e126.dt0', 126, 6)
        _e08_tile(context, 'n06e127.dt0', 127, 6, datum_code='E96')
        result = _run(run_folder, os.path.join(tmp_dir, 'out'), context_folders=[context])
        assert result.exit_code == 0 and result.seams == 0 and result.tiles == []
        assert any('n06e127.dt0 declares EGM96, not EGM2008; it is left out' in line for line in log_lines)
        assert any('1 DEM(s) found; 0 adjoin the run' in line for line in log_lines)

    def test_context_and_table_are_ignored_without_flattening(self, tmp_dir, log_lines):
        run_folder = os.path.join(tmp_dir, 'run')
        context = os.path.join(tmp_dir, 'context')
        _e08_tile(run_folder, 'n06e126.dt0', 126, 6)
        _e08_tile(context, 'n06e127.dt0', 127, 6)
        result = _run(run_folder, os.path.join(tmp_dir, 'out'), context_folders=[context], flatten=False,
                      water_levels=os.path.join(tmp_dir, 'no_such_table.csv'))
        assert result.exit_code == 0 and result.tiles == [] and result.files_processed == 1
        assert any('No water body is leveled because flattening is off: the context folder(s), the water-level '
                   'table are ignored' in line for line in log_lines)


class TestRerunsAndFailures:
    def test_existing_outputs_are_listed_then_skipped_on_request(self, tmp_dir, log_lines):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder, cells=((0, 0), (1, 0)))
        out = os.path.join(tmp_dir, 'out')
        profile = _profile(tmp_dir)
        first = _convert(folder, out, profile, naming='cell')
        assert first.exit_code == 0 and first.files_processed == 2
        log_lines.clear()
        second = _convert(folder, out, profile, naming='cell')
        assert second.files_processed == 2
        assert any('2 of the 2 planned output(s) already exist' in line and 'will be replaced' in line
                   for line in log_lines)
        log_lines.clear()
        os.remove(os.path.join(out, 'N85E031.dt2'))
        third = _convert(folder, out, profile, naming='cell', skip_existing=True)
        assert third.skipped_existing == 1 and third.files_processed == 1 and third.exit_code == 0
        assert sorted(os.path.basename(path) for path in third.outputs) == ['N85E030.dt2', 'N85E031.dt2']
        assert any('Skipping 1 output(s) already written' in line for line in log_lines)
        assert not any('will be replaced' in line for line in log_lines)
        fourth = _convert(folder, out, profile, naming='cell', skip_existing=True)
        assert fourth.skipped_existing == 2 and fourth.files_processed == 0 and fourth.exit_code == 0

    def test_a_failed_cell_clears_an_earlier_output(self, tmp_dir, monkeypatch, log_lines):
        from egmtrans import io as egm_io

        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder)
        out = os.path.join(tmp_dir, 'out')
        profile = _profile(tmp_dir)
        assert _convert(folder, out, profile, naming='cell', create_mask=True).exit_code == 0
        assert os.path.isfile(os.path.join(out, 'N85E030.dt2'))
        assert os.path.isfile(os.path.join(out, 'tile_85_30_mask.tif'))

        def broken(path, cell, posts, **kwargs):
            raise RuntimeError('verification failed on purpose')

        monkeypatch.setattr(egm_io, '_verify_dted', broken)
        result = _convert(folder, out, profile, naming='cell', create_mask=True)
        assert result.exit_code == 1 and result.files_processed == 0
        assert not os.path.exists(os.path.join(out, 'N85E030.dt2'))
        assert not os.path.exists(os.path.join(out, 'tile_85_30_mask.tif'))
        assert any('Removed' in line and 'failed to write anew' in line for line in log_lines)

    def test_an_unwritable_export_path_stops_the_run_before_anything(self, tmp_dir, log_lines):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder)
        out = os.path.join(tmp_dir, 'out')
        export = os.path.join(tmp_dir, 'nowhere', 'levels.csv')
        result = _convert(folder, out, _profile(tmp_dir), export_water_levels=export)
        assert result.exit_code == 2 and not os.path.exists(out)
        assert any('does not exist' in line for line in log_lines)

    def test_partial_cells_are_logged(self, tmp_dir, log_lines):
        from tests.conftest import lattice_geotransform, synthetic_cell

        folder = os.path.join(tmp_dir, 'in')
        os.makedirs(folder)
        heights = synthetic_cell(CELL_PER_DEGREE, base_cm=40000)
        wide = np.concatenate([heights, heights[:, 1:CELL_PER_DEGREE // 2 + 1]], axis=1)  # 1.5 degrees wide
        write_geotiff(os.path.join(folder, 'wide.tif'), wide,
                      lattice_geotransform(CELL_LON0, CELL_LAT0, CELL_PER_DEGREE, CELL_PER_DEGREE), nodata=-32767.0)
        result = _convert(folder, os.path.join(tmp_dir, 'out'), _profile(tmp_dir), naming='cell')
        assert result.exit_code == 0 and result.files_processed == 1
        assert any(f'Cell N85E{CELL_LON0 + 1:03d} is covered only in part by wide.tif and is skipped' in line
                   for line in log_lines)

    def test_a_stop_request_ends_the_run_between_tiles(self, tmp_dir, log_lines):
        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder, cells=((0, 0), (1, 0)))
        calls = []

        def should_stop():
            calls.append(1)
            return len(calls) > 3  # the first tile passes, the second is never started

        result = _convert(folder, os.path.join(tmp_dir, 'out'), _profile(tmp_dir), naming='cell',
                          should_stop=should_stop)
        assert result.cancelled and result.exit_code == 1 and result.files_processed == 1
        assert any('Stopped on request after 1 of 2' in line for line in log_lines)
        assert not os.path.isfile(os.path.join(tmp_dir, 'out', 'N85E031.dt2'))

    def test_too_little_free_space_stops_the_run(self, tmp_dir, monkeypatch, log_lines):
        from collections import namedtuple

        from egmtrans import batch as batch_module

        folder = os.path.join(tmp_dir, 'in')
        lattice_tiles(folder)
        usage = namedtuple('usage', 'total used free')
        monkeypatch.setattr(batch_module.shutil, 'disk_usage', lambda path: usage(10, 10, 1000))
        result = _convert(folder, os.path.join(tmp_dir, 'out'), _profile(tmp_dir))
        assert result.exit_code == 1 and result.files_processed == 0
        assert any('GB free' in line and 'planned outputs need' in line for line in log_lines)
        with_mask = batch_module.estimated_output_bytes(result.units, True)
        assert with_mask > batch_module.estimated_output_bytes(result.units, False)


def test_water_level_table_accepts_a_bom_and_names_a_spreadsheet_resave(tmp_dir):
    body = (
        '# EGMTrans water levels: one row per crossing of a water body over a tile edge\n'
        '# version: test\n# source: EGM2008\n# target: EGM96\n# created: 2026-01-01\n'
        'height_m,level_m,posts,tiles,side,line,start,end\n'
        '150.00,149.25,400,2,E,127.0,6.3,6.4\n'
    )
    path = os.path.join(tmp_dir, 'levels.csv')
    with open(path, 'w', encoding='utf-8-sig') as handle:
        handle.write(body)
    table = read_water_levels(path, 'EGM2008', 'EGM96')
    assert len(table.rows) == 1 and table.rows[0].level == 149.25
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(body.replace(',', ';'))
    with pytest.raises(ValueError, match='uses ";" as its separator'):
        read_water_levels(path, 'EGM2008', 'EGM96')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(body.replace('150.00,149.25', '"150,00";"149,25"').replace(',', ';').replace('";"', ';'))
    with pytest.raises(ValueError, match='separator'):
        read_water_levels(path, 'EGM2008', 'EGM96')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(body.replace('150.00,149.25,400', '"150,00","149,25",400'))
    with pytest.raises(ValueError, match='decimal commas'):
        read_water_levels(path, 'EGM2008', 'EGM96')
