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

With a DTED level, every GeoTIFF of the run becomes one work unit per whole
cell it covers (:func:`plan_units`), named by the ``--dted-naming`` template,
and nothing of the input tree is copied; problems found while planning stop
the run before anything is written, and the shared posts of the DTED outputs
are compared after the run (:func:`~egmtrans.tiling.compare_seams`).
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
from egmtrans.config import DATUM_MAPPING, DTED_EXTENSIONS
from egmtrans.dted.header import CellGeometry, DtedHeader, read_header
from egmtrans.dted.writer import DtedMetadataSource, HeaderAssemblyError, header_plan_lines
from egmtrans.file_utils import IOPaths, copy_folder_structure, dted_output_name, find_dems, is_valid_dem
from egmtrans.flattening import DEFAULT_CONTAINMENT
from egmtrans.io import new_dted_header, preview_dted_header
from egmtrans.tiling import (
    SeamCheck,
    TableRow,
    TileAnalysis,
    TileLevels,
    WaterBody,
    WaterLevelTable,
    compare_seams,
    connected_tiles,
    find_seams,
    format_boundary_report,
    format_seam_report,
    merge_patches,
)
from egmtrans.transform import _make_temp_dir, _remove_temp_dir, analyze_tile, source_grid, tile_geometry

TABLE_COLUMNS = ('height_m', 'level_m', 'posts', 'tiles', 'side', 'line', 'start', 'end')


@dataclass(frozen=True)
class WorkUnit:
    """One output of a run: an input file, where its output goes (None for a
    context tile), and the DTED cell to make from it (None for a transform)."""

    input_file: str
    output_file: str | None
    cell: CellGeometry | None = None

    @property
    def name(self) -> str:
        base = os.path.basename(self.input_file)
        return f'{base} [{self.cell.cell_id}]' if self.cell is not None else base

    @property
    def is_context(self) -> bool:
        return self.output_file is None


@dataclass
class BatchResult:
    """What a batch run did."""

    exit_code: int = 0
    files_processed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    water_bodies: list[WaterBody] = field(default_factory=list)
    seams: int = 0
    tiles: list[TileAnalysis] = field(default_factory=list)
    units: list[WorkUnit] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    seam_checks: list[SeamCheck] = field(default_factory=list)


def plan_units(
    inputs: list[tuple[str, str | None]],
    dted_level: int | None,
    naming: str,
    input_root: str,
    output_root: str,
) -> list[WorkUnit]:
    """The work units of *inputs* (file, output or None for context).

    Without a level every input is one unit. With a level, a DTED input keeps
    its level and is one unit; a GeoTIFF becomes one unit per whole cell it
    covers, named by *naming* under *output_root*.

    Raises:
        ValueError: If a GeoTIFF cannot become DTED, covers no whole cell, or
            the naming template is invalid.
    """
    units: list[WorkUnit] = []
    for input_file, output_file in inputs:
        if dted_level is None or input_file.lower().endswith(DTED_EXTENSIONS):
            units.append(WorkUnit(input_file, output_file, None))
            continue
        grid = source_grid(input_file)
        cells = grid.cells()
        if not cells:
            raise ValueError(
                f'{os.path.basename(input_file)} holds no whole one-degree cell with posts on all four of '
                f'its edges, so no DTED cell can be made from it'
            )
        for lon0, lat0 in cells:
            cell = CellGeometry(dted_level, lon0, lat0)
            output = None
            if output_file is not None:
                output = os.path.join(
                    output_root, dted_output_name(naming, input_file, input_root, cell.cell_id, dted_level)
                )
            units.append(WorkUnit(input_file, output, cell))
    return units


def _mask_name(output_file: str) -> str:
    return os.path.join(os.path.dirname(output_file), f'{os.path.splitext(os.path.basename(output_file))[0]}_mask.tif')


