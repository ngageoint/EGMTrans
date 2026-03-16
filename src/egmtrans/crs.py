"""CRS / Spatial Reference System utilities.

Functions for extracting, inspecting, and constructing GDAL/OGR
SpatialReference objects.  Handles compound CRS (horizontal + vertical),
geographic and projected systems, and EPSG identification.
"""

from __future__ import annotations

import os

from osgeo import osr

from egmtrans import _state
from egmtrans.config import DATUM_MAPPING


def get_proj4(srs: osr.SpatialReference, grid: str | None = None) -> str:
    """Generate a PROJ.4 string for the given spatial reference system.

    Args:
        srs: The input spatial reference system.
        grid: Full path to the geoid grid file.  If provided, any
            ``+geoidgrids=<basename>`` token in the PROJ string is replaced
            with the full path so that GDAL can locate the grid.

    Returns:
        PROJ.4 string representation of the spatial reference system.
    """
    proj4 = srs.ExportToProj4()
    if grid:
        proj4 = proj4.replace(
            f'+geoidgrids={os.path.basename(grid)}',
            f'+geoidgrids={grid}',
        )
    return proj4


def get_horizontal_srs(srs: osr.SpatialReference) -> osr.SpatialReference:
    """Extract the horizontal component of a spatial reference system.

    Handles compound CRS (tries EPSG extraction first, falls back to WKT
    export), standalone projected/geographic CRS (cloned as-is), and
    other types (re-imported via WKT2).

    Args:
        srs: The input spatial reference system.

    Returns:
        The horizontal component of the input SRS.

    Raises:
        ValueError: If the horizontal CRS cannot be extracted from a compound CRS.
    """
    horiz_srs = osr.SpatialReference()

    if srs.IsCompound():
        proj_epsg = srs.GetAuthorityCode('COMPD_CS|PROJCS')
        geog_epsg = srs.GetAuthorityCode('COMPD_CS|GEOGCS')
        if proj_epsg:
            horiz_srs.ImportFromEPSG(int(proj_epsg))
        elif geog_epsg:
            horiz_srs.ImportFromEPSG(int(geog_epsg))
        else:
            try:
                horiz_wkt = srs.ExportToWkt(['FORMAT=WKT2_2019', 'MULTILINE=NO']).split('\n')[0]
                horiz_srs.ImportFromWkt(horiz_wkt)
            except Exception as exc:
                raise ValueError("Could not extract horizontal CRS from compound CRS") from exc
    elif srs.IsProjected() or srs.IsGeographic():
        horiz_srs = srs.Clone()
    else:
        horiz_srs.ImportFromWkt(srs.ExportToWkt(['FORMAT=WKT2_2019']))

    return horiz_srs


def get_horizontal_epsg(srs: osr.SpatialReference) -> int:
    """Get the EPSG code of the horizontal component of a spatial reference system.

    Handles compound, geographic, and projected CRS types.

    Args:
        srs: The input spatial reference system.

    Returns:
        EPSG code of the horizontal component.

    Raises:
        ValueError: If the horizontal EPSG code cannot be determined.
    """
    if srs.IsCompound():
        epsg = get_horizontal_epsg(get_horizontal_srs(srs))
    elif srs.IsGeographic():
        epsg = srs.GetAuthorityCode('GEOGCS')
    elif srs.IsProjected():
        epsg = srs.GetAuthorityCode('PROJCS')
    else:
        raise ValueError("Could not extract horizontal EPSG code")
    return int(epsg)


def get_horizontal_name(srs: osr.SpatialReference) -> str:
    """Get the name of the horizontal component of a spatial reference system."""
    if srs.IsCompound():
        return (
            srs.GetAttrValue('COMPD_CS|GEOGCS')
            or srs.GetAttrValue('COMPD_CS|PROJCS')
            or "Unknown horizontal CRS"
        )
    elif srs.IsGeographic():
        return srs.GetAttrValue('GEOGCS')
    elif srs.IsProjected():
        return srs.GetAttrValue('PROJCS')
    else:
        return "Unknown horizontal CRS"


def standardize_srs(wkt: str) -> osr.SpatialReference:
    """Standardize a WKT string to a clean, EPSG-based SpatialReference.

    Imports the WKT, attempts to auto-identify its EPSG code, and
    re-imports from that code to produce a canonical object.  This avoids
    issues with non-standard or vendor-specific WKT variants.

    Args:
        wkt: The WKT string to standardize.

    Returns:
        A standardized spatial reference object (EPSG-based when possible,
        otherwise the original WKT is preserved).
    """
    logger = _state.get_logger()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    logger.debug(f"Original WKT: {wkt}")
    logger.debug(f"Original EPSG: {srs.GetAuthorityCode(None)}")
    logger.debug(f"Original Name: {get_horizontal_name(srs)}")
    logger.debug(f"SRS Name: {srs}")

    if srs.AutoIdentifyEPSG() == 0:
        epsg_code = srs.GetAuthorityCode(None)
        if epsg_code:
            logger.info(f"Successfully identified EPSG:{epsg_code} from WKT.")
            clean_srs = osr.SpatialReference()
            clean_srs.ImportFromEPSG(int(epsg_code))
            return clean_srs

    logger.warning("Could not standardize CRS to an EPSG code. Proceeding with original WKT.")
    return srs


def create_compound_srs(srs: osr.SpatialReference, datum: str) -> osr.SpatialReference:
    """Create a compound SRS by combining a horizontal SRS with a vertical datum.

    For WGS84, returns EPSG:4979 (3D geographic) when the input is geographic,
    or the horizontal SRS unchanged when projected (a 3D ellipsoid cannot serve
    as a vertical CRS in a compound CRS).  For EGM96/EGM2008, manually
    constructs a ``COMPD_CS`` WKT string for maximum compatibility across
    GDAL versions.

    Args:
        srs: The source spatial reference system.
        datum: Target vertical datum name (``'WGS84'``, ``'EGM96'``, or ``'EGM2008'``).

    Returns:
        The compound (or 3D) spatial reference system.
    """
    logger = _state.get_logger()
    horiz_srs = get_horizontal_srs(srs)

    if datum == 'WGS84':
        if horiz_srs.IsGeographic():
            geod_srs = osr.SpatialReference()
            geod_srs.ImportFromEPSG(4979)
            return geod_srs
        else:
            return horiz_srs

    vert_srs = osr.SpatialReference()
    vert_epsg = DATUM_MAPPING[datum]['epsg']
    if vert_epsg:
        vert_srs.ImportFromEPSG(int(vert_epsg))

    horiz_wkt = horiz_srs.ExportToWkt(['FORMAT=WKT2_2019', 'MULTILINE=NO'])
    vert_wkt = vert_srs.ExportToWkt(['FORMAT=WKT2_2019', 'MULTILINE=NO'])

    horiz_name = get_horizontal_name(horiz_srs)
    vert_name = vert_srs.GetAttrValue("VERT_CS")
    compound_name = f'{horiz_name} + {vert_name}'
    compound_wkt = f'COMPD_CS["{compound_name}",{horiz_wkt},{vert_wkt}]'

    compound_srs = osr.SpatialReference()
    try:
        logger.debug(
            f"Attempting to create compound SRS with:\n"
            f"- HORIZONTAL WKT: {horiz_wkt}\n"
            f"- VERTICAL WKT: {vert_wkt}\n"
            f"- COMPOUND WKT: {compound_wkt}"
        )
        compound_srs.ImportFromWkt(compound_wkt)
        logger.debug("Successfully imported compound WKT.")
    except Exception as e:
        logger.error(f"Failed to import compound WKT: {e}")
        raise

    return compound_srs
