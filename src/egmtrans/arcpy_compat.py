"""ArcPy-specific helpers — isolated so the rest of the package never imports arcpy directly.

These functions are only called when ``arc_mode`` is True (i.e. running inside
ArcGIS Pro).  They use core ArcPy functionality only — no Spatial Analyst or
other licensed extensions are required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from egmtrans import _state

if TYPE_CHECKING:
    import arcpy  # type: ignore


def init_arcpy() -> None:
    """Import arcpy and store it in runtime state. Safe to call multiple times."""
    try:
        import arcpy  # type: ignore
        arcpy.env.overwriteOutput = True
        _state.set_arcpy(arcpy)
    except ImportError:
        pass


def create_compound_srs_arcpy(
    spatial_ref: arcpy.SpatialReference, datum: str
) -> arcpy.SpatialReference:
    """Create a compound spatial reference using ArcPy by adding a vertical coordinate system."""
    arcpy = _state.get_arcpy()
    compound_srs = spatial_ref.clone()

    if datum == 'WGS84':
        compound_srs.VCS = None
    elif datum == 'EGM96':
        vertical_cs = arcpy.SpatialReference(5773)
        compound_srs.VCS = vertical_cs.VCS
    elif datum == 'EGM2008':
        vertical_cs = arcpy.SpatialReference(3855)
        compound_srs.VCS = vertical_cs.VCS
    else:
        raise ValueError(f'Unsupported vertical datum: {datum}')

    return compound_srs


def batch_project_points_arcpy(
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    in_srs: arcpy.SpatialReference,
    out_srs: arcpy.SpatialReference,
) -> tuple[np.ndarray, np.ndarray]:
    """Batch project points between spatial references using core ArcPy (no extensions)."""
    arcpy = _state.get_arcpy()
    points = np.array([arcpy.Point(x, y) for x, y in zip(x_coords, y_coords)])
    geometries = [arcpy.PointGeometry(pt, in_srs) for pt in points]
    projected = [geom.projectAs(out_srs) for geom in geometries]
    x_proj = np.array([pt.centroid.X for pt in projected], dtype=np.float64)
    y_proj = np.array([pt.centroid.Y for pt in projected], dtype=np.float64)
    return x_proj, y_proj
