"""The batch runner shared by the command line and the ArcGIS Pro toolbox.

A folder of tiles is transformed in two passes so that a water body spanning
several tiles gets one level everywhere:

1. Every tile of the run, and every tile of the context folders, is analyzed
   (:func:`~egmtrans.transform.analyze_tile`): labeled, its edge posts
   recorded, and, where a flat patch touches an edge, transformed once to
   record the patch's lowest level.
2. Patches are joined across the seams between tiles
   (:func:`~egmtrans.tiling.merge_patches`), each water body takes the lowest
   level over all its parts (or a lower one from an imported table), and the
   tiles of the run are transformed with those levels.

Context tiles are analyzed but never written: a producer who holds the
neighboring source tiles gets the level the neighbor's producer would compute.
A water-level table exported by a run over a larger area gives a later, partial
run the same levels whatever the order of production.
"""

from __future__ import annotations

import csv
import datetime
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from egmtrans import _state
from egmtrans._version import __version__
from egmtrans.config import DTED_EXTENSIONS
from egmtrans.file_utils import IOPaths, copy_folder_structure, find_dems, is_valid_dem
from egmtrans.flattening import DEFAULT_CONTAINMENT
from egmtrans.tiling import (
    TableRow,
    TileAnalysis,
    TileLevels,
    WaterBody,
    WaterLevelTable,
    connected_tiles,
    find_seams,
    format_boundary_report,
    merge_patches,
)
from egmtrans.transform import _make_temp_dir, _remove_temp_dir, analyze_tile, tile_geometry

TABLE_COLUMNS = ('height_m', 'level_m', 'posts', 'tiles', 'side', 'line', 'start', 'end')


@dataclass
class BatchResult:
    """What a batch run did."""

    exit_code: int = 0
    files_processed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    water_bodies: list[WaterBody] = field(default_factory=list)
    seams: int = 0
    tiles: list[TileAnalysis] = field(default_factory=list)


def read_water_levels(path: str, source_datum: str, target_datum: str) -> WaterLevelTable:
    """Read a table written by :func:`write_water_levels`.

    Raises:
        ValueError: If the table was made for another pair of datums, or is malformed.
    """
    header: dict[str, str] = {}
    rows: list[TableRow] = []
    with open(path, newline='') as f:
        lines = []
        for line in f:
            if line.startswith('#'):
                key, _, value = line[1:].partition(':')
                header[key.strip()] = value.strip()
            else:
                lines.append(line)
    if header.get('source') != source_datum or header.get('target') != target_datum:
        raise ValueError(
            f"The water-level table {path} is for {header.get('source')} to {header.get('target')}, "
            f"not {source_datum} to {target_datum}."
        )
    try:
        for record in csv.DictReader(lines):
            rows.append(TableRow(
                height_cm=int(round(float(record['height_m']) * 100)),
                level=float(record['level_m']),
                posts=int(record['posts']),
                tiles=int(record['tiles']),
                side=record['side'],
                line=float(record['line']),
                start=float(record['start']),
                end=float(record['end']),
            ))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f'The water-level table {path} is malformed: {e}') from e
    return WaterLevelTable(rows, source_datum, target_datum, header.get('version', ''))


def write_water_levels(path: str, bodies: list[WaterBody], source_datum: str, target_datum: str) -> int:
    """Write every edge crossing of every water body, one row each; return the row count.

    Coordinates and levels are written with full precision so that a run that
    imports the table reproduces the exporting run's outputs exactly.
    """
    count = 0
    with open(path, 'w', newline='') as f:
        f.write('# EGMTrans water levels: one row per crossing of a water body over a tile edge\n')
        f.write(f'# version: {__version__}\n')
        f.write(f'# source: {source_datum}\n')
        f.write(f'# target: {target_datum}\n')
        f.write(f'# created: {datetime.date.today().isoformat()}\n')
        writer = csv.writer(f)
        writer.writerow(TABLE_COLUMNS)
        for body in bodies:
            if not body.water:
                continue
            for crossing in body.crossings:
                writer.writerow([
                    f'{body.height_cm / 100:.2f}', repr(float(body.level)), body.posts, len(body.tile_ids),
                    crossing.side, repr(float(crossing.line)), repr(float(crossing.start)),
                    repr(float(crossing.end)),
                ])
                count += 1
    return count


def _failed_tile(tile_id: int, input_file: str, output_file: str | None, error: str) -> TileAnalysis:
    return TileAnalysis(
        tile_id=tile_id,
        input_file=input_file,
        output_file=output_file,
        crs_key='',
        geotransform=(0.0, 1.0, 0.0, 0.0, 0.0, -1.0),
        rows=0,
        cols=0,
        is_dted=input_file.lower().endswith(DTED_EXTENSIONS),
        error=error,
    )


