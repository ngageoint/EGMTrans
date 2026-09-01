"""Core orchestration — datum array creation and vertical datum transformation.

This module ties together CRS construction, geoid grid clipping, interpolation,
flat-area processing, and output writing to perform a complete vertical datum
transformation of a GeoTIFF or DTED file.
"""

from __future__ import annotations

import os
import shutil
import time
from secrets import token_hex

import numpy as np
from osgeo import gdal, osr

from egmtrans import _state
from egmtrans.arcpy_compat import batch_project_points_arcpy
from egmtrans.config import BASE_PATH, DATUM_MAPPING, DTED_EXTENSIONS, DTED_NODATA
from egmtrans.crs import create_compound_srs, get_proj4
from egmtrans.file_utils import copy_as_writable
from egmtrans.flattening import (
    create_flat_mask,
    create_labeled_array_flt,
    create_labeled_array_int,
    process_patches,
    process_patches_arcpy,
)
from egmtrans.interpolation import bilinear_interpolation, delaunay_triangulation, spline_interpolation
from egmtrans.io import apply_scale_factor, restore_nodata, update_dted_header


def create_gdal_warp_array(
    input_file: str,
    src_srs: osr.SpatialReference,
    src_datum: str,
    tgt_srs: osr.SpatialReference,
    tgt_datum: str,
    temp_dir: str,
    base_name: str,
    data_type: int,
) -> np.ndarray:
    """Perform vertical datum transformation using GDAL Warp.

    Equivalent to bilinear interpolation but driven by the PROJ pipeline.
    **Not supported in ArcGIS Pro** because Esri's bundled PROJ library lacks
    the grid-based vertical transformation operations that GDAL Warp requires.
    Uses nearest-neighbour resampling to avoid jagged NoData edge artifacts.

    Args:
        input_file: Path to the input DEM.
        src_srs: Source compound spatial reference system.
        src_datum: Source vertical datum name.
        tgt_srs: Target compound spatial reference system.
        tgt_datum: Target vertical datum name.
        temp_dir: Directory for temporary files.
        base_name: Base filename for temporary outputs.
        data_type: GDAL data type constant (e.g. ``gdal.GDT_Float32``).

    Returns:
        Transformed elevation array.

    Raises:
        ValueError: If called in ArcGIS Pro mode.
        RuntimeError: If GDAL Warp fails.
    """
    logger = _state.get_logger()

    if _state.get_arc_mode():
        err = "GDAL's Warp function is not supported in ArcGIS Pro. Update script to use spline interpolation."
        logger.error(err)
        raise ValueError(err)

    try:
        src_grid_filename = DATUM_MAPPING[src_datum]['grid']
        src_grid = os.path.join(BASE_PATH, 'datums', src_grid_filename) if src_grid_filename else None
        tgt_grid_filename = DATUM_MAPPING[tgt_datum]['grid']
        tgt_grid = os.path.join(BASE_PATH, 'datums', tgt_grid_filename) if tgt_grid_filename else None
        src_proj = get_proj4(src_srs, src_grid)
        tgt_proj = get_proj4(tgt_srs, tgt_grid)
        warp_options = gdal.WarpOptions(
            format='GTiff',
            srcSRS=src_proj,
            dstSRS=tgt_proj,
            resampleAlg=gdal.GRA_NearestNeighbour,
            multithread=True,
            dstNodata=DTED_NODATA if data_type == gdal.GDT_Int16 else np.nan,
            transformerOptions=['VERIFY_GRID=TRUE', 'GRID_CHECK_WITH_PROJ4=TRUE'],
        )
        warp_file = os.path.join(temp_dir, f'{base_name}_warp.tif')
        warp_result = gdal.Warp(warp_file, input_file, options=warp_options)
        # Flush and close before reopening from disk below, or the reopen races
        # an unflushed write.
        warp_result.Close()
        logger.info("Transformed vertical datum with GDAL's Warp function.")
    except Exception as e:
        logger.error(f"Unexpected error during GDAL's Warp function: {str(e)}")
        raise

    with gdal.Open(warp_file, gdal.GA_ReadOnly) as warp_ds:
        return warp_ds.GetRasterBand(1).ReadAsArray()


