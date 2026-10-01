"""The ``dted-header`` and ``dted-index`` subcommands.

    egmtrans dted-header FILE... [--format text|json|csv|md] [--out PATH]
                         [--zero-based] [--check-data] [--strict]
    egmtrans dted-index build --out INDEX (--from-dted PATH... | --from-rasters PATH...
                         | --from-footprints FILE [--cell-field NAME]) [--profile FILE]
                         [--level N] [--product NAME] [--update]
    egmtrans dted-index validate INDEX [--profile FILE] [--level N]

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
from egmtrans.dted.harvest import build_index
from egmtrans.dted.index import read_index, validate_index
from egmtrans.dted.profile import load_profile
from egmtrans.dted.report import FORMATS, build_report, render_report
from egmtrans.dted.validate import count, validate_file

SUBCOMMANDS = ('dted-header', 'dted-index')


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
    header.add_argument('files', nargs='+', metavar='FILE', help='DTED file(s) (.dt0, .dt1, .dt2)')
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
                    'tags and XML sidecars (through the profile\'s harvest mappings), or from a footprint layer.',
    )
    build.add_argument('--out', required=True, metavar='INDEX', help='Index to write (.gpkg or .parquet)')
    build.add_argument('--from-dted', nargs='+', metavar='PATH', default=[],
                       help='DTED files or folders whose headers become rows')
    build.add_argument('--from-rasters', nargs='+', metavar='PATH', default=[],
                       help='Source rasters (GeoTIFF) or folders; one row per cell they cover')
    build.add_argument('--from-footprints', metavar='FILE', help='A footprint layer (any OGR format)')
    build.add_argument('--cell-field', metavar='NAME', help='Footprint field that holds the cell id')
    build.add_argument('--profile', metavar='FILE', help='Product profile (TOML) with the harvest mappings')
    build.add_argument('--level', type=int, choices=(0, 1, 2), help='DTED level the index is for')
    build.add_argument('--product', metavar='NAME', help='Product name recorded in the index meta table')
    build.add_argument('--update', action='store_true', help='Update an existing index instead of replacing it')

    validate = index_commands.add_parser('validate', help='Check an index against the schema.')
    validate.add_argument('index', metavar='INDEX', help='Index to check (.gpkg or .parquet)')
    validate.add_argument('--profile', metavar='FILE', help='Product profile to check alongside')
    validate.add_argument('--level', type=int, choices=(0, 1, 2), help='DTED level the index must be for')
    return parser


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
            header, issues = validate_file(path, check_data=args.check_data)
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


def run_index_build(args: argparse.Namespace, logger: logging.Logger) -> int:
    if not (args.from_dted or args.from_rasters or args.from_footprints):
        logger.error('Give at least one source: --from-dted, --from-rasters or --from-footprints')
        return 2
    profile = None
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ValueError as e:
            logger.error(str(e))
            return 2
    try:
        index = build_index(
            args.out, from_dted=args.from_dted, from_rasters=args.from_rasters, from_footprints=args.from_footprints,
            cell_field=args.cell_field, profile=profile, level=args.level, update=args.update, product=args.product,
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


def main(argv: list[str] | None = None) -> int:
    """Run a subcommand; returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = _stderr_logger()
    if args.command == 'dted-header':
        return run_header(args, logger)
    if args.index_command == 'build':
        return run_index_build(args, logger)
    return run_index_validate(args, logger)
