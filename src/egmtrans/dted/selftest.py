"""The DTED self-test: convert built-in synthetic tiles and compare the bytes
with the pinned reference.

Two Float32 tiles are built from integer formulas (no transcendental function,
no random stream): a tile south of the 50-degree boundary at 300 posts per
degree, and one north of it at 200 by 300, whose south row the first tile's
north row copies as a TanDEM-X tile does. They hold a lake with a low spot
behind a one-post dam, an outlet to a lower lake, ocean, posts a centimeter
either side of it, a void block and a flat hilltop, so the resampler, the
master row, the containment rule, the low-spot rule and the rounding are all
exercised. They are converted to DTED2, DTED1 and DTED0 from EGM2008 to EGM96
with fictional header values (and a minimum patch size that suits the coarse
tiles, :data:`MIN_PATCH_SIZE`), and the SHA-256 of every header and record
block is compared with :data:`REFERENCE`.

A producer runs ``egmtrans dted-selftest`` on their own host: a match shows
the host reproduces the reference bytes, so the DTED it makes from real tiles
will match any other host that matches.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field

import numpy as np
from osgeo import gdal, osr

from egmtrans.dted.header import CellGeometry
from egmtrans.dted.profile import HarvestConfig, Profile
from egmtrans.dted.resample import BAND_EDGE_STEP
from egmtrans.dted.schema import HEADER_LENGTH
from egmtrans.dted.writer import DtedMetadata, DtedMetadataSource

SOURCE_DATUM = 'EGM2008'
TARGET_DATUM = 'EGM96'
LON0 = 6
LAT0_SOUTH = 49  # the tile south of the 50-degree longitude-spacing boundary
FINE = 300       # posts per degree of the southern tile
COARSE = 200     # posts per degree of longitude of the northern tile

# Minimum patch size of the self-test. The tiles are coarser than the level-2
# grid (one source post becomes 12 x 12 posts), so a low spot of one source
# post is a few hundred posts there; the limit keeps the rule in play.
MIN_PATCH_SIZE = 400

# The header values of the fictional product.
PRODUCT = {
    'dted_level': None,
    'security_code': 'U',
    'security_handling': 'SELF-TEST',
    'unique_ref_uhl': 'SELFTEST',
    'data_edition': 1,
    'match_merge_version': 'A',
    'producer_code': 'USNGA',
    'digitizing_system': 'SYNTHETIC',
    'compilation_date': '2026-01',
    'abs_horiz_acc': 10,
    'abs_vert_acc': 5,
    'rel_horiz_acc': 'NA',
    'rel_vert_acc': 3,
    'dsi_free_text': 'EGMTrans DTED self-test: synthetic data, not terrain.',
}

# SHA-256 of the header and of the record block of every output, pinned on
# the reference host. Regenerate with ``egmtrans dted-selftest --print-reference``
# whenever the conversion is meant to change.
REFERENCE: dict[str, tuple[str, str]] = {
    'N49E006.dt2': (
        '0d1ceba1e744b40dc83c5280eac94fb98910586cc80fb119819527f34613e9bb',
        'e9ad50fdf1f9d43b3b23ecc456655ee1237a18f13aef821ded818adfdad5c16c',
    ),
    'N49E006.dt1': (
        '40802b415759d5dbb5f620f0c1224441b072bf63a3ba8b25139ec0461f2d9987',
        'c8fc6060ffa831eb6d83d5a95094fb8c12d1d1508f45780645a5b3a5d05aa07b',
    ),
    'N49E006.dt0': (
        '1f5ed7dc0b3fbd1eeff5f95915b9d22dc6b8cbe16f3a2ab64c32f7959a4bdfbd',
        'b883635a167118f605ccc3183297827834accf5982274affd2724944c996fd42',
    ),
    'N50E006.dt2': (
        '1dd65c04c5a67e34c97922595bc07a1b2395f2fde8a6f1cbd5a79931e7ad3fbb',
        '3e6f7d3befac435fbbcc98bf480222630358e18eeb118ec0d84d18530e5347e7',
    ),
    'N50E006.dt1': (
        '057047ead43c1988022428d4cb568c50c6abb1670d7b7258ab5fe28674cf24d1',
        '0e797efaa6994e718ba16a650688339df0167bdacdbc98c6b209ae71802e92e0',
    ),
    'N50E006.dt0': (
        '5087d34b501f10c07b296cf6ed88fcb9665c382ddf629aa59ded953c98036398',
        '48b881f244b13e019649dd2a808ca71b35af02e47343757d77b525f1f2fb3204',
    ),
}


def _terrain(rows: int, cols: int, base_cm: int) -> np.ndarray:
    """Heights in whole centimeters from an integer formula: no two 4-neighbors alike."""
    i, j = np.mgrid[0:rows, 0:cols]
    return base_cm + ((i * 7 + j * 13) % 997) * 3


def northern_tile() -> np.ndarray:
    """The tile north of the boundary: COARSE x FINE posts per degree, Float32 heights."""
    rows, cols = FINE + 1, COARSE + 1
    cm = _terrain(rows, cols, 30000)
    cm[40:120, 60:140] = 26000          # a lake at 260.00 m
    cm[120, 100] = 25950                # a low post behind ...
    cm[121, 100] = 26050                # ... a one-post dam
    cm[250:290, 20:60] = 20000          # a lower lake
    cm[np.arange(120, 250), np.arange(60, 190)[:130] % cols] = 25900  # a channel toward it: an outlet
    cm[10:35, 150:175] = 90000          # a flat hilltop: a plateau that is not water
    cm[:, 0] = 0                        # ocean along the west edge
    cm[:, 1] = -1                       # a centimeter under it: ocean too
    cm[200, 2] = -5                     # a post under the sea, enclosed by the land east of it
    cm[260:270, 160:170] = -2147483648  # a void block (set below)
    heights = (cm / 100).astype(np.float32)
    heights[cm == -2147483648] = np.nan
    return heights


def southern_tile(north: np.ndarray) -> np.ndarray:
    """The tile south of the boundary: FINE x FINE posts per degree, with its
    north row a nearest-neighbor copy of the northern tile's south row."""
    rows, cols = FINE + 1, FINE + 1
    cm = _terrain(rows, cols, 40000)
    cm[100:200, 120:220] = 35000        # a lake at 350.00 m
    cm[200, 170] = 34960                # a low post behind ...
    cm[201, 170] = 35050                # ... a one-post dam
    cm[99, 150] = 34800                 # a post on its north shore, 2 m down, enclosed
    cm[80:95, 20:40] = 35001            # a flat area a centimeter above the lake's height, elsewhere
    cm[:, -1] = 0                       # ocean along the east edge
    cm[150:160, -2] = -3                # posts under the sea beside it
    heights = (cm / 100).astype(np.float32)
    numerator, denominator = BAND_EDGE_STEP[50]
    coarse_per_degree = FINE * numerator // denominator
    assert coarse_per_degree == COARSE
    i = np.arange(cols)
    nearest = (2 * i * numerator + denominator) // (2 * denominator)  # nearest coarse post, up on a tie
    heights[0] = north[-1][np.minimum(nearest, COARSE)]
    return heights


