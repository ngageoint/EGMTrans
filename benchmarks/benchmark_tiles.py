#!/usr/bin/env python3
"""Time EGMTrans on real tiles and extrapolate to a global run.

Each measurement runs in a fresh Python process so that the Numba settings
(JIT on or off, thread count, compile cache) are exactly those of the mode being
measured.  For every tile the modes are, in order:

  cold    first run with an empty Numba cache: includes JIT compilation
  warm    the same run again with the cache primed (median of --repeats)
  single  warm, but restricted to one thread (NUMBA_NUM_THREADS=1): the cost of
          one tile per core, which is how a large batch is packed
  nojit   pure Python (NUMBA_DISABLE_JIT=1): the no-Numba baseline; slow on
          large tiles, hence --nojit-timeout
  cli     the whole command line, `egmtrans ... -y`: imports, grid checksum
          checks and logging included, as a container would run per tile

Every run is checked, not just timed: voids must stay voids, ocean must stay at
0 m, and a DTED output must carry the target datum code in its header.

Examples (conda env with EGMTrans installed, from the repository root):

  python benchmarks/benchmark_tiles.py D:/data/tiles/N06E126_DEM.tif
  python benchmarks/benchmark_tiles.py D:/data/tiles D:/data/dted --modes warm,single,cli
  python benchmarks/benchmark_tiles.py samples --crosscheck-proj --out results/laptop

Writes <out>.csv and <out>.md; paste the Markdown tables into a briefing.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

# Run from a plain clone as well as from an installed package: like download_grids.py,
# fall back to the repository's src/ when egmtrans is not installed. PYTHONPATH carries the
# fallback to the child processes and to `python -m egmtrans`. find_spec only locates the
# package, so the children still time its import.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src')
if importlib.util.find_spec('egmtrans') is None and os.path.isdir(os.path.join(_SRC, 'egmtrans')):
    sys.path.insert(0, _SRC)
    os.environ['PYTHONPATH'] = os.pathsep.join(p for p in (_SRC, os.environ.get('PYTHONPATH')) if p)

DEM_EXTENSIONS = ('.tif', '.tiff', '.dt0', '.dt1', '.dt2')
DTED_EXTENSIONS = ('.dt0', '.dt1', '.dt2')
ALL_MODES = ('cold', 'warm', 'single', 'nojit', 'cli')
TILE_COUNTS = (19389, 26475)  # TanDEM-X 90 m DEM tiles; Copernicus GLO-30 tiles


# ---------------------------------------------------------------------------
# Child process: one timed transform, printed as JSON on the last line
# ---------------------------------------------------------------------------

def _peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB, or None if unavailable."""
    try:
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return kb / 1024 / (1024 if sys.platform == 'darwin' else 1)
    except ImportError:
        pass
    try:
        import psutil

        return psutil.Process().memory_info().peak_wset / 2**20
    except Exception:
        pass
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 2**20
    return None


def _check_output(input_file: str, output_file: str, target: str) -> dict:
    """Voids kept, ocean at 0 m, and (DTED) the header datum code."""
    import numpy as np
    from osgeo import gdal

    from egmtrans.config import DATUM_MAPPING, DTED_NODATA

    with gdal.Open(input_file) as ds:
        band = ds.GetRasterBand(1)
        src = band.ReadAsArray().astype(np.float64)
        nodata = band.GetNoDataValue()
        scale = band.GetScale() or 1
        offset = band.GetOffset() or 0
    with gdal.Open(output_file) as ds:
        out = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)

    is_dted = output_file.lower().endswith(DTED_EXTENSIONS)
    src_void = np.isnan(src) if nodata is None else (src == nodata) | np.isnan(src)
    src = src * scale + offset
    out_void = (out == DTED_NODATA) if is_dted else np.isnan(out)
    ocean = ~src_void & ((src == 0) if is_dted else (np.abs(np.round(src, 2)) < 0.01))

    result = {
        'voids': int(src_void.sum()),
        'voids_kept': bool(np.array_equal(src_void, out_void)),
        'ocean_px': int(ocean.sum()),
        'ocean_zero': bool(np.all(out[ocean] == 0)),
    }
    if is_dted:
        with open(output_file, 'rb') as f:
            f.seek(80 + 141)
            code = f.read(3).decode('ascii', 'replace')
        result['header'] = code
        result['header_ok'] = code == DATUM_MAPPING[target]['dted_code']
    return result


