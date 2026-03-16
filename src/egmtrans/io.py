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


def update_dted_header(output_file: str, tgt_datum: str, abs_horiz_accuracy: int | None = None) -> None:
    """Update DTED file header with new vertical datum and accuracy information.

    Opens the file in binary mode and overwrites specific bytes per
    **STANAG 3809** (MIL-PRF-89020B):

    - **Vertical datum** (DSI record, offset 221): set to the 3-char DTED
      code from ``DATUM_MAPPING`` (e.g. ``E96``).
    - **Accuracy fields** (ACC record, offsets 731-746): four 4-byte fields
      for absolute/relative horizontal/vertical accuracy.  Existing numeric
      values are preserved.  Non-numeric values (e.g. ``'NA  '``) are
      standardised to right-justified ``'  NA'``.  For absolute horizontal
      accuracy, the *abs_horiz_accuracy* parameter is used as a fallback if
      the existing value is non-numeric.

    Args:
        output_file: Path to the DTED file to update.
        tgt_datum: Target vertical datum (``'EGM96'`` or ``'EGM2008'``).
        abs_horiz_accuracy: Fallback horizontal accuracy (0-9999 m) applied
            only when the existing field is non-numeric.

    Raises:
        OSError: If the file cannot be opened or written to.
        ValueError: If the target datum is not supported.
    """
    logger = _state.get_logger()

    DSI_POS = 80
    ACC_POS = 728
    VERT_DATUM_POS = DSI_POS + 141

    accuracy_fields = {
        'Abs. Horizontal Accuracy': {'pos': ACC_POS + 3, 'len': 4},
        'Abs. Vertical Accuracy':   {'pos': ACC_POS + 7, 'len': 4},
        'Rel. Horizontal Accuracy': {'pos': ACC_POS + 11, 'len': 4},
        'Rel. Vertical Accuracy':   {'pos': ACC_POS + 15, 'len': 4},
    }

    try:
        with open(output_file, 'r+b') as f:
            dted_code = DATUM_MAPPING.get(tgt_datum, {}).get('dted_code')
            if dted_code:
                f.seek(VERT_DATUM_POS)
                original_datum = f.read(3).decode('ascii')
                f.seek(VERT_DATUM_POS)
                f.write(dted_code.encode('ascii'))
                logger.info('Changes to vertical datum and accuracy fields in DTED header:')
                log_msg = f"    Vertical Datum Code: '{original_datum}' -> '{dted_code}'"
                if original_datum == dted_code:
                    log_msg += " (no change)"
                logger.info(log_msg)
            else:
                logger.warning(f"No DTED code found for datum {tgt_datum}. Vertical datum in header not updated.")

            for name, details in accuracy_fields.items():
                f.seek(details['pos'])
                original_value = f.read(details['len']).decode('ascii')
                new_value = original_value

                is_numeric = original_value.strip().isdigit()

                if not is_numeric:
                    if name == 'Abs. Horizontal Accuracy':
                        if abs_horiz_accuracy is not None:
                            if 0 <= abs_horiz_accuracy <= 9999:
                                new_value = str(abs_horiz_accuracy).zfill(4)
                            else:
                                logger.warning(
                                    "Absolute Horizontal Accuracy must be between 0 and 9999. Value not updated."
                                )
                        else:
                            new_value = '  NA'
                    else:
                        new_value = '  NA'

                if new_value != original_value:
                    f.seek(details['pos'])
                    f.write(new_value.encode('ascii'))

                log_msg = f"    {name}: '{original_value}' -> '{new_value}'"
                if original_value == new_value:
                    log_msg += " (no change)"
                logger.info(log_msg)

    except OSError as e:
        logger.error(f"Failed to write to DTED header for {output_file}: {e}")
        raise
    except KeyError as exc:
        raise ValueError(f"Unsupported target datum for DTED: {tgt_datum}") from exc