def write_tile(path: str, heights: np.ndarray, lon0: int, lat0: int, per_degree_x: int, per_degree_y: int) -> str:
    """Write a pixel-is-point Float32 GeoTIFF on the whole-degree lattice."""
    rows, cols = heights.shape
    px, py = 1.0 / per_degree_x, 1.0 / per_degree_y
    driver = gdal.GetDriverByName('GTiff')
    ds = driver.Create(path, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((lon0 - px / 2, px, 0.0, lat0 + 1 + py / 2, 0.0, -py))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.SetMetadataItem('AREA_OR_POINT', 'Point')
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-32767.0)
    band.WriteArray(np.where(np.isnan(heights), np.float32(-32767.0), heights))
    band = None
    ds = None
    return path


def metadata_for(level: int) -> DtedMetadataSource:
    product = dict(PRODUCT)
    product['dted_level'] = level
    profile = Profile(path='<built-in self-test profile>', product=product, harvest=HarvestConfig())
    return DtedMetadataSource(None, profile)


def file_hashes(path: str) -> tuple[str, str]:
    """SHA-256 of the header and of the record block of a DTED file."""
    with open(path, 'rb') as handle:
        content = handle.read()
    return hashlib.sha256(content[:HEADER_LENGTH]).hexdigest(), hashlib.sha256(content[HEADER_LENGTH:]).hexdigest()


@dataclass
class SelfTestItem:
    name: str
    header: str
    records: str
    expected: tuple[str, str] | None

    @property
    def ok(self) -> bool:
        return self.expected is not None and (self.header, self.records) == self.expected


@dataclass
class SelfTestResult:
    items: list[SelfTestItem] = field(default_factory=list)
    folder: str = ''

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(item.ok for item in self.items)


def run_selftest(folder: str | None = None, *, keep: bool = False) -> SelfTestResult:
    """Build the tiles, convert them and compare the hashes.

    *folder* is where the files go (a temporary folder when None, removed
    unless *keep*). The geoid grids must be present.
    """
    from egmtrans.transform import transform_vertical_datum

    temporary = folder is None
    folder = folder or tempfile.mkdtemp(prefix='egmtrans_selftest_')
    os.makedirs(folder, exist_ok=True)
    result = SelfTestResult(folder=folder)
    try:
        north = northern_tile()
        south = southern_tile(north)
        tiles = {
            (LON0, LAT0_SOUTH): write_tile(os.path.join(folder, 'south.tif'), south, LON0, LAT0_SOUTH, FINE, FINE),
            (LON0, LAT0_SOUTH + 1): write_tile(os.path.join(folder, 'north.tif'), north, LON0, LAT0_SOUTH + 1,
                                                COARSE, FINE),
        }
        for (lon0, lat0), path in tiles.items():
            for level in (2, 1, 0):
                cell = CellGeometry(level, lon0, lat0)
                name = f'{cell.cell_id}.dt{level}'
                output = os.path.join(folder, name)
                transform_vertical_datum(
                    path, output, SOURCE_DATUM, TARGET_DATUM, True, False, MIN_PATCH_SIZE, 'bilinear', None, False,
                    dted_metadata=metadata_for(level), cell=cell,
                )
                header, records = file_hashes(output)
                result.items.append(SelfTestItem(name, header, records, REFERENCE.get(name)))
    finally:
        if temporary and not keep:
            shutil.rmtree(folder, ignore_errors=True)
    return result


def reference_text(result: SelfTestResult) -> str:
    """The REFERENCE mapping for the hashes of *result*, as Python source."""
    lines = ['REFERENCE: dict[str, tuple[str, str]] = {']
    for item in result.items:
        lines.append(f"    '{item.name}': (")
        lines.append(f"        '{item.header}',")
        lines.append(f"        '{item.records}',")
        lines.append('    ),')
    lines.append('}')
    return '\n'.join(lines)


def metadata_object(level: int) -> DtedMetadata:
    """The per-cell metadata of the fictional product, for callers that want it directly."""
    return metadata_for(level).for_cell('N00E000')