def _crosscheck_proj(input_file: str, source: str, target: str, workdir: str) -> dict:
    """Largest difference between the bilinear and PROJ transforms, flattening off."""
    import numpy as np
    from osgeo import gdal

    from egmtrans.transform import transform_vertical_datum

    outs = {}
    for algorithm in ('bilinear', 'proj'):
        path = os.path.join(workdir, f'xcheck_{algorithm}.tif')
        transform_vertical_datum(input_file, path, source, target, False, False, 16, algorithm, save_log=False)
        with gdal.Open(path) as ds:
            outs[algorithm] = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    diff = np.abs(outs['bilinear'] - outs['proj'])
    diff = diff[np.isfinite(diff)]
    return {
        'proj_max_diff_m': round(float(diff.max()), 3) if diff.size else None,
        'proj_p99_diff_m': round(float(np.percentile(diff, 99)), 3) if diff.size else None,
    }


def child(spec: dict) -> None:
    """Run one transform in this process and print a JSON result line.

    *spec* arrives as JSON on stdin (see :func:`_run_child`).
    """
    import logging

    t0 = time.perf_counter()
    from egmtrans.transform import transform_vertical_datum

    t_import = time.perf_counter() - t0
    logger = logging.getLogger('egmtrans')
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.setLevel(logging.WARNING)

    t1 = time.perf_counter()
    transform_vertical_datum(
        spec['input'], spec['output'], spec['source'], spec['target'],
        flatten=True, create_mask=False, min_patch_size=spec['min_patch_size'],
        algorithm='bilinear', save_log=False,
    )
    t_transform = time.perf_counter() - t1
    peak = _peak_rss_mb()

    result = {'import_s': round(t_import, 3), 'transform_s': round(t_transform, 3), 'peak_mb': peak}
    result.update(_check_output(spec['input'], spec['output'], spec['target']))
    if spec.get('crosscheck_proj') and not spec['input'].lower().endswith(DTED_EXTENSIONS):
        result.update(_crosscheck_proj(spec['input'], spec['source'], spec['target'], spec['workdir']))
    print(json.dumps(result))


# ---------------------------------------------------------------------------
# Parent process: modes, timing, tables
# ---------------------------------------------------------------------------

def _find_inputs(paths: list[str]) -> list[str]:
    """Expand folders to the DEMs inside them, skipping masks and auxiliary layers."""
    from egmtrans.file_utils import is_valid_dem

    found = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if name.lower().endswith(DEM_EXTENSIONS) and is_valid_dem(full):
                        found.append(full)
        elif os.path.isfile(path):
            found.append(path)
        else:
            sys.exit(f'error: not found: {path}')
    return found


def _describe(path: str) -> dict:
    from osgeo import gdal

    with gdal.Open(path) as ds:
        gt = ds.GetGeoTransform()
        cols, rows = ds.RasterXSize, ds.RasterYSize
        dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
        srs = ds.GetSpatialRef()
    spacing = f'{abs(gt[5]) * 3600:.2f}"' if srs is not None and srs.IsGeographic() else f'{abs(gt[5]):.1f} m'
    ext = os.path.splitext(path)[1].lower()
    kind = {'.dt0': 'DTED0', '.dt1': 'DTED1', '.dt2': 'DTED2'}.get(ext, 'GeoTIFF')
    return {'kind': kind, 'rows': rows, 'cols': cols, 'dtype': dtype, 'spacing': spacing}


