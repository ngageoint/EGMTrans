# Contributing to EGMTrans

Thanks for your interest in EGMTrans. This guide covers what you need to
get a development environment running and what we expect from pull
requests.

## Development setup

EGMTrans targets Python 3.11+. GDAL is the one dependency that is painful
to install via pip on Windows; conda is the path of least resistance.

```bash
conda env create -f environment.yml
conda activate egm_trans
pip install -e ".[dev]"
python download_grids.py
```

The `[dev]` extra installs `pytest`, `pytest-cov`, and `ruff` in addition
to the runtime dependencies.

## Running tests and lint

```bash
pytest                         # full test suite
pytest tests/test_accuracy.py  # regression tests against known values
ruff check src tests           # style and lint
```

CI runs the same commands on Python 3.11 and 3.12 on Ubuntu — please make
sure both are green locally before opening a PR.

## Code style

The project uses `ruff` with the rule set declared in `pyproject.toml`
(`E`, `W`, `F`, `I`, `UP`, `B`). Line length is 120. Type hints are
expected on new public functions. Logging goes through the per-module
logger obtained from `egmtrans._state.get_logger()`, not `print`.

## Commit attribution

If you use an AI assistant (Claude Code, Copilot, etc.) while working on a
patch, do **not** add a `Co-Authored-By:` line for the assistant to your
commit message. Attribute only the humans who directed and reviewed the
change.

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

Open a GitHub issue with a minimal reproducer — ideally the command line
you ran, the input file's format and size, and the traceback or wrong
output. For security-relevant bugs, follow the process in
`SECURITY.md` instead.
