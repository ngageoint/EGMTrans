# Security Policy

EGMTrans is a small, self-contained vertical-datum transformation tool. This
document describes its security-relevant behavior so that infosec teams
reviewing the code before adoption can make an informed decision.

## Network egress

EGMTrans makes **exactly one kind of outbound connection**: HTTPS to
`github.com` to download the geoid grid files on first run (or whenever a
grid file is missing or fails its checksum). No other network activity
occurs at any point during normal operation.

- Destination: `https://github.com/ngageoint/EGMTrans/releases/download/datum-grids-v1/`
- Protocol: HTTPS only (no fallback)
- Triggered by: the CLI (`egmtrans`, `download_grids.py`) and the ArcGIS Pro
  toolbox, only when the required grids are not already present in
  `datums/`
- No other hosts are contacted.

Air-gapped deployments: download the grids manually from the release page
above and place them in `datums/`. The tool will detect them on startup and
skip the network call entirely.

## Grid integrity

Every grid file is verified against a SHA-256 hash that is **pinned in the
source code** at `src/egmtrans/download.py` (see the `GRID_FILES` dictionary
near the top of the file). Hashes are recomputed after download and
compared; on mismatch the partial file is deleted and a `RuntimeError` is
raised. Downloads stream to a `.part` file and are renamed atomically on
successful verification.

If you want to verify the grids against the pinned hashes by hand:

```bash
sha256sum datums/us_nga_egm96_1.tif
sha256sum datums/us_nga_egm08_1.tif
```

## Static analysis of the attack surface

- **No `eval`, `exec`, `pickle`, `subprocess`, or `os.system` calls** in the
  core package (`src/egmtrans/`). The tool uses only the standard library
  (`urllib`, `hashlib`) and GDAL's Python bindings to read and write
  raster files.
- **No custom binary parsers.** GeoTIFF reads are delegated to GDAL. DTED
  header writes use hardcoded byte offsets from STANAG 3809 and only write
  numeric or `"  NA"` values into fixed-width fields.
- **No credentials, API keys, or tokens** of any kind, in source or at
  runtime.
- **No telemetry.** The tool does not send usage data anywhere.
- **No dynamic code loading.** No plugin system, no `importlib` of
  user-supplied module names.

## Dependencies

Runtime dependencies are pinned to permissive licenses only: `numpy`
(BSD), `scipy` (BSD), `GDAL` (MIT/X), `numba` (BSD, optional), `tqdm`
(MPL-2.0 / MIT). See `pyproject.toml` for the version floors.

## Reporting a vulnerability

Please report security issues by email to **terrain@nga.mil** with "EGMTrans
security" in the subject line. Do not open a public GitHub issue for
exploitable findings until after a fix has been published.