def check_units(units: list[WorkUnit], create_mask: bool) -> list[str]:
    """Problems with a plan that must stop the run: output names used twice,
    an output that is also an input, two inputs for one cell."""
    problems: list[str] = []
    seen: dict[str, WorkUnit] = {}
    inputs = {os.path.normcase(os.path.abspath(u.input_file)) for u in units}
    for unit in units:
        if unit.output_file is None:
            continue
        names = [unit.output_file] + ([_mask_name(unit.output_file)] if create_mask else [])
        for name in names:
            key = os.path.normcase(os.path.abspath(name))
            if key in inputs:
                problems.append(f'{unit.name} would overwrite an input file: {name}')
            other = seen.get(key)
            if other is not None and other is not unit:
                hint = ''
                if other.input_file == unit.input_file and unit.cell is not None:
                    hint = ' (a raster that covers several cells needs --dted-naming cell or a template with {cell})'
                problems.append(f'{unit.name} and {other.name} would both be written to {name}{hint}')
            seen[key] = unit
    cells: dict[str, WorkUnit] = {}
    for unit in units:
        if unit.cell is None or unit.output_file is None:
            continue
        other = cells.get(unit.cell.cell_id)
        if other is not None and other.input_file != unit.input_file:
            problems.append(
                f'cell {unit.cell.cell_id} is covered by both {os.path.basename(other.input_file)} and '
                f'{os.path.basename(unit.input_file)}'
            )
        cells.setdefault(unit.cell.cell_id, unit)
    return problems


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
    if not path:
        return
    for candidate in (path, path + '.aux.xml'):
        if os.path.isfile(candidate):
            try:
                os.remove(candidate)
            except OSError as e:
                _state.get_logger().warning(f'Could not remove {candidate}: {e}')