def create_interp_array(
    input_array: np.ndarray,
    input_file: str,
    src_datum: str,
    tgt_datum: str,
    algorithm: str,
    temp_dir: str,
    output_dir: str,
) -> np.ndarray:
    """Perform vertical datum transformation using interpolation.

    Handles all four datum-combination scenarios:

    1. **Both non-WGS84** — computes ``delta = tgt_grid - src_grid``.
    2. **Source is WGS84** — uses the target datum grid directly.
    3. **Target is WGS84** — negates the source datum grid.
    4. **Both WGS84** — returns the input array unchanged.

    The delta is subtracted from the input elevations to produce the
    transformed output.

    Args:
        input_array: Input elevation array.
        input_file: Path to the input file (for georeferencing).
        src_datum: Source datum name.
        tgt_datum: Target datum name.
        algorithm: Interpolation algorithm (``'bilinear'``, ``'delaunay'``,
            ``'spline'``).
        temp_dir: Directory for temporary processing files.
        output_dir: Directory for output files.

    Returns:
        Transformed elevation array matching input dimensions.
    """
    src_array = None
    tgt_array = None
    if src_datum != 'WGS84':
        src_array = create_datum_array(input_file, src_datum, algorithm, temp_dir, output_dir)
    if tgt_datum != 'WGS84':
        tgt_array = create_datum_array(input_file, tgt_datum, algorithm, temp_dir, output_dir)

    delta_array = None
    if tgt_datum != 'WGS84':
        if src_datum != 'WGS84':
            if tgt_array is not None and src_array is not None:
                delta_array = tgt_array - src_array
        else:
            if tgt_array is not None:
                delta_array = tgt_array
    elif src_datum != 'WGS84':
        if src_array is not None:
            delta_array = 0 - src_array
    else:
        delta_array = None

    warp_array = input_array
    if delta_array is not None:
        warp_array = input_array - delta_array

    return warp_array


