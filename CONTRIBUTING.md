# Contributing to EGMTrans

Thanks for your interest in EGMTrans. This guide covers what you need to
get a development environment running and what we expect from pull
requests.

## Development setup

EGMTrans targets Python 3.13+ (ArcGIS Pro 3.7 ships 3.13; the development
environment is 3.14). GDAL is the one dependency that is painful to install
via pip on Windows; conda is the path of least resistance.

```bash
conda env create -f environment.yml
conda activate egmtrans
pip install -e ".[dev]"
python download_grids.py
```

The `[dev]` extra installs `pytest`, `pytest-cov`, and `ruff` in addition
to the runtime dependencies, plus `pyarrow` and `lxml` (the `[index]` extra)
so the GeoParquet and XML harvest tests run rather than skip.

## Running tests and lint

```bash
pytest                         # full test suite
pytest tests/test_accuracy.py  # regression tests against known values
ruff check src tests           # style and lint
```

CI runs the same commands on Python 3.13 and 3.14 on Ubuntu, and the
determinism, conversion and flattening tests once more on a stack like
ArcGIS Pro 3.7 (Python 3.13, GDAL 3.12, numpy 2.3, no Numba); please make
sure the suite is green locally before opening a PR.

## Code style

The project uses `ruff` with the rule set declared in `pyproject.toml`
(`E`, `W`, `F`, `I`, `UP`, `B`). Line length is 120. Type hints are
expected on new public functions. Logging goes through the per-module
logger obtained from `egmtrans._state.get_logger()`, not `print`.

Text is written in American English (meter, neighbor, labeled, analyze,
center); `tests/test_spelling.py` fails on the common British spellings.

A DTED file made from a GeoTIFF must come out the same on every computer,
so the code that can feed one keeps to a few rules: heights are compared in
whole centimeters (`flattening.height_centimeters`), never as floats; the
Numba kernels on that path take no fast-math flags and do no float
arithmetic beyond comparisons and copies, so their plain-Python form gives
the same result; the resampler (`dted/resample.py`) uses whole-number
weights and elementwise numpy, no reductions, no GDAL; and the DTED bytes
are written by `dted/records.py`, not by a GDAL driver. A change that moves
those bytes on purpose must regenerate the reference hashes of
`egmtrans dted-selftest` (`--print-reference`) and the pins in
`tests/test_determinism.py`, and say so in the CHANGELOG.

## Pull requests

- One logical change per PR. Small and focused beats large and sweeping.
- Update `CHANGELOG.md` under the "Unreleased" section if the change is
  user-visible.
- If you touch the vertical-datum transform or the interpolation code,
  add or update a test in `tests/test_accuracy.py` that pins the new
  behavior against a known reference value.
- If you touch `src/egmtrans/download.py` or change which grid files are
  required, update both the pinned SHA-256 hashes and `SECURITY.md`.

## Reporting bugs

Open a GitHub issue with a minimal reproducer – ideally the command line
you ran, the input file's format and size, and the traceback or wrong
output. For security-relevant bugs, follow the process in
`SECURITY.md` instead.
