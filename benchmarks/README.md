# Benchmarking EGMTrans

`benchmark_tiles.py` times EGMTrans on real tiles and turns the result into two
tables: seconds and memory per tile, and the cost of a global run. Every
measurement runs in a fresh process, so each mode sees exactly the Numba settings
it names:

| Mode | What it measures |
|---|---|
| `cold` | First run with an empty Numba cache, JIT compilation included |
| `warm` | The same run with the cache primed (median of `--repeats`, default 3) |
| `single` | Warm, restricted to one thread: the cost of one tile per core in a large batch |
| `nojit` | Pure Python (`NUMBA_DISABLE_JIT=1`): the baseline for the Numba speedup |
| `cli` | The whole `egmtrans ... -y` command, as a container runs it per tile |

Every run is checked as well as timed: voids stay voids, ocean stays at 0 m, and
a DTED output carries the target datum code (`E96`) in its header.

## Setup

Use the conda environment from the main README, then fetch the grids:

```bat
conda env create -f environment.yml
conda activate egmtrans
pip install -e .
python download_grids.py
```

On a machine without internet access, copy `us_nga_egm96_1.tif` and
`us_nga_egm08_1.tif` into `datums\` instead; the benchmark reads only those two.

## Run

Point it at tiles or folders. Folders are scanned for DEMs, and TanDEM-X auxiliary
layers (HEM, WBM, EDM, AMP, SDM) are skipped. The default direction is
EGM2008 to EGM96.

```bat
REM One 0.4-arc-second GeoTIFF tile, plus the DTED2 and DTED1 made from it
python benchmarks\benchmark_tiles.py ^
    D:\data\tiles\N06E126_DEM.tif ^
    D:\data\dted\e126\n06.dt2 D:\data\dted1\e126\n06.dt1 ^
    --crosscheck-proj --out results\run1

REM A folder of tiles, fast modes only
python benchmarks\benchmark_tiles.py D:\data\tiles --modes warm,single,cli --out results\batch
```

On Linux or macOS, use `/` paths and `\` for line continuation.

It writes `<out>.csv` (every field of every run) and `<out>.md` (the tables to
paste into a briefing), and prints the Markdown when it finishes.

## Choosing tiles

- Use tiles from a region where EGM2008 and EGM96 differ by meters (the
  Philippines, eastern Indonesia, Myanmar), with coastline and lakes, so the
  flattening and the checks do real work.
- Include one of each product you want to quote: a 0.4″ GeoTIFF, a DTED2 and
  a DTED1.
- The `nojit` mode is slow on large tiles: tens of minutes for a 0.4″ tile. To
  quote the Numba speedup from the DTED tiles only, add
  `--nojit-max-pixels 20000000`.
- Close other heavy applications, and keep a laptop plugged in.

## Reading the results

- **Transform (s)** is the work on one tile once Python is running. **Process
  wall (s)** adds interpreter start-up and imports, and for `cli` also the SHA-256
  check of the grids and the log setup.
- **Core-hours** assume one tile per core, single-threaded (the `single` time),
  which is how a large pre-cache run is packed. Wall time divides the core-hours
  by the core count and ignores disk contention.
- `--crosscheck-proj` transforms each GeoTIFF a second time with PROJ (GDAL)
  instead of EGMTrans's bilinear interpolation, with flattening off. It reports
  the largest difference, which should be within the 1 cm output rounding.