def create_datum_array(
    input_file: str, datum: str, algorithm: str, temp_dir: str, output_dir: str
) -> np.ndarray:
    """Create a resampled datum grid array matched to the input DEM extent/resolution.

    Workflow:
    1. Opens the input DEM and the corresponding geoid grid (from ``datums/``).
    2. Clips the geoid grid to the input extent plus a small buffer to
       ensure accurate interpolation at edges.
    3. Transforms coordinates if the input CRS is projected (the geoid
       grids are always geographic, EPSG:4326).
    4. Calls the selected interpolation algorithm to resample the grid.

    Supports both ArcPy and standalone GDAL code paths.

    Args:
        input_file: Path to the input DEM.
        datum: Vertical datum name (``'EGM96'`` or ``'EGM2008'``).
        algorithm: Interpolation algorithm name.
        temp_dir: Directory for temporary processing files.
        output_dir: Directory for output and optional verification files.

    Returns:
        2-D array of geoid undulation values at the input DEM's resolution.

    Raises:
        ValueError: If insufficient grid points are found for interpolation.
    """
    logger = _state.get_logger()
    arcpy = _state.get_arcpy()
    arc_mode = _state.get_arc_mode()

    try:
        with gdal.Open(input_file, gdal.GA_ReadOnly) as input_ds:
            input_gt = input_ds.GetGeoTransform()
            input_proj = input_ds.GetProjection()
            input_cols = input_ds.RasterXSize
            input_rows = input_ds.RasterYSize
        input_srs = osr.SpatialReference()
        input_col_width = input_gt[1]
        input_row_height = input_gt[5]
        datum_grid_filename = DATUM_MAPPING[datum]['grid']
        if not datum_grid_filename:
            raise ValueError(f"Datum grid not specified for datum: {datum}")
        datum_file = os.path.join(BASE_PATH, 'datums', datum_grid_filename)

        if arc_mode:
            input_raster = arcpy.Raster(input_file)
            input_srs = input_raster.spatialReference
            input_extent = (
                input_raster.extent.XMin,
                input_raster.extent.YMin,
                input_raster.extent.XMax,
                input_raster.extent.YMax,
            )
            datum_raster = arcpy.Raster(datum_file)
            datum_srs = datum_raster.spatialReference
            datum_col_width = datum_raster.meanCellWidth
            datum_row_height = abs(datum_raster.meanCellHeight)

            if input_srs.type != 'Geographic':
                sw_corner = arcpy.PointGeometry(arcpy.Point(input_extent[0], input_extent[1]), input_srs)
                ne_corner = arcpy.PointGeometry(arcpy.Point(input_extent[2], input_extent[3]), input_srs)
                spatial_ref = arcpy.SpatialReference(4326)
                sw_corner_geog = sw_corner.projectAs(spatial_ref)
                ne_corner_geog = ne_corner.projectAs(spatial_ref)
                min_lon = sw_corner_geog.centroid.X
                min_lat = sw_corner_geog.centroid.Y
                max_lon = ne_corner_geog.centroid.X
                max_lat = ne_corner_geog.centroid.Y
            else:
                min_lon, min_lat = input_extent[0], input_extent[1]
                max_lon, max_lat = input_extent[2], input_extent[3]
        else:
            input_srs.ImportFromWkt(input_proj)
            input_extent = (
                input_gt[0],
                input_gt[3] + input_row_height * input_rows,
                input_gt[0] + input_col_width * input_cols,
                input_gt[3],
            )
            with gdal.Open(datum_file, gdal.GA_ReadOnly) as datum_ds:
                datum_gt = datum_ds.GetGeoTransform()
                datum_proj = datum_ds.GetProjection()
            datum_srs = osr.SpatialReference(wkt=datum_proj)
            datum_col_width = datum_gt[1]
            datum_row_height = abs(datum_gt[5])

            input_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            datum_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

            if not input_srs.IsGeographic():
                transform = osr.CoordinateTransformation(input_srs, datum_srs)
                (min_lon, min_lat, _) = transform.TransformPoint(input_extent[0], input_extent[1])
                (max_lon, max_lat, _) = transform.TransformPoint(input_extent[2], input_extent[3])
            else:
                min_lon, min_lat = input_extent[0], input_extent[1]
                max_lon, max_lat = input_extent[2], input_extent[3]

        # Symmetric: bilinear needs one source cell beyond every query point, and
        # the query points are now centre-registered, so no side is a special case.
        buffered_extent = (
            min_lon - 1.5 * datum_col_width,
            min_lat - 1.5 * datum_row_height,
            max_lon + 1.5 * datum_col_width,
            max_lat + 1.5 * datum_row_height,
        )

        projwin = (
            buffered_extent[0],
            buffered_extent[3],
            buffered_extent[2],
            buffered_extent[1],
        )

        translate_options = gdal.TranslateOptions(
            format='GTiff',
            outputSRS='EPSG:4326',
            projWin=projwin,
            projWinSRS='EPSG:4326',
        )

        clipped_file = os.path.join(temp_dir, f'{datum}_clipped.tif')
        gdal.Translate(clipped_file, datum_file, options=translate_options)

        clipped_ds = gdal.Open(clipped_file)
        clipped_gt = clipped_ds.GetGeoTransform()
        clipped_band = clipped_ds.GetRasterBand(1)
        clipped_scale = clipped_band.GetScale() or 1
        clipped_data = clipped_band.ReadAsArray() * clipped_scale
        clipped_rows, clipped_cols = clipped_data.shape
        clipped_band = None
        clipped_ds = None

        x_res = clipped_gt[1]
        y_res = clipped_gt[5]
        x_start = clipped_gt[0] + 0.5 * x_res
        y_start = clipped_gt[3] + 0.5 * y_res

        x_coords = x_start + np.arange(clipped_cols) * x_res
        y_coords = y_start + np.arange(clipped_rows) * y_res

        x_grid, y_grid = np.meshgrid(x_coords, y_coords)
        x_flat = x_grid.flatten()
        y_flat = y_grid.flatten()
        z_flat = clipped_data.flatten()

        points = {'x': x_flat, 'y': y_flat, 'z': z_flat}

        if arc_mode:
            if hasattr(input_srs, 'type') and input_srs.type != 'Geographic':
                x_transformed, y_transformed = batch_project_points_arcpy(x_flat, y_flat, datum_srs, input_srs)
                points = {'x': x_transformed, 'y': y_transformed, 'z': z_flat}
        else:
            if not input_srs.IsGeographic():
                coords = np.vstack((x_flat, y_flat)).T
                transform = osr.CoordinateTransformation(datum_srs, input_srs)
                transformed_coords = np.array(transform.TransformPoints(coords))
                x_transformed = transformed_coords[:, 0]
                y_transformed = transformed_coords[:, 1]
                points = {'x': x_transformed, 'y': y_transformed, 'z': z_flat}

        if len(points['x']) < 4:
            raise ValueError(f"Not enough valid points for interpolation: {len(points['x'])} points found")

        # Cell centres, matching the geoid source points built above. Using the
        # extent corner offset every sample by half a DEM pixel.
        x = input_extent[0] + (np.arange(input_cols) + 0.5) * input_col_width
        y = input_extent[3] + (np.arange(input_rows) + 0.5) * input_row_height
        xx, yy = np.meshgrid(x, y)

        if len(points['x']) < 4:
            raise ValueError(f"Insufficient points for interpolation: {len(points['x'])} points found")

        if algorithm == 'bilinear':
            logger.info(f"Performing bilinear interpolation of the {datum} grid...")
            interp_array = bilinear_interpolation(points, xx, yy)
        elif algorithm == 'delaunay':
            logger.info(f"Performing Delaunay triangulation of the {datum} grid...")
            interp_array = delaunay_triangulation(points, xx, yy)
        else:
            logger.info(f"Performing thin plate spline interpolation of the {datum} grid...")
            interp_array = spline_interpolation(points, xx, yy)
        logger.info(f"Completed interpolation of the {datum} grid.")

        return interp_array

    except Exception as e:
        logger.error(f"Error in create_datum_array: {str(e)}")
        raise