def _header_datum(path: str) -> str | None:
    """The datum a DTED file's header names, as EGMTrans spells it."""
    try:
        code = read_header(path).stripped('dsi.vertical_datum')
    except (OSError, ValueError):
        return None
    for datum, info in DATUM_MAPPING.items():
        if info.get('dted_code') == code:
            return datum
    return 'EGM96' if code == 'MSL' else None


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
    dted_metadata: DtedMetadataSource | None = None,
    dted_level: int | None = None,
    dted_naming: str = 'stem',
) -> BatchResult:
    """Transform every DEM of a run with water bodies leveled across tiles.

    *paths* names a folder of DEMs (transformed into the output folder, the
    tree copied first as before) or a single file.  *context_folders* are
    searched for DEMs the same way, and every tile found that adjoins the run
    (directly or through other context tiles) is analyzed but not written.
    The prompts a run can need (the header datum disagreeing with
    *source_datum*, source and target the same) are asked once, before
    anything is copied.

    With *dted_level*, every GeoTIFF of the run becomes DTED of that level,
    one file per whole cell it covers, named by *dted_naming* (a preset or a
    template, see :data:`~egmtrans.file_utils.DTED_NAMING_PRESETS`); DTED
    inputs keep their level, nothing of the input tree is copied, and the
    shared posts of the DTED outputs are compared after the run.

    A tile whose analysis or transform fails is reported, its untransformed
    copy is removed from the output tree, and the run goes on; the exit code
    is then 1.  The report of water bodies that touch an edge with no
    neighbor in the run is logged after the merge, so it exists even if a
    later transform fails.

    *dted_metadata* (the index and profile for DTED headers) is checked
    against every DTED output's cell before anything is copied: a cell the
    index does not hold, or a header that cannot be completed, ends the run.

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
    converting = dted_level is not None

    if paths.mode == 'folder':
        run_files = find_dems(paths.input_path)
        output_for = {
            f: os.path.join(paths.output_path, os.path.relpath(f, paths.input_path)) for f in run_files
        }
        output_dir = paths.output_path
        input_root = paths.input_path
    else:
        run_files = [paths.input_path] if is_valid_dem(paths.input_path) else []
        output_for = {paths.input_path: paths.output_path}
        output_dir = os.path.dirname(os.path.abspath(paths.output_path))
        input_root = os.path.dirname(os.path.abspath(paths.input_path))

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
    if converting:
        # A DTED context tile in another datum is a finished product, not a source.
        kept = []
        for f in context_files:
            if f.lower().endswith(DTED_EXTENSIONS) and _header_datum(f) not in (None, source_datum):
                logger.warning(f'Context tile {os.path.basename(f)} is not in {source_datum}; it is left out.')
                continue
            kept.append(f)
        context_files = kept

    # Checks that end the run happen before anything is copied or written.
    try:
        if paths.mode == 'file' and converting and not paths.output_derived:
            # A single file goes to the one output name the caller gave.
            units = plan_units([(run_files[0], None)], dted_level, dted_naming, input_root, output_dir)
            if len(units) != 1:
                raise ValueError(
                    f'{os.path.basename(run_files[0])} covers {len(units)} cells; give a folder as the output '
                    f'and name the cells with --dted-naming'
                )
            units = [WorkUnit(units[0].input_file, output_for[run_files[0]], units[0].cell)]
        else:
            units = plan_units(
                [(f, output_for[f]) for f in run_files], dted_level, dted_naming, input_root, output_dir
            )
    except ValueError as e:
        logger.error(f'{e}\nAborting transformation.')
        result.exit_code = 1
        return result
    context_units = []
    for f in context_files:
        try:
            context_units.extend(plan_units([(f, None)], dted_level, dted_naming, input_root, output_dir))
        except ValueError as e:
            # A context tile that cannot be analyzed is left out, not fatal.
            logger.warning(f'Context tile left out: {e}')
    result.units = units
    problems = check_units(units, create_mask)
    if problems:
        for problem in problems:
            logger.error(problem)
        logger.error('Aborting transformation.')
        result.exit_code = 1
        return result

    dted_units = [u for u in units if u.output_file.lower().endswith(DTED_EXTENSIONS)]
    if algorithm != 'bilinear':
        if dted_units and cli.DTED_REQUIRES_BILINEAR:
            logger.error(
                f"DTED output requires the bilinear algorithm; {len(dted_units)} of the {len(units)} outputs "
                f"would be written as DTED with '{algorithm}'.\nAborting transformation."
            )
            result.exit_code = 1
            return result
        logger.warning(
            f"The '{algorithm}' algorithm is not independent of the tile extent, or differs from bilinear by "
            f"millimeters: tiles transformed with it will not match bilinear tiles at shared posts, and a "
            f"water body's level may differ at a seam."
        )

    if dted_units:
        ok, plan = _check_dted_headers(dted_units, dted_metadata, target_datum, abs_horiz_accuracy, dted_level)
        if not ok:
            result.exit_code = 1
            return result
        if plan is not None:
            source = dted_metadata if dted_metadata is not None and not dted_metadata.empty else None
            describe = source.describe() if source is not None else 'no index or profile'
            for line in header_plan_lines(
                describe, plan.cell_id, plan.unit.input_file, plan.unit.output_file, plan.header, plan.sources,
                base=plan.base, more=len(dted_units) - 1,
            ):
                logger.info(line)
            if not arc_mode and not cli.confirm('Write the DTED headers as planned?', assume_yes):
                logger.error('Aborting transformation.')
                result.exit_code = 1
                return result

    table = None
    if water_levels:
        try:
            table = read_water_levels(water_levels, source_datum, target_datum)
        except (OSError, ValueError) as e:
            logger.error(f'{e}\nAborting transformation.')
            result.exit_code = 1
            return result
        logger.info(f'Read {len(table.rows)} edge crossing(s) from the water-level table {water_levels}.')

    first = units[0]
    if not cli.check_file_datum(first.input_file, first.output_file, source_datum, arc_mode, assume_yes):
        result.exit_code = 1
        return result
    # With the datums the same, a DTED-to-DTED unit has nothing to do and
    # refuses; a conversion resamples and flattens all the same.
    datum_check = next((u for u in units if u.cell is None), first)
    if not cli.check_same_datum(
        datum_check.input_file, source_datum, target_datum, arc_mode, assume_yes,
        converting=datum_check.cell is not None,
    ):
        result.exit_code = 1
        return result

    if paths.mode == 'folder' and not converting:
        copy_folder_structure(paths.input_path, paths.output_path)
    for unit in units:
        os.makedirs(os.path.dirname(os.path.abspath(unit.output_file)), exist_ok=True)

    needs_merge = (
        flatten
        and 'WGS84' not in (source_datum, target_datum)
        and (source_datum != target_datum or converting)
        and (len(units) + len(context_units) > 1 or table is not None or export_water_levels is not None)
    )

    tiles: list[TileAnalysis] = []
    levels: dict[int, TileLevels] = {}
    seams = []
    temp_root = None
    all_units = units + context_units
    try:
        if needs_merge:
            temp_root = _make_temp_dir(output_dir)
            logger.info(
                f'Pass 1: analyzing {len(units)} tile(s)'
                + (f' and {len(context_units)} context tile(s)' if context_units else '')
                + ' for water bodies that cross tile edges...'
            )
            for tile_id, unit in enumerate(all_units):
                tile_temp = os.path.join(temp_root, f'tile_{tile_id}')
                os.makedirs(tile_temp, exist_ok=True)
                logger.info(f'Analyzing {tile_id + 1}/{len(all_units)}: {unit.name}')
                try:
                    tile = analyze_tile(
                        unit.input_file, source_datum, target_datum, algorithm, min_patch_size,
                        tile_temp, tile_id, unit.output_file, cell=unit.cell,
                    )
                except Exception as e:
                    logger.error(f'Could not analyze {unit.name}: {e}')
                    tile = _failed_tile(tile_id, unit.input_file, unit.output_file, str(e))
                    result.failed.append((unit.name, f'analysis failed: {e}'))
                    result.exit_code = 1
                    if unit.output_file is not None and not converting:
                        _remove_if_present(unit.output_file)
                finally:
                    _remove_temp_dir(tile_temp)
                tiles.append(tile)

            seams = find_seams(tiles)
            levels, bodies = merge_patches(tiles, seams, table, min_containment)
            for tile in tiles:
                if tile.error is None:
                    levels.setdefault(tile.tile_id, TileLevels()).array_crc = tile.array_crc
            result.seams = len(seams)
            result.water_bodies = bodies
            result.tiles = tiles
            for line in format_boundary_report(bodies, tiles):
                logger.info(line)
            logger.info(f'\nPass 2: transforming {len(units)} tile(s)...')

        failed_ids = {t.tile_id for t in tiles if t.error is not None}
        written: dict[int, str] = {}
        for tile_id, unit in enumerate(units):
            if tile_id in failed_ids:
                continue
            logger.info(f'Processing file: {unit.output_file}')
            tile_levels = levels.get(tile_id) if needs_merge else None
            try:
                ok = cli.process_file(
                    unit.input_file, unit.output_file, source_datum, target_datum,
                    flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log,
                    check_for_wrong_datum=False, arc_mode=arc_mode, assume_yes=True,
                    tile_levels=tile_levels, min_containment=min_containment,
                    dted_metadata=dted_metadata, dted_cell=unit.cell, dted_plan=False,
                )
            except cli.NonInteractiveError:
                raise
            except Exception as e:
                logger.error(f'Error processing {unit.name}: {e}')
                ok = False
            if ok is False:
                result.failed.append((unit.name, 'transformation failed'))
                result.exit_code = 1
                if paths.mode == 'folder' and not converting:
                    _remove_if_present(unit.output_file)
                continue
            result.files_processed += 1
            result.outputs.append(unit.output_file)
            written[tile_id] = unit.output_file
    finally:
        _remove_temp_dir(temp_root)

    if seams and len(written) > 1:
        try:
            result.seam_checks = compare_seams(tiles, seams, written)
        except Exception as e:
            logger.warning(f'The seam check of the DTED outputs failed: {e}')
        for line in format_seam_report(result.seam_checks):
            (logger.warning if line.startswith('  Seam') else logger.info)(line)

    if export_water_levels:
        count = write_water_levels(export_water_levels, result.water_bodies, source_datum, target_datum)
        water = sum(1 for body in result.water_bodies if body.water)
        logger.info(f'Wrote {count} edge crossing(s) of {water} water bod(ies) to {export_water_levels}')

    elapsed = time.time() - start_time
    logger.info(
        f'Transformed {result.files_processed} of {len(units)} DEM(s) in '
        f'{elapsed / 60:.1f} minutes.' if elapsed >= 60 else
        f'Transformed {result.files_processed} of {len(units)} DEM(s) in {elapsed:.1f} seconds.'
    )
    for name, reason in result.failed:
        logger.error(f'  failed: {name} ({reason})')
    return result


@dataclass
class HeaderPlan:
    """The first DTED output's header as the pre-flight assembled it: the
    example a run shows before it writes anything."""

    unit: WorkUnit
    cell_id: str
    header: DtedHeader
    sources: dict[str, str]
    base: DtedHeader | None = None


def _check_dted_headers(
    dted_units: list[WorkUnit],
    dted_metadata: DtedMetadataSource | None,
    target_datum: str,
    abs_horiz_accuracy: int | None,
    dted_level: int | None,
) -> tuple[bool, HeaderPlan | None]:
    """Before anything is written: the index and profile are for the level
    being written, every DTED output has its index row, every cell made from
    scratch can have its header completed, and a DTED-to-DTED rewrite with an
    index, a profile or overrides can be assembled. Returns whether the run
    may go on and the plan of its first DTED output (None when there is
    nothing to show: a DTED-to-DTED run without any of the three)."""
    logger = _state.get_logger()
    source = dted_metadata if dted_metadata is not None and not dted_metadata.empty else None
    if source is not None and dted_level is not None:
        errors = [issue for issue in source.validate(dted_level) if issue.severity == 'error']
        if errors:
            for issue in errors:
                logger.error(str(issue))
            logger.error('Aborting transformation.')
            return False, None
    missing = []
    problems = []
    plan = None
    for unit in dted_units:
        if unit.cell is not None:
            cell_id = unit.cell.cell_id
        else:
            try:
                cell_id = read_header(unit.input_file).cell_id
            except (OSError, ValueError):
                cell_id = None
        if source is not None and source.index is not None and (cell_id is None or source.index.get(cell_id) is None):
            missing.append(f'{unit.name} ({cell_id or "no readable origin"})')
            continue
        if unit.cell is not None:
            try:
                metadata = source.for_cell(cell_id) if source is not None else None
                header, sources = new_dted_header(unit.cell, target_datum, abs_horiz_accuracy, metadata=metadata)
            except (HeaderAssemblyError, LookupError, ValueError) as e:
                problems.append(f'{unit.name}: {e}')
                continue
            if plan is None:
                plan = HeaderPlan(unit, cell_id, header, sources)
        elif source is not None and plan is None:
            try:
                base, cell, header, sources = preview_dted_header(
                    unit.input_file, target_datum, abs_horiz_accuracy, source=source,
                )
            except (OSError, HeaderAssemblyError, LookupError, ValueError) as e:
                problems.append(f'{unit.name}: {e}')
                continue
            plan = HeaderPlan(unit, cell.cell_id, header, sources, base)
    if missing:
        logger.error(
            f"The DTED metadata index has no row for {len(missing)} of the {len(dted_units)} DTED output(s): "
            f"{', '.join(missing[:5])}{', ...' if len(missing) > 5 else ''}.\nAborting transformation."
        )
        return False, None
    if problems:
        for problem in problems[:5]:
            logger.error(f'The DTED header cannot be completed for {problem}')
        if len(problems) > 5:
            logger.error(f'... and {len(problems) - 5} more')
        logger.error(
            'A DTED cell made from GeoTIFF needs --dted-profile and/or --dted-index to fill its header.\n'
            'Aborting transformation.'
        )
        return False, None
    return True, plan
