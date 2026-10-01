"""GeoTIFF and DTED I/O utilities.

Handles writing transformed arrays to GeoTIFF, applying scale/offset
corrections, updating DTED file headers per STANAG 3809, and exporting
interpolation points to GeoJSON for verification.
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np
from osgeo import gdal

from egmtrans import _state
from egmtrans.config import DATUM_MAPPING
from egmtrans.dted.header import read_header, write_header
from egmtrans.dted.validate import validate_header
from egmtrans.dted.writer import DerivedFields, DtedMetadata, assemble_header, cell_of_header, describe_changes


def apply_scale_factor(
    input_file: str, scaled_file: str, scale: float, offset: float, nodata_value: float
) -> str:
    """Apply scale factor and offset to produce a file with true elevation values.

    Creates a Float32 copy of the input, applies ``data = data * scale + offset``,
    and resets the band's scale/offset metadata to 1.0/0.0 so downstream code
    can treat pixel values as real elevations.

    Args:
        input_file: Path to the input raster.
        scaled_file: Path for the corrected output (Float32 GeoTIFF).
        scale: Scale factor to apply.
        offset: Offset to apply.
        nodata_value: NoData value to preserve during the operation.

    Returns:
        Path to the corrected file (*scaled_file*).
    """
    gdal.Translate(scaled_file, input_file, format='GTiff', options=['-ot', 'Float32'])

    with gdal.Open(scaled_file, gdal.GA_Update) as scaled_ds:
        band = scaled_ds.GetRasterBand(1)
        data = band.ReadAsArray()
        if nodata_value is not None:
            data = np.ma.masked_equal(data, nodata_value)
        data = data * scale + offset
        band.WriteArray(data.filled(nodata_value) if nodata_value is not None else data)
        band.SetScale(1.0)
        band.SetOffset(0.0)
        band.FlushCache()

    return scaled_file


def round_half_away(array: np.ndarray) -> np.ndarray:
    """Round to the nearest whole number, halves away from zero, as GDAL does
    when it writes a float array into an Int16 band.

    Rounding in EGMTrans rather than leaving it to the band write makes the
    DTED value explicit wherever it is compared with a water level (the
    containment count, the boundary report).  NaN passes through.
    """
    array = np.asarray(array, dtype=np.float64)
    return np.where(array >= 0, np.floor(array + 0.5), np.ceil(array - 0.5))


def restore_nodata(array: np.ndarray, nodata: float) -> np.ndarray:
    """Replace NaN with *nodata* before writing to an integer band.

    Upstream processing converts the input's nodata to NaN so that arithmetic
    propagates voids correctly.  GDAL, however, maps NaN to 0 when writing a
    float array into an integer band — which would turn DTED voids into sea
    level.  Integer arrays are passed through untouched.

    Args:
        array: The transformed elevation array, possibly containing NaN.
        nodata: The value voids should be written as (-32767 for DTED).

    Returns:
        An array with NaN replaced by *nodata*.
    """
    if not np.issubdtype(array.dtype, np.floating):
        return array
    return np.where(np.isnan(array), nodata, array)


def write_array_to_geotiff(
    array: np.ndarray,
    output_file: str,
    proj: str,
    gt: tuple[float, float, float, float, float, float],
) -> None:
    """Write a numpy array to a single-band Float32 GeoTIFF.

    Uses DEFLATE compression with PREDICTOR=2 and GeoTIFF 1.1 for
    modern CRS support.  BIGTIFF is enabled when needed.

    Args:
        array: 2-D array of elevation data.
        output_file: Destination path for the GeoTIFF.
        proj: WKT or PROJ string defining the spatial reference system.
        gt: GDAL geotransform tuple ``(origin_x, pixel_w, rot, origin_y, rot, pixel_h)``.
    """
    rows, cols = array.shape
    driver = gdal.GetDriverByName('GTiff')
    output_ds = driver.Create(
        output_file, cols, rows, 1, gdal.GDT_Float32,
        options=['COMPRESS=DEFLATE', 'PREDICTOR=2', 'GEOTIFF_VERSION=1.1', 'BIGTIFF=IF_SAFER'],
    )
    output_ds.SetGeoTransform(gt)
    output_ds.SetProjection(proj)

    output_band = output_ds.GetRasterBand(1)
    output_band.WriteArray(array)
    output_band = None
    output_ds = None

    logging.info(f'Interpolated grid written to {output_file}.')


def write_points_to_geojson(points: dict[str, np.ndarray], datum: str, output_dir: str) -> None:
    """Write interpolation source points to a GeoJSON file for verification.

    Creates ``{datum}_points.geojson`` so that the point distribution and
    z-values used during interpolation can be visually inspected in a GIS.

    Args:
        points: Dict with ``'x'``, ``'y'``, ``'z'`` arrays.
        datum: Datum name (used in the output filename).
        output_dir: Directory where the GeoJSON file will be saved.
    """
    output_file = os.path.join(output_dir, f'{datum}_points.geojson')

    features = []
    for x, y, z in zip(points['x'], points['y'], points['z']):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(x), float(y)]},
            "properties": {"z": float(z)},
        })

    feature_collection = {"type": "FeatureCollection", "features": features}

    with open(output_file, 'w') as f:
        json.dump(feature_collection, f, indent=4)

    logging.info(f'{datum} grid points written to {output_file}.')


def update_dted_header(
    output_file: str,
    tgt_datum: str,
    abs_horiz_accuracy: int | None = None,
    *,
    metadata: DtedMetadata | None = None,
) -> None:
    """Rewrite the header of a DTED file for its new vertical datum.

    The file's own header is the base. The cell's row in the metadata index
    and the product profile (*metadata*, when the run names them) override
    it field by field, and the fields the geometry and the target datum
    determine are written last; see :func:`egmtrans.dted.writer.assemble_header`
    for the precedence. Without index or profile the header changes only in:

    - the **vertical datum** (DSI 142-144): the code of *tgt_datum*,
      ``E96`` or ``E08``;
    - the **accuracies** (ACC 4-19 and UHL 29-32): a value that is neither
      0000-9999 nor NA becomes NA, and NA is left justified (``NA  ``) as
      MIL-PRF-89020B 3.13.5 requires; the UHL copy follows the ACC value;
    - **absolute horizontal accuracy**: *abs_horiz_accuracy* fills it only
      when it is NA;
    - bytes that are not printable (the NULs some writers leave) become blanks.

    Raises:
        OSError: If the file cannot be read or written.
        ValueError: If the target datum has no DTED code, or the header
            cannot be completed (:class:`~egmtrans.dted.writer.HeaderAssemblyError`).
    """
    logger = _state.get_logger()
    dted_code = DATUM_MAPPING.get(tgt_datum, {}).get('dted_code')
    if not dted_code:
        raise ValueError(f'Unsupported target datum for DTED: {tgt_datum}')

    base = read_header(output_file)
    cell = cell_of_header(base, os.path.splitext(output_file)[1])
    header, sources = assemble_header(
        cell, base=base, metadata=metadata, derived=DerivedFields(vertical_datum=dted_code),
        cli_abs_horiz_accuracy=abs_horiz_accuracy,
    )
    try:
        write_header(output_file, header)
    except OSError as e:
        logger.error(f'Failed to write the DTED header of {output_file}: {e}')
        raise

    logger.info(f'DTED header of cell {cell.cell_id} rewritten; fields that changed:')
    for line in describe_changes(base, header, sources):
        logger.info(line)
    # Problems the input header had and nothing corrected are carried over,
    # as they always were; they are reported so the producer can fix them.
    for issue in validate_header(header, extension=os.path.splitext(output_file)[1]):
        if issue.severity in ('error', 'warning'):
            logger.warning(f'DTED header: {issue}')