def _run_child(spec: dict, env: dict, timeout: float | None) -> dict:
    t0 = time.perf_counter()
    try:
        # The spec goes through stdin: a JSON argument would be at the mercy of Windows quoting.
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), '--child'],
            input=json.dumps(spec), env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {'error': f'timed out after {timeout:.0f} s', 'wall_s': round(time.perf_counter() - t0, 1)}
    wall = time.perf_counter() - t0
    lines = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not lines:
        return {'error': (proc.stderr.strip().splitlines() or ['no output'])[-1], 'wall_s': round(wall, 1)}
    result = json.loads(lines[-1])
    result['wall_s'] = round(wall, 3)
    return result


def _run_cli(spec: dict, env: dict, timeout: float | None) -> dict:
    args = [
        sys.executable, '-m', 'egmtrans', '-i', spec['input'], '-o', spec['output'],
        '-s', spec['source'], '-t', spec['target'], '-y', '-l', 'False', '-p', str(spec['min_patch_size']),
    ]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': f'timed out after {timeout:.0f} s'}
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        return {'error': f'exit code {proc.returncode}', 'wall_s': round(wall, 1)}
    return {'wall_s': round(wall, 3)}


def benchmark(args: argparse.Namespace) -> list[dict]:
    inputs = _find_inputs(args.inputs)
    if not inputs:
        sys.exit('error: no DEM tiles found')
    modes = [m.strip() for m in args.modes.split(',') if m.strip()]
    unknown = set(modes) - set(ALL_MODES)
    if unknown:
        sys.exit(f"error: unknown mode(s): {', '.join(sorted(unknown))}")

    rows = []
    for n, path in enumerate(inputs, 1):
        info = _describe(path)
        print(f"[{n}/{len(inputs)}] {os.path.basename(path)}  {info['kind']} {info['rows']}x{info['cols']} "
              f"{info['dtype']} {info['spacing']}", flush=True)
        workdir = tempfile.mkdtemp(prefix='egmtrans_bench_')
        cache = os.path.join(workdir, 'numba_cache')
        out = os.path.join(workdir, 'out' + os.path.splitext(path)[1].lower())
        base_env = dict(os.environ, NUMBA_CACHE_DIR=cache, PYTHONUNBUFFERED='1')
        base_env.pop('NUMBA_DISABLE_JIT', None)
        spec = {
            'input': path, 'output': out, 'source': args.source, 'target': args.target,
            'min_patch_size': args.min_patch_size, 'workdir': workdir, 'crosscheck_proj': False,
        }
        pixels = info['rows'] * info['cols']
        tile = {'file': os.path.basename(path), **info, 'pixels': pixels}

        def record(mode: str, result: dict, runs: int = 1, tile=tile, out=out) -> None:
            rows.append({**tile, 'mode': mode, 'runs': runs, **result})
            if 'error' in result:
                print(f"    {mode:<6} ERROR: {result['error']}", flush=True)
            else:
                t = result.get('transform_s', result.get('wall_s'))
                checks = ' '.join(f'{k}={v}' for k, v in result.items() if k.endswith(('_kept', '_zero', '_ok')))
                print(f'    {mode:<6} {t:8.2f} s  {checks}', flush=True)
            if os.path.exists(out):
                os.remove(out)

        try:
            if 'cold' in modes or 'warm' in modes or 'single' in modes:
                # The cold run also primes the cache for the warm and single runs.
                cold = _run_child(spec, base_env, args.timeout)
                if 'cold' in modes:
                    record('cold', cold)
            if 'warm' in modes:
                runs = [_run_child(dict(spec, crosscheck_proj=args.crosscheck_proj and i == 0), base_env,
                                   args.timeout) for i in range(args.repeats)]
                good = [r for r in runs if 'error' not in r]
                if good:
                    best = dict(good[0])
                    for key in ('transform_s', 'wall_s', 'import_s'):
                        best[key] = round(statistics.median(r[key] for r in good), 3)
                    best['peak_mb'] = max((r['peak_mb'] or 0) for r in good) or None
                    record('warm', best, runs=len(good))
                else:
                    record('warm', runs[0])
            if 'single' in modes:
                record('single', _run_child(spec, dict(base_env, NUMBA_NUM_THREADS='1'), args.timeout))
            if 'nojit' in modes:
                if args.nojit_max_pixels and pixels > args.nojit_max_pixels:
                    print(f'    nojit  skipped ({pixels:,} px > --nojit-max-pixels)', flush=True)
                else:
                    record('nojit', _run_child(spec, dict(base_env, NUMBA_DISABLE_JIT='1'), args.nojit_timeout))
            if 'cli' in modes:
                record('cli', _run_cli(spec, base_env, args.timeout))
        finally:
            if args.keep:
                print(f'    outputs kept in {workdir}')
            else:
                shutil.rmtree(workdir, ignore_errors=True)
    return rows


