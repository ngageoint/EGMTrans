# Geoid Grid Files

The geoid grid GeoTIFF files are not included in this repository.
They are hosted as GitHub Release assets and must be downloaded separately.

## Quick download

Run from the EGMTrans root directory:

```bash
python download_grids.py
```

The ArcGIS Pro toolbox will also download the grid files automatically the
first time you run the EGMTrans Tool.

## Manual download

Download from the GitHub Releases page:
<https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1>

Place the `.tif` files in this `datums/` directory.

### Required for transformation

| File | Size | Description |
|------|------|-------------|
| `us_nga_egm96_1.tif` | 323 MB | EGM96 geoid at 1 arc-minute resolution |
| `us_nga_egm08_1.tif` | 374 MB | EGM2008 geoid at 1 arc-minute resolution |

### For EGMTrans Explorer map

| File | Size | Description |
|------|------|-------------|
| `egm96_to_egm2008_delta.tif` | 468 MB | EGM96-to-EGM2008 difference grid |
| `us_nga_egm08_25.tif` | 96 MB | EGM2008 at 2.5 arc-minute resolution |
| `us_nga_egm96_15.tif` | 3.2 MB | EGM96 at 15 arc-minute resolution |

## SHA-256 Checksums

```
bb1d699ff17f6d641116c70109521c234396f68095d49234655d8cd519997b03  us_nga_egm96_1.tif
916b9a59a9590bcaea022cc4e3aec78d4c55653c30b06b9969380d1872de4b4e  us_nga_egm08_1.tif
ce33cddc49d2bf964bcec80bd0ed065bca9cc4034dc3d2e29eae6addcc7c4181  egm96_to_egm2008_delta.tif
2fceeebc3f1e43719a7f350cf5162ca75187d8ea5732dd9871d63f688e00a1a3  us_nga_egm08_25.tif
856a47a31af9195f8b3e565ae364924cbca3e7dd329f874595a2e6e0487ed678  us_nga_egm96_15.tif
```