def _remove_if_present(path: str | None) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            _state.get_logger().warning(f'Could not remove {path}: {e}')


def adjoining_context(run_files: list[str], context_files: list[str]) -> list[str]:
    """The context tiles that can share a water body with the run.

    Reads only the headers: a context tile is kept when a chain of seams
    connects it to a run tile, through other context tiles if need be.
    """
    logger = _state.get_logger()
    geometry: list[TileAnalysis] = []
    for tile_id, path in enumerate(run_files + context_files):
        try:
            geometry.append(tile_geometry(path, tile_id, path if tile_id < len(run_files) else None))
        except Exception as e:
            logger.warning(f'Could not read the georeferencing of {path}: {e}')
    run_ids = {t.tile_id for t in geometry if t.tile_id < len(run_files)}
    reached = connected_tiles(find_seams(geometry), run_ids)
    return [t.input_file for t in geometry if t.tile_id >= len(run_files) and t.tile_id in reached]


def run_batch(
    paths: IOPaths,
    source_datum: str,
    target_datum: str,
    flatten: bool,
    create_mask: bool,
    min_patch_size: int,
    algorithm: str,
    abs_horiz_accuracy: int | None = None,
    save_log: bool = True,
    arc_mode: bool = False,
    assume_yes: bool = False,
    context_folders: Sequence[str] = (),
    water_levels: str | None = None,
    export_water_levels: str | None = None,
    min_containment: float = DEFAULT_CONTAINMENT,
) -> BatchResult:
    """Transform every DEM of a run with water bodies levelled across tiles.

    *paths* names a folder of DEMs (transformed into the output folder, the
    tree copied first as before) or a single file.  *context_folders* are
    searched for DEMs the same way, and every tile found that adjoins the run
    (directly or through other context tiles) is analyzed but not written.
    The prompts a run can need (the header datum disagreeing with
    *source_datum*, source and target the same) are asked once, before
    anything is copied.

    A tile whose analysis or transform fails is reported, its untransformed
    copy is removed from the output tree, and the run goes on; the exit code
    is then 1.  The report of water bodies that touch an edge with no
    neighbor in the run is logged after the merge, so it exists even if a
    later transform fails.

    Raises:
        NonInteractiveError: If a prompt is needed, stdin is closed, and
            *assume_yes* is False.
    """
    # Lazy: cli imports this module. Attribute access keeps the CLI tests'
    # monkeypatching of cli.process_file and cli.verify_grids effective here.
    from egmtrans import cli

    logger = _state.get_logger()
    result = BatchResult()
    start_time = time.time()

    cli._configure_runtime(arc_mode)
    cli.verify_grids(source_datum, target_datum)

    if paths.mode == 'folder':
        run_files = find_dems(paths.input_path)
        output_for = {
            f: os.path.join(paths.output_path, os.path.relpath(f, paths.input_path)) for f in run_files
        }
        output_dir = paths.output_path
    else:
        run_files = [paths.input_path] if is_valid_dem(paths.input_path) else []
        output_for = {paths.input_path: paths.output_path}
        output_dir = os.path.dirname(os.path.abspath(paths.output_path))

    if not run_files:
        logger.error(f'No DEM to transform under {paths.input_path}.')
        result.exit_code = 1
        return result

    run_set = {os.path.abspath(f) for f in run_files}
    context_files = []
    for folder in context_folders:
        for f in find_dems(folder):
            if os.path.abspath(f) not in run_set:
                context_files.append(os.path.abspath(f))
    if context_files:
        found = len(context_files)
        context_files = adjoining_context(run_files, context_files)
        logger.info(
            f'Context: {found} DEM(s) found; {len(context_files)} adjoin the run and will be analyzed.'
        )

    # Checks that end the run happen before anything is copied.
    dted_outputs = [f for f in run_files if output_for[f].lower().endswith(DTED_EXTENSIONS)]
    if algorithm != 'bilinear':
        if dted_outputs and cli.DTED_REQUIRES_BILINEAR:
            logger.error(
                f"DTED output requires the bilinear algorithm; {len(dted_outputs)} of the {len(run_files)} DEMs "
                f"would be written as DTED with '{algorithm}'.\nAborting transformation."
            )
            result.exit_code = 1
            return result
        logger.warning(
            f"The '{algorithm}' algorithm is not independent of the tile extent, or differs from bilinear by "
            f"millimeters: tiles transformed with it will not match bilinear tiles at shared posts, and a "
            f"water body's level may differ at a seam."
        )

    table = None
    if water_levels:
        try:
            table = read_water_levels(water_levels, source_datum, target_datum)
        except (OSError, ValueError) as e:
            logger.error(f'{e}\nAborting transformation.')
            result.exit_code = 1
            return result
        logger.info(f'Read {len(table.rows)} edge crossing(s) from the water-level table {water_levels}.')

    first = run_files[0]
    if not cli.check_file_datum(first, output_for[first], source_datum, arc_mode, assume_yes):
        result.exit_code = 1
        return result
    if not cli.check_same_datum(first, source_datum, target_datum, arc_mode, assume_yes):
        result.exit_code = 1
        return result

    if paths.mode == 'folder':
        copy_folder_structure(paths.input_path, paths.output_path)

    needs_merge = (
        flatten
        and 'WGS84' not in (source_datum, target_datum)
        and source_datum != target_datum
        and (len(run_files) + len(context_files) > 1 or table is not None or export_water_levels is not None)
    )

    tiles: list[TileAnalysis] = []
    levels: dict[int, TileLevels] = {}
    temp_root = None
    try:
        if needs_merge:
            temp_root = _make_temp_dir(output_dir)
            all_files = [(f, output_for[f]) for f in run_files] + [(f, None) for f in context_files]
            logger.info(
                f'Pass 1: analyzing {len(run_files)} tile(s)'
                + (f' and {len(context_files)} context tile(s)' if context_files else '')
                + ' for water bodies that cross tile edges...'
            )
            for tile_id, (input_file, output_file) in enumerate(all_files):
                tile_temp = os.path.join(temp_root, f'tile_{tile_id}')
                os.makedirs(tile_temp, exist_ok=True)
                logger.info(f'Analyzing {tile_id + 1}/{len(all_files)}: {os.path.basename(input_file)}')
                try:
                    tile = analyze_tile(
                        input_file, source_datum, target_datum, algorithm, min_patch_size,
                        tile_temp, tile_id, output_file,
                    )
                except Exception as e:
                    logger.error(f'Could not analyze {input_file}: {e}')
                    tile = _failed_tile(tile_id, input_file, output_file, str(e))
                    result.failed.append((input_file, f'analysis failed: {e}'))
                    result.exit_code = 1
                    if output_file is not None:
                        _remove_if_present(output_file)
                finally:
                    _remove_temp_dir(tile_temp)
                tiles.append(tile)

            seams = find_seams(tiles)
            levels, bodies = merge_patches(tiles, seams, table, min_containment)
            result.seams = len(seams)
            result.water_bodies = bodies
            result.tiles = tiles
            for line in format_boundary_report(bodies, tiles):
                logger.info(line)
            logger.info(f'\nPass 2: transforming {len(run_files)} tile(s)...')

        failed_inputs = {t.input_file for t in tiles if t.error is not None}
        run_tiles = {t.input_file: t for t in tiles if t.output_file is not None}
        for input_file in run_files:
            if input_file in failed_inputs:
                continue
            output_file = output_for[input_file]
            logger.info(f'Processing file: {output_file}')
            tile = run_tiles.get(input_file)
            tile_levels = levels.get(tile.tile_id) if tile is not None else None
            try:
                ok = cli.process_file(
                    input_file, output_file, source_datum, target_datum,
                    flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log,
                    check_for_wrong_datum=False, arc_mode=arc_mode, assume_yes=True,
                    tile_levels=tile_levels, min_containment=min_containment,
                )
            except cli.NonInteractiveError:
                raise
            except Exception as e:
                logger.error(f'Error processing {input_file}: {e}')
                ok = False
            if ok is False:
                result.failed.append((input_file, 'transformation failed'))
                result.exit_code = 1
                if paths.mode == 'folder':
                    _remove_if_present(output_file)
                continue
            result.files_processed += 1
    finally:
        _remove_temp_dir(temp_root)

    if export_water_levels:
        count = write_water_levels(export_water_levels, result.water_bodies, source_datum, target_datum)
        water = sum(1 for body in result.water_bodies if body.water)
        logger.info(f'Wrote {count} edge crossing(s) of {water} water bod(ies) to {export_water_levels}')

    elapsed = time.time() - start_time
    logger.info(
        f'Transformed {result.files_processed} of {len(run_files)} DEM(s) in '
        f'{elapsed / 60:.1f} minutes.' if elapsed >= 60 else
        f'Transformed {result.files_processed} of {len(run_files)} DEM(s) in {elapsed:.1f} seconds.'
    )
    for input_file, reason in result.failed:
        logger.error(f'  failed: {input_file} ({reason})')
    return result