def transform_vertical_datum(
    input_file: str,
    output_file: str,
    src_datum: str,
    tgt_datum: str,
    flatten: bool,
    create_mask: bool,
    min_patch_size: int,
    algorithm: str,
    abs_horiz_accuracy: int | None = None,
    save_log: bool = True,
) -> None:
    """Transform the vertical datum of a GeoTIFF or DTED elevation model.

    End-to-end workflow:
    1. Applies scale factor / offset correction if the input band has them.
    2. Performs the vertical datum shift (via interpolation or GDAL Warp).
    3. Optionally detects and flattens ocean / flat patches.
    4. Optionally writes a flat-area mask GeoTIFF.
    5. Saves the result — Cloud Optimized GeoTIFF (COG) with DEFLATE compression
       and compound CRS for GeoTIFF inputs, or an updated DTED file with the
       new vertical datum code written to the header.

    Args:
        input_file: Path to the input DEM file.
        output_file: Path for the transformed output.
        src_datum: Source vertical datum.
        tgt_datum: Target vertical datum.
        flatten: Whether to preserve flat areas during transformation.
        create_mask: Whether to write a flat-area mask file alongside the output.
        min_patch_size: Minimum pixel count for a flat area to be retained.
        algorithm: Interpolation algorithm name.
        abs_horiz_accuracy: Fallback horizontal accuracy for DTED headers.
        save_log: Whether to retain the log file (passed through for cleanup).

    Raises:
        ValueError: If the input data type is unsupported or CRS is missing.
        RuntimeError: If GDAL operations fail.
    """
    logger = _state.get_logger()
    arc_mode = _state.get_arc_mode()
    base_name = os.path.splitext(os.path.basename(input_file))[0]
    input_ds = None
    input_band = None
    temp_dir = None

    start_time = time.time()

    try:
        input_ds = gdal.Open(input_file, gdal.GA_ReadOnly)
        data_type = input_ds.GetRasterBand(1).DataType
        if data_type not in (gdal.GDT_Int16, gdal.GDT_Int32, gdal.GDT_Float32, gdal.GDT_Float64):
            raise ValueError(f'Unsupported data type: {data_type}')
        input_band = input_ds.GetRasterBand(1)
        input_nodata = input_band.GetNoDataValue()
        input_array = input_band.ReadAsArray()
        input_array = np.where(input_array == input_nodata, np.nan, input_array)
        gt = input_ds.GetGeoTransform()
        scale = input_band.GetScale() or 1
        offset = input_band.GetOffset() or 0
        nodata_value = input_band.GetNoDataValue()
        metadata = input_ds.GetMetadata()

        output_dir = os.path.dirname(os.path.abspath(output_file))
        temp_dir = os.path.join(output_dir, f'temp_{token_hex(8)}')
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

        src_srs = input_ds.GetSpatialRef()
        if src_srs is None:
            raise ValueError('Could not retrieve spatial reference from the input file')

        epsg_result = src_srs.AutoIdentifyEPSG()
        if epsg_result == 0:
            logger.info("Successfully identified EPSG code from input CRS.")
        else:
            logger.warning(
                "Could not automatically identify an EPSG code from the input CRS. "
                "Proceeding with the original WKT."
            )

        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        src_srs_compound = create_compound_srs(src_srs, src_datum)
        tgt_srs = create_compound_srs(src_srs, tgt_datum)

        is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
        if not is_dted:
            if scale != 1 or offset != 0:
                scaled_file = os.path.join(temp_dir, f'{base_name}_scaled.tif')
                input_file = apply_scale_factor(input_file, scaled_file, scale, offset, nodata_value)
                # Re-read: input_array still holds the raw, unscaled values, and the
                # interpolation algorithms work from the array rather than the file.
                with gdal.Open(input_file) as scaled_ds:
                    scaled_band = scaled_ds.GetRasterBand(1)
                    scaled_nodata = scaled_band.GetNoDataValue()
                    input_array = scaled_band.ReadAsArray()
                    scaled_band = None
                if scaled_nodata is None:
                    scaled_nodata = nodata_value
                if scaled_nodata is not None:
                    input_array = np.where(input_array == scaled_nodata, np.nan, input_array)
                logger.info(f'Preprocessed the input DEM with scale factor {scale} and offset {offset}.')
            else:
                logger.info('No scale factor or offset applied; using original elevation values.')

            if not arc_mode:
                nan_file = os.path.join(temp_dir, f'{base_name}_nan.xml')
                vrt_options = gdal.BuildVRTOptions(VRTNodata=np.nan)
                gdal.BuildVRT(nan_file, input_file, options=vrt_options)
                input_file = nan_file

        if src_datum == tgt_datum:
            logger.info('Updating GeoTIFF file with compound CRS and optimized compression...')
            warp_array = input_array
        else:
            logger.info(f'Starting vertical datum transformation from {src_datum} to {tgt_datum}...')

            if algorithm == 'proj':
                warp_array = create_gdal_warp_array(
                    input_file, src_srs_compound, src_datum, tgt_srs, tgt_datum,
                    temp_dir, base_name, data_type,
                )
            else:
                warp_array = create_interp_array(
                    input_array, input_file, src_datum, tgt_datum,
                    algorithm, temp_dir, output_dir,
                )

            if flatten:
                labeled_array = None
                is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
                if is_dted:
                    labeled_array = create_labeled_array_int(input_array)
                    logger.info('Mapped DTED ocean area(s).')
                else:
                    labeled_array = create_labeled_array_flt(input_array, min_patch_size)
                    logger.info(f'Mapped ocean and other flat areas > {min_patch_size} pixels.')

                if arc_mode:
                    warp_array = process_patches_arcpy(warp_array, labeled_array)
                else:
                    warp_array = process_patches(warp_array, labeled_array)
                logger.info('Flattened ocean and preserved other flat areas.')

                if create_mask:
                    mask_file = os.path.join(
                        os.path.dirname(output_file),
                        f'{os.path.splitext(os.path.basename(output_file))[0]}_mask.tif',
                    )
                    create_flat_mask(labeled_array, mask_file, input_ds)
                    logger.info(f'Created flat mask: {mask_file}')

        if output_file.lower().endswith(DTED_EXTENSIONS):
            logger.info(f'Updating vertical datum to {tgt_datum}...')
            # copy_as_writable, not shutil.copy: the latter carries the source's
            # read-only bit onto the copy and silently redirects into a directory.
            copy_as_writable(input_file, output_file)

            # DTED bands are Int16 and GDAL writes NaN as 0, so voids must be
            # restored to -32767 or they come out of the transform at sea level.
            dted_nodata = input_nodata if input_nodata is not None else DTED_NODATA
            with gdal.Open(output_file, gdal.GA_Update) as final_ds:
                band = final_ds.GetRasterBand(1)
                band.WriteArray(restore_nodata(warp_array, dted_nodata))
                band.FlushCache()
                band = None

            update_dted_header(output_file, tgt_datum, abs_horiz_accuracy)
        else:
            logger.info('Setting the compound CRS, optimizing compression, and saving as Cloud Optimized GeoTIFF...')
            warp_array = np.round(warp_array, 2)

            metadata['AREA_OR_POINT'] = 'Point'

            final_temp_file = os.path.join(temp_dir, f'{base_name}_final_temp.tif')
            driver = gdal.GetDriverByName('GTiff')
            final_ds = driver.Create(
                final_temp_file, input_ds.RasterXSize, input_ds.RasterYSize, 1, gdal.GDT_Float32,
            )
            final_ds.SetGeoTransform(gt)
            final_ds.SetProjection(tgt_srs.ExportToWkt(['FORMAT=WKT2_2019']))
            final_band = final_ds.GetRasterBand(1)
            final_band.WriteArray(warp_array)
            final_band.SetNoDataValue(np.nan)
            final_ds.SetMetadata(metadata)
            final_band.FlushCache()
            final_ds = None

            translate_options = gdal.TranslateOptions(
                format='COG',
                stats=True,
                creationOptions=[
                    'COMPRESS=DEFLATE',
                    'PREDICTOR=2',
                    'GEOTIFF_VERSION=1.1',
                    'BIGTIFF=IF_SAFER',
                    'NUM_THREADS=ALL_CPUS',
                ],
            )
            cog_ds = gdal.Translate(output_file, final_temp_file, options=translate_options)
            cog_ds.Close()

        elapsed_time = time.time() - start_time
        if elapsed_time < 60:
            time_str = f"{elapsed_time:.1f} seconds"
        else:
            minutes = elapsed_time / 60
            time_str = f"{minutes:.1f} minutes"
        logger.info(f'Total processing time: {time_str}')

        aux_file = output_file + '.aux.xml'
        if os.path.exists(aux_file):
            os.remove(aux_file)

        logger.info(f'Transformed file: {output_file}')
        logger.info(f'\n{"=" * 80}\n')
    except Exception as e:
        logger.error(f'An error occurred: {str(e)}')
        raise
    finally:
        # input_band holds a reference to input_ds, so both must go for the
        # dataset to actually close. Clean the scratch directory here too, or a
        # failed run leaves temp_<hex>/ behind in the user's output folder.
        input_band = None
        input_ds = None
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(temp_dir):
                logger.warning(f'Could not remove temporary directory: {temp_dir}')