def _environment() -> dict:
    import numba
    from osgeo import gdal

    import egmtrans
    from egmtrans import __version__

    cpu = os.environ.get('PROCESSOR_IDENTIFIER') or platform.processor()
    if sys.platform == 'win32':
        try:  # the marketing name, not "Intel64 Family 6 Model ..."
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                cpu = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/cpuinfo') as f:
                cpu = next(line.split(':', 1)[1].strip() for line in f if line.startswith('model name'))
        except (OSError, StopIteration):
            pass
    return {
        'egmtrans': __version__, 'egmtrans_path': os.path.dirname(os.path.abspath(egmtrans.__file__)),
        'python': platform.python_version(), 'gdal': gdal.__version__,
        'numba': numba.__version__, 'threads': numba.config.NUMBA_NUM_THREADS, 'cpu': cpu,
        'logical_cpus': os.cpu_count(), 'platform': platform.platform(),
    }


def _markdown(rows: list[dict], env: dict, args: argparse.Namespace) -> str:
    lines = [
        f"EGMTrans {env['egmtrans']} benchmark, {time.strftime('%Y-%m-%d')}: {env['cpu']} "
        f"({env['logical_cpus']} logical CPUs, {env['threads']} Numba threads), {env['platform']}, "
        f"Python {env['python']}, GDAL {env['gdal']}, Numba {env['numba']}. "
        f"{args.source} to {args.target}, flattening on, min patch size {args.min_patch_size}.",
        '',
        '| Tile | Type | Size | Mode | Transform (s) | Process wall (s) | Peak RAM (MB) | Checks |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for r in rows:
        if 'error' in r:
            lines.append(f"| {r['file']} | {r['kind']} | {r['rows']}x{r['cols']} | {r['mode']} | "
                         f"error: {r['error']} | | | |")
            continue
        checks = ', '.join(k for k in ('voids_kept', 'ocean_zero', 'header_ok') if r.get(k) is True)
        failed = [k for k in ('voids_kept', 'ocean_zero', 'header_ok') if r.get(k) is False]
        if failed:
            checks += (', ' if checks else '') + 'FAILED: ' + ', '.join(failed)
        if 'proj_max_diff_m' in r:
            checks += f", PROJ max diff {r['proj_max_diff_m']} m"
        transform = f"{r['transform_s']:.2f}" if 'transform_s' in r else ''
        peak = f"{r['peak_mb']:.0f}" if r.get('peak_mb') else ''
        lines.append(f"| {r['file']} | {r['kind']} {r['spacing']} | {r['rows']}x{r['cols']} | {r['mode']} | "
                     f"{transform} | {r['wall_s']:.2f} | {peak} | {checks} |")

    # Speedups and global extrapolation, per tile.
    by_file: dict[str, dict[str, dict]] = {}
    for r in rows:
        if 'error' not in r:
            by_file.setdefault(r['file'], {})[r['mode']] = r
    lines += ['', '| Tile | Numba speedup (no-JIT / warm) | Numba speedup (no-JIT / 1 thread) | '
              + ' | '.join(f'{n:,} tiles: core-hours (1 thread/tile)' for n in args.tiles)
              + ' | ' + ' | '.join(f'{n:,} tiles on {c} cores (h)' for n in args.tiles for c in args.cores) + ' |']
    lines.append('|' + '---|' * (3 + len(args.tiles) + len(args.tiles) * len(args.cores)))
    for name, modes in by_file.items():
        warm, single, nojit = modes.get('warm'), modes.get('single'), modes.get('nojit')
        s1 = f"{nojit['transform_s'] / warm['transform_s']:.0f}x" if nojit and warm else ''
        s2 = f"{nojit['transform_s'] / single['transform_s']:.0f}x" if nojit and single else ''
        per_core = (single or warm or {}).get('transform_s')
        core_h = [n * per_core / 3600 for n in args.tiles] if per_core else []
        wall = [n * per_core / 3600 / c for n in args.tiles for c in args.cores] if per_core else []
        lines.append(f'| {name} | {s1} | {s2} | ' + ' | '.join(f'{h:,.0f}' for h in core_h)
                     + ' | ' + ' | '.join(f'{h:,.1f}' for h in wall) + ' |')
    lines += ['', 'Core-hours assume one tile per core with one Numba thread (the `single` time); wall time '
              'divides those core-hours across the stated cores and ignores I/O contention.']
    return '\n'.join(lines) + '\n'


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == '--child':
        child(json.loads(sys.stdin.read()))
        return

    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('inputs', nargs='+', help='DEM files, or folders to scan for them')
    parser.add_argument('-s', '--source', default='EGM2008', help='source vertical datum (default: EGM2008)')
    parser.add_argument('-t', '--target', default='EGM96', help='target vertical datum (default: EGM96)')
    parser.add_argument('--modes', default='cold,warm,single,nojit,cli',
                        help=f"comma-separated subset of {','.join(ALL_MODES)}")
    parser.add_argument('--repeats', type=int, default=3, help='warm runs per tile; the median is reported')
    parser.add_argument('-p', '--min-patch-size', type=int, default=16)
    parser.add_argument('--timeout', type=float, default=1800, help='seconds allowed for one Numba run')
    parser.add_argument('--nojit-timeout', type=float, default=5400, help='seconds allowed for one no-JIT run')
    parser.add_argument('--nojit-max-pixels', type=int, default=0,
                        help='skip the no-JIT mode for tiles larger than this (0: never skip)')
    parser.add_argument('--crosscheck-proj', action='store_true',
                        help='also compare the bilinear transform with PROJ (GeoTIFF tiles only)')
    parser.add_argument('--tiles', type=int, nargs='+', default=list(TILE_COUNTS),
                        help='tile counts for the global estimate (default: 19389 26475)')
    parser.add_argument('--cores', type=int, nargs='+', default=[os.cpu_count() or 1, 64],
                        help='core counts for the global wall-time estimate')
    parser.add_argument('--out', default='egmtrans_benchmark', help='output prefix for the .csv and .md files')
    parser.add_argument('--keep', action='store_true', help='keep the transformed outputs')
    args = parser.parse_args()

    from egmtrans.config import get_datums_dir, normalize_datum, required_grids
    from egmtrans.download import ensure_grids

    args.source, args.target = normalize_datum(args.source), normalize_datum(args.target)
    env = _environment()
    print(f"EGMTrans {env['egmtrans']} from {env['egmtrans_path']}", flush=True)
    print(f"on {env['cpu']} ({env['threads']} Numba threads)", flush=True)

    # Fetch or verify the grids now, so that no timed run includes a download.
    grids = required_grids(args.source, args.target)
    try:
        ensure_grids(datums_dir=get_datums_dir(), filenames=grids, message_func=print)
    except Exception as e:
        sys.exit(f"error: the geoid grids are missing and could not be downloaded ({e}).\n"
                 f"Copy {' and '.join(grids)} into {get_datums_dir()} and run again.")
    rows = benchmark(args)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in rows[0], k))
    with open(f'{args.out}.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    markdown = _markdown(rows, env, args)
    with open(f'{args.out}.md', 'w', encoding='utf-8') as f:
        f.write(markdown)
    print('\n' + markdown)
    print(f'Wrote {args.out}.csv and {args.out}.md')


if __name__ == '__main__':
    main()
