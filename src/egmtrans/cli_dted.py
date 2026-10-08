"""The ``dted-header``, ``dted-index``, ``dted-selftest`` and ``dmed`` subcommands.

    egmtrans dted-header FILE... [--format text|json|csv|md] [--out PATH]
                         [--zero-based] [--check-data] [--strict]
    egmtrans dmed FOLDER [--out PATH] [--check]
    egmtrans dted-index build --out INDEX (--from-dted PATH... | --from-rasters PATH...
                         | --from-table FILE [--layer NAME] [--cell-field NAME]
                           [--map INDEX_COLUMN=TABLE_COLUMN]... [--set INDEX_COLUMN=VALUE]...
                           [--prefer TABLE_COLUMN]) [--profile FILE] [--level N] [--product NAME] [--update]
    egmtrans dted-index validate INDEX [--profile FILE] [--level N]
    egmtrans dted-selftest [--keep FOLDER] [--print-reference]

Reports go to stdout (or ``--out``), log messages to stderr, so a JSON or
CSV report can be piped. Exit codes: 0 clean, 1 findings or failures, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from egmtrans import _state
from egmtrans.dted.companions import COMPANION_EXTENSIONS
from egmtrans.dted.harvest import build_index, parse_column_map, parse_constants
from egmtrans.dted.index import HEADER_COLUMNS, DtedIndex, read_index, validate_index
from egmtrans.dted.profile import load_profile
from egmtrans.dted.report import FORMATS, build_report, render_report
from egmtrans.dted.validate import count, validate_file

SUBCOMMANDS = ('dted-header', 'dted-index', 'dted-selftest', 'dmed')


def _stderr_logger() -> logging.Logger:
    """The egmtrans logger writing to stderr, so stdout carries only the report."""
    logger = _state.get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='egmtrans', description='DTED header tools.')
    subparsers = parser.add_subparsers(dest='command', required=True)

    header = subparsers.add_parser(
        'dted-header', help='Report and validate the header of DTED files.',
        description='Report every field of the UHL, DSI and ACC records of DTED files, with the level evidence, '
                    'a summary and the MIL-PRF-89020B findings.',
    )
    header.add_argument('files', nargs='+', metavar='FILE',
                        help='DTED file(s) (.dt0, .dt1, .dt2, or a DTED0 companion .avg, .min or .max)')
    header.add_argument('--format', choices=FORMATS, default='text', help='Report format (default: text)')
    header.add_argument('--out', metavar='PATH', help='Write the report to this file instead of stdout')
    header.add_argument('--zero-based', action='store_true',
                        help='Count byte positions from 0 instead of the spec\'s 1')
    header.add_argument('--check-data', action='store_true',
                        help='Also check the elevation records: sentinels, counts, checksums, null share')
    header.add_argument('--strict', action='store_true', help='Exit with 1 on warnings as well as errors')

    index = subparsers.add_parser('dted-index', help='Build or validate a DTED metadata index.')
    index_commands = index.add_subparsers(dest='index_command', required=True)

    build = index_commands.add_parser(
        'build', help='Harvest rows into a GeoPackage or GeoParquet index.',
        description='Build (or update) an index from the headers of DTED files, from source rasters and their '
                    'tags and XML sidecars (through the profile\'s harvest mappings), or from any attribute table '
                    '(a footprint layer, a catalog): a table column named like an index column fills it, --map '
                    'names the others, --set fills a column with one value, and the import is reported.',
    )
    build.add_argument('--out', required=True, metavar='INDEX', help='Index to write (.gpkg or .parquet)')
    build.add_argument('--from-dted', nargs='+', metavar='PATH', default=[],
                       help='DTED files or folders whose headers become rows')
    build.add_argument('--from-rasters', nargs='+', metavar='PATH', default=[],
                       help='Source rasters (GeoTIFF) or folders; one row per cell they cover')
    build.add_argument('--from-table', metavar='FILE',
                       help='An attribute table (.gpkg, .parquet or any OGR format); one row per record')
    build.add_argument('--from-footprints', metavar='FILE', help='The same as --from-table')
    build.add_argument('--layer', metavar='NAME', help='The layer of the table to read (default: the first)')
    build.add_argument('--cell-field', metavar='NAME',
                       help='Table column that holds the cell id (default: a cell_id column, else the geometry)')
    build.add_argument('--map', action='append', default=[], metavar='INDEX_COLUMN=TABLE_COLUMN',
                       help='Fill an index column from a table column of another name (may be repeated)')
    build.add_argument('--set', action='append', default=[], metavar='INDEX_COLUMN=VALUE',
                       help='Fill an index column with one value for every row of the table (may be repeated)')
    build.add_argument('--prefer', metavar='TABLE_COLUMN',
                       help='When several rows share a cell, keep the one with the greatest value here')
    build.add_argument('--profile', metavar='FILE', help='Product profile (TOML) with the harvest mappings')
    build.add_argument('--level', type=int, choices=(0, 1, 2), help='DTED level the index is for')
    build.add_argument('--product', metavar='NAME', help='Product name recorded in the index meta table')
    build.add_argument('--update', action='store_true', help='Update an existing index instead of replacing it')

    validate = index_commands.add_parser('validate', help='Check an index against the schema.')
    validate.add_argument('index', metavar='INDEX', help='Index to check (.gpkg or .parquet)')
    validate.add_argument('--profile', metavar='FILE', help='Product profile to check alongside')
    validate.add_argument('--level', type=int, choices=(0, 1, 2), help='DTED level the index must be for')

    selftest = subparsers.add_parser(
        'dted-selftest', help='Convert built-in synthetic tiles to DTED and compare the bytes with the reference.',
        description='Builds two synthetic tiles, converts them to DTED2, DTED1 and DTED0 from EGM2008 to EGM96 '
                    'and compares the SHA-256 of every header and record block with the pinned reference. A match '
                    'shows this host reproduces the reference bytes. The geoid grids must be present.',
    )
    selftest.add_argument('--keep', metavar='FOLDER',
                          help='Write the tiles and the DTED files to this folder and keep them')
    selftest.add_argument('--print-reference', action='store_true',
                          help='Print the hashes as the REFERENCE mapping of egmtrans.dted.selftest')

    dmed = subparsers.add_parser(
        'dmed', help='Write the DMED volume file of a DTED delivery.',
        description='Describe every cell under the DTED folder of a delivery (DTED/E006/N49.dt2, '
                    'MIL-PRF-89020B 3.10.7.2) in the DMED file of 3.9.5: the bounding rectangle, then for each '
                    'cell of the rectangle its edition, its match/merge version and the minimum, maximum, mean '
                    'and standard deviation of the posts of each 15-minute area. The file is written as DMED at '
                    'the root of the delivery, beside the DTED folder.',
    )
    dmed.add_argument('folder', metavar='FOLDER', help='The delivery (the folder that holds DTED/), or its DTED folder')
    dmed.add_argument('--out', metavar='PATH', help='Write the DMED to this file instead of <FOLDER>/DMED')
    dmed.add_argument('--check', action='store_true',
                      help='Do not write: compare the existing DMED with what the cells give')
    return parser


def report_extension(path: str) -> str:
    """The extension the validator judges *path* by: a DTED0 companion file
    (``.avg``, ``.min``, ``.max``) is judged as a ``.dt0``."""
    extension = os.path.splitext(path)[1].lower()
    return '.dt0' if extension in COMPANION_EXTENSIONS else extension


def _write_out(text: str, out: str | None) -> None:
    if out:
        with open(out, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def run_header(args: argparse.Namespace, logger: logging.Logger) -> int:
    reports = []
    exit_code = 0
    for path in args.files:
        if not os.path.isfile(path):
            logger.error(f'Not a file: {path}')
            return 2
        try:
            header, issues = validate_file(path, check_data=args.check_data, extension=report_extension(path))
        except (OSError, ValueError) as e:
            logger.error(f'{path}: {e}')
            return 2
        report = build_report(path, header, issues, zero_based=args.zero_based)
        reports.append(report)
        errors, warnings = count(issues, 'error'), count(issues, 'warning')
        if errors or (args.strict and warnings):
            exit_code = 1
        logger.info(f'{os.path.basename(path)}: {errors} error(s), {warnings} warning(s)')

    if args.format == 'json':
        documents = [{k: v for k, v in r.items() if not k.startswith('_')} for r in reports]
        text = json.dumps(documents[0] if len(documents) == 1 else documents, indent=2) + '\n'
    elif args.format == 'csv':
        chunks = [render_report(r, 'csv') for r in reports]
        text = chunks[0] + ''.join(c.split('\n', 1)[1] for c in chunks[1:])
    else:
        text = '\n'.join(render_report(r, args.format) for r in reports)
    _write_out(text, args.out)
    if args.out:
        logger.info(f'Report written to {args.out}')
    return exit_code


def supply_lines(index: DtedIndex, profile) -> list[str]:
    """Which required header columns the index lacks, split by whether the profile supplies them."""
    absent = [column.name for column in HEADER_COLUMNS if column.required and column.name not in index.columns]
    if not absent:
        return ['Every required header column is in the index.']
    product = profile.product if profile is not None else {}
    from_profile = [name for name in absent if name in product]
    from_nothing = [name for name in absent if name not in product]
    lines = []
    if from_profile:
        lines.append('Required header columns the profile supplies: ' + ', '.join(from_profile))
    if from_nothing:
        lines.append('Required header columns nothing supplies yet (a profile, or --dted-set at run time, must): '
                     + ', '.join(from_nothing))
    return lines


def run_index_build(args: argparse.Namespace, logger: logging.Logger) -> int:
    table = args.from_table or args.from_footprints
    if not (args.from_dted or args.from_rasters or table):
        logger.error('Give at least one source: --from-dted, --from-rasters or --from-table')
        return 2
    if (args.map or args.set or args.prefer or args.layer) and not table:
        logger.error('--map, --set, --prefer and --layer go with --from-table')
        return 2
    profile = None
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ValueError as e:
            logger.error(str(e))
            return 2
    try:
        column_map = parse_column_map(args.map)
        constants = parse_constants(args.set)
    except ValueError as e:
        logger.error(str(e))
        return 2
    try:
        index = build_index(
            args.out, from_dted=args.from_dted, from_rasters=args.from_rasters, from_table=table,
            cell_field=args.cell_field, layer=args.layer, column_map=column_map, constants=constants,
            prefer=args.prefer, profile=profile, level=args.level, update=args.update, product=args.product,
        )
    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f'Index not written: {e}')
        return 1
    logger.info(f'{os.path.basename(args.out)}: {len(index)} cell(s), '
                f'{sum(len(s) for s in index.subregions.values())} subregion(s), level {index.level}')
    issues = validate_index(index, level=args.level)
    for issue in issues:
        logger.log(logging.ERROR if issue.severity == 'error' else logging.WARNING if issue.severity == 'warning'
                   else logging.INFO, str(issue))
    for line in supply_lines(index, profile):
        logger.info(line)
    return 1 if count(issues, 'error') else 0


def run_index_validate(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        index = read_index(args.index)
    except (OSError, ValueError, RuntimeError) as e:
        logger.error(str(e))
        return 2
    level = args.level
    issues = validate_index(index, level=level)
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ValueError as e:
            logger.error(str(e))
            return 2
        if level is not None and profile.level not in (None, level):
            logger.error(f'The profile is for level {profile.level}, not {level}')
            return 1
        from egmtrans.dted.index import HEADER_COLUMNS

        missing = [c.name for c in HEADER_COLUMNS if c.required and c.name not in index.columns
                   and c.name not in profile.product]
        if missing:
            logger.error(f'Neither the index nor the profile supplies: {", ".join(missing)}')
            return 1
    logger.info(f'{os.path.basename(args.index)}: {len(index)} cell(s), level {index.level}, '
                f'{sum(len(s) for s in index.subregions.values())} subregion(s)')
    for issue in issues:
        logger.log(logging.ERROR if issue.severity == 'error' else logging.WARNING if issue.severity == 'warning'
                   else logging.INFO, str(issue))
    errors = count(issues, 'error')
    logger.info(f'{errors} error(s), {count(issues, "warning")} warning(s)')
    return 1 if errors else 0


def run_selftest(args: argparse.Namespace, logger: logging.Logger) -> int:
    from egmtrans.cli import versions_line
    from egmtrans.config import verify_grids
    from egmtrans.dted.selftest import SOURCE_DATUM, TARGET_DATUM, reference_text
    from egmtrans.dted.selftest import run_selftest as run

    logger.info(versions_line())
    try:
        verify_grids(SOURCE_DATUM, TARGET_DATUM)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2
    try:
        result = run(args.keep, keep=bool(args.keep))
    except Exception as e:
        logger.error(f'The self-test could not run: {e}')
        return 1
    for item in result.items:
        if item.expected is None:
            state = 'no reference'
        else:
            state = 'matches the reference' if item.ok else 'DIFFERS from the reference'
        logger.info(f'{item.name}: header {item.header[:16]}..., records {item.records[:16]}...: {state}')
    if args.print_reference:
        sys.stdout.write(reference_text(result) + '\n')
    if args.keep:
        logger.info(f'Files kept under {result.folder}')
    if result.ok:
        logger.info('Self-test passed: this host reproduces the reference bytes.')
        return 0
    logger.error('Self-test FAILED: this host does not reproduce the reference bytes.')
    return 1


def run_dmed(args: argparse.Namespace, logger: logging.Logger) -> int:
    from egmtrans.dted.dmed import DmedError, check_dmed, write_dmed

    try:
        if args.check:
            problems = check_dmed(args.folder, args.out)
            if problems:
                for problem in problems:
                    logger.error(problem)
                logger.error('The DMED does not match the cells.')
                return 1
            logger.info('The DMED matches the cells.')
            return 0
        result = write_dmed(args.folder, args.out)
    except DmedError as e:
        logger.error(str(e))
        return 2
    except OSError as e:
        logger.error(f'The DMED could not be written: {e}')
        return 1
    rectangle = result.rectangle
    logger.info(
        f'Wrote {result.path}: {result.record_count} records of 394 bytes for {len(result.cells)} cell(s) in a '
        f'rectangle of {rectangle.east - rectangle.west} x {rectangle.north - rectangle.south} degrees.'
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run a subcommand; returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = _stderr_logger()
    if args.command == 'dted-header':
        return run_header(args, logger)
    if args.command == 'dted-selftest':
        return run_selftest(args, logger)
    if args.command == 'dmed':
        return run_dmed(args, logger)
    if args.index_command == 'build':
        return run_index_build(args, logger)
    return run_index_validate(args, logger)
