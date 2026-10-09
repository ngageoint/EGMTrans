#!/usr/bin/env python3
# ******************************************************************************
# Project: EGMTrans
# Author: Eric Robeck
#
# Copyright (c) 2025, National Geospatial-Intelligence Agency
# Licensed under the MIT License
# ******************************************************************************

"""
EGMTrans ArcGIS Pro Toolbox

This Python toolbox (.pyt) provides an ArcGIS Pro interface for the EGMTrans
script. It allows users to perform vertical datum transformations directly
within the ArcGIS Pro environment, make standard DTED deliveries from GeoTIFF
tiles, and check, describe and complete them.
"""
import arcpy # type: ignore
from importlib import reload
import os
import re
import sys

# Add the directory containing EGMTrans.py to the Python path
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

import EGMTrans
reload(EGMTrans)  # refresh changes if the Python script was altered

# The DTED metadata of the parameters as they stand, kept between validation
# calls so the index and the profile are not reloaded on every edit of the
# dialog. One entry: the key is the files' paths and times, the level, the
# fallback accuracy and the override rows.
_METADATA_CACHE = {}

# Notes set while the parameters are updated (a value the dialog changed for
# the user) and shown as warnings when the messages are updated.
_NOTES = {}

DTED_EXTENSIONS = (".dt0", ".dt1", ".dt2")
GEOTIFF_EXTENSIONS = (".tif", ".tiff")

# The Output Format value list: the DTED level it stands for, or None for GeoTIFF.
FORMAT_LEVELS = {"DTED2": 2, "DTED1": 1, "DTED0": 0, "GeoTIFF": None}
DEFAULT_FORMAT = "DTED2"
ALL_DATUMS = ["WGS84", "EGM96", "EGM2008"]
DTED_DATUMS = ["EGM96"]  # DTED is written in EGM96 only (MIL-PRF-89020B 3.2.2)
ALL_ALGORITHMS = ["Bilinear Interpolation", "Thin Plate Spline", "Delaunay Triangulation"]
DTED_ALGORITHMS = ["Bilinear Interpolation"]
ALGORITHM_CODES = {
    "Bilinear Interpolation": "bilinear",
    "Thin Plate Spline": "spline",
    "Delaunay Triangulation": "delaunay",
}
# The DTED Output Naming value list, mapped to the command line's presets;
# the last entry takes its template from the Naming Template parameter.
NAMING_LABELS = {"DTED standard": "dted", "Cell name": "cell", "Input name": "stem"}
CUSTOM_NAMING = "Custom template"
DEFAULT_MIN_PATCH_SIZE = 16
DEFAULT_CONTAINMENT = 0.8
# The parameters that matter only when DTED is written, shown as one group.
DTED_CATEGORY = "DTED output"
DTED_GROUP = (
    "abs_horiz_accuracy", "dted_index", "dted_profile", "dted_naming", "dted_naming_template", "dted_overrides",
    "dted_summary",
)
WATER_GROUP = ("context_folder", "water_levels", "containment")
# ArcGIS Pro's browse dialog opens a raster on a double click and lists its
# bands; a band chosen there (N49.dt2\Band_1) stands for its file.
RASTER_BAND = re.compile(r"band_\d+", re.IGNORECASE)


def _by_name(parameters):
    """The parameters keyed by name, so an insertion never shifts an index."""
    return {parameter.name: parameter for parameter in parameters}


def _dataset_of(path):
    """*path* without a trailing raster band (N49.dt2\\Band_1 is N49.dt2), else *path* as it is."""
    if not path:
        return path
    head, tail = os.path.split(path.rstrip("\\/"))
    return head if RASTER_BAND.fullmatch(tail) and os.path.isfile(head) else path


def _input_path(parameter):
    """The file or folder an input parameter stands for: a raster layer's data source, a band's file."""
    value = parameter.value
    path = value.dataSource if hasattr(value, "dataSource") else parameter.valueAsText
    return _dataset_of(path)


def _show_dataset(parameter):
    """Put a band's file in the input box in place of the band, so the dialog shows what the tool reads."""
    text = parameter.valueAsText
    dataset = _dataset_of(text)
    if dataset != text:
        parameter.value = dataset


def _override_pairs(parameter):
    """The (field, value) pairs of the DTED Header Overrides table, blank rows left out."""
    pairs = []
    for row in parameter.values or []:
        cells = list(row) if isinstance(row, (list, tuple)) else [row]
        field_name = str(cells[0] if cells and cells[0] is not None else "").strip()
        value = cells[1] if len(cells) > 1 and cells[1] is not None else ""
        if field_name:
            pairs.append((field_name, str(value)))
    return pairs


def _file_key(path):
    if not path:
        return None
    try:
        return (path, os.path.getmtime(path))
    except OSError:
        return (path, None)


class MetadataFileError(Exception):
    """A DTED metadata file could not be loaded; *source* says which parameter it belongs to."""

    def __init__(self, source, error):
        super().__init__(str(error))
        self.source = source


def _load_metadata(index, profile, overrides):
    """The run's DTED metadata source, loading the index and the profile one
    at a time so a failure names the parameter it belongs to."""
    from egmtrans.dted.index import read_index
    from egmtrans.dted.profile import load_profile

    try:
        loaded_index = read_index(index) if index else None
    except (OSError, ValueError, RuntimeError) as e:
        raise MetadataFileError("dted_index", e) from e
    try:
        loaded_profile = load_profile(profile) if profile else None
    except (OSError, ValueError) as e:
        raise MetadataFileError("dted_profile", e) from e
    return EGMTrans.DtedMetadataSource(loaded_index, loaded_profile, overrides)


def _format_of(p):
    return p["output_format"].valueAsText or DEFAULT_FORMAT


def _level_of(p):
    return FORMAT_LEVELS.get(_format_of(p))


def _dted_metadata_for(p):
    """The run's DTED metadata (index, profile and overrides), its coverage of
    the header fields and its validation issues, for the parameters as they
    stand. Raises MetadataFileError, ValueError or OSError when a file or an
    override is wrong."""
    index = p["dted_index"].valueAsText or None
    profile = p["dted_profile"].valueAsText or None
    level = _level_of(p)
    abs_horiz = p["abs_horiz_accuracy"].value
    pairs = tuple(_override_pairs(p["dted_overrides"]))
    key = (_file_key(index), _file_key(profile), level, abs_horiz, pairs)
    if _METADATA_CACHE.get("key") == key:
        return _METADATA_CACHE["value"]
    overrides = EGMTrans.parse_overrides(pairs)
    source = _load_metadata(index, profile, overrides)
    issues = source.validate(level) if not source.empty else []
    coverage = source.coverage(level, abs_horiz) if not source.empty else None
    _METADATA_CACHE["key"] = key
    _METADATA_CACHE["value"] = (source, coverage, issues)
    return _METADATA_CACHE["value"]


def _version_on_disk():
    """The version the package on disk declares, read as text (no code is run)."""
    from egmtrans import _version

    with open(_version.__file__, encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', handle.read())
    return match.group(1) if match else None


def _warn_if_stale():
    """ArcGIS Pro reloads the shim, not the package: a session that loaded an
    earlier egmtrans would keep writing that version's bytes."""
    try:
        import egmtrans

        on_disk = _version_on_disk()
        if on_disk != egmtrans.__version__:
            arcpy.AddWarning(
                f"EGMTrans {egmtrans.__version__} is loaded in this session, but version {on_disk} "
                f"is on disk. Restart ArcGIS Pro to run the version on disk."
            )
    except Exception:
        pass


def _naming_value(p):
    """The --dted-naming value the dropdown and the template box stand for."""
    label = p["dted_naming"].valueAsText or next(iter(NAMING_LABELS))
    if label == CUSTOM_NAMING:
        return (p["dted_naming_template"].valueAsText or "").strip()
    return NAMING_LABELS.get(label, NAMING_LABELS["DTED standard"])


def _declared_datum(path):
    """What a single input file declares as its vertical datum, or None."""
    try:
        from egmtrans.cli import file_datum_of

        return file_datum_of(path)
    except Exception:
        return None


class Toolbox:
    def __init__(self):
        """Define the toolbox (the name of the toolbox is the name of the
        .pyt file)."""
        self.label = "EGM Transformation Tools"
        self.alias = "egmtrans"
        self.icon = "../img/icons/EGMTrans_32.png"

        # List of tool classes associated with this toolbox
        self.tools = [Tool, DtedHeaderReport, DtedSelfTest, DtedDmed]


class Tool:
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "EGMTrans Tool"
        self.description = (
            "Transform the vertical datum of DTED and GeoTIFF files between the WGS 84 ellipsoid, EGM96 and "
            "EGM2008, or make a standard EGM96 DTED delivery from GeoTIFF tiles."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define the tool parameters."""
        params = []

        input_param = arcpy.Parameter(
            displayName="Input File or Folder",
            name="input",
            datatype=["GPRasterLayer", "DEFile", "DEFolder"],
            parameterType="Required",
            direction="Input")
        params.append(input_param)

        output_param = arcpy.Parameter(
            displayName="Output File or Folder",
            name="output",
            datatype=["DEFile", "DEFolder"],
            parameterType="Required",
            direction="Output")
        params.append(output_param)

        output_format = arcpy.Parameter(
            displayName="Output Format",
            name="output_format",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        output_format.filter.list = list(FORMAT_LEVELS)
        output_format.value = DEFAULT_FORMAT
        params.append(output_format)

        source_datum = arcpy.Parameter(
            displayName="Source Datum",
            name="source_datum",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        source_datum.filter.list = list(ALL_DATUMS)
        params.append(source_datum)

        target_datum = arcpy.Parameter(
            displayName="Target Datum",
            name="target_datum",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        target_datum.filter.list = list(DTED_DATUMS)
        target_datum.value = "EGM96"
        params.append(target_datum)

        algorithm_text = arcpy.Parameter(
            displayName="Interpolation Algorithm",
            name="algorithm",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        algorithm_text.filter.list = list(DTED_ALGORITHMS)
        algorithm_text.value = "Bilinear Interpolation"
        params.append(algorithm_text)

        min_patch_size = arcpy.Parameter(
            displayName="Minimum Patch Size (posts)",
            name="min_patch_size",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        min_patch_size.value = DEFAULT_MIN_PATCH_SIZE
        params.append(min_patch_size)

        flatten = arcpy.Parameter(
            displayName="Retain Flat Areas",
            name="flatten",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        flatten.value = True
        params.append(flatten)

        create_mask = arcpy.Parameter(
            displayName="Create Mask",
            name="create_mask",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        create_mask.value = False
        params.append(create_mask)

        save_log = arcpy.Parameter(
            displayName="Save Log File",
            name="save_log",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        save_log.value = True
        params.append(save_log)

        skip_existing = arcpy.Parameter(
            displayName="Skip Existing Cells",
            name="skip_existing",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        skip_existing.value = False
        params.append(skip_existing)

        context_folder = arcpy.Parameter(
            displayName="Neighboring Tiles (not processed)",
            name="context_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        params.append(context_folder)

        water_levels = arcpy.Parameter(
            displayName="Water Levels Table",
            name="water_levels",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        water_levels.filter.list = ["csv"]
        params.append(water_levels)

        containment = arcpy.Parameter(
            displayName="Minimum Containment (0-1)",
            name="containment",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        containment.value = DEFAULT_CONTAINMENT
        params.append(containment)

        abs_horiz_accuracy = arcpy.Parameter(
            displayName="Absolute Horizontal Accuracy",
            name="abs_horiz_accuracy",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        params.append(abs_horiz_accuracy)

        dted_index = arcpy.Parameter(
            displayName="DTED Metadata Index",
            name="dted_index",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_index.filter.list = ["gpkg", "parquet"]
        params.append(dted_index)

        dted_profile = arcpy.Parameter(
            displayName="DTED Product Profile",
            name="dted_profile",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_profile.filter.list = ["toml"]
        params.append(dted_profile)

        dted_naming = arcpy.Parameter(
            displayName="DTED Output Naming",
            name="dted_naming",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_naming.filter.list = list(NAMING_LABELS) + [CUSTOM_NAMING]
        dted_naming.value = next(iter(NAMING_LABELS))
        params.append(dted_naming)

        dted_naming_template = arcpy.Parameter(
            displayName="Naming Template",
            name="dted_naming_template",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_naming_template.enabled = False
        params.append(dted_naming_template)

        dted_overrides = arcpy.Parameter(
            displayName="DTED Header Overrides",
            name="dted_overrides",
            datatype="GPValueTable",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_overrides.columns = [["GPString", "Header field"], ["GPString", "Value"]]
        dted_overrides.filters[0].type = "ValueList"
        dted_overrides.filters[0].list = list(EGMTrans.HEADER_FIELD_NAMES)
        params.append(dted_overrides)

        # Display only: validation writes the summary of the header fields here.
        dted_summary = arcpy.Parameter(
            displayName="DTED Header Summary (read only)",
            name="dted_summary",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category=DTED_CATEGORY)
        dted_summary.enabled = False
        params.append(dted_summary)

        output_layer = arcpy.Parameter(
            displayName="Output Raster Layer",
            name="output_layer",
            datatype="GPRasterLayer",
            parameterType="Derived",
            direction="Output")
        params.append(output_layer)

        return params

    def isLicensed(self):
        """Set whether the tool is licensed to execute."""
        return True

    def updateParameters(self, parameters):
        """Refresh the dialog for the Output Format, the datums and the water
        options, then fill the read-only summary of the DTED header fields
        from the index, the profile and the overrides as they are chosen."""
        p = _by_name(parameters)
        _show_dataset(p["input"])
        input_value = p["input"].valueAsText or ""

        # A DTED file input is written at its own level: the format follows
        # it unless the user has set the format themselves.
        if input_value.lower().endswith(DTED_EXTENSIONS) and not getattr(p["output_format"], "altered", False):
            level_format = f"DTED{input_value[-1]}"
            if p["output_format"].valueAsText != level_format:
                p["output_format"].value = level_format
        is_dted = _level_of(p) is not None

        # DTED is written in EGM96 only (MIL-PRF-89020B 3.2.2) and with the
        # bilinear algorithm only: the lists shrink to those values for a DTED
        # format and grow back for GeoTIFF. A value that is not on the list
        # any more is replaced, and a note says so.
        p["target_datum"].filter.list = list(DTED_DATUMS if is_dted else ALL_DATUMS)
        if is_dted and p["target_datum"].valueAsText not in DTED_DATUMS:
            if p["target_datum"].valueAsText:
                _NOTES["target_datum"] = (
                    f"Target Datum was set to EGM96: DTED is written in EGM96 only (MIL-PRF-89020B 3.2.2). "
                    f"Choose the GeoTIFF format for another target datum."
                )
            p["target_datum"].value = "EGM96"
        p["algorithm"].filter.list = list(DTED_ALGORITHMS if is_dted else ALL_ALGORITHMS)
        if is_dted and p["algorithm"].valueAsText not in DTED_ALGORITHMS:
            if p["algorithm"].valueAsText:
                _NOTES["algorithm"] = "Interpolation Algorithm was set to Bilinear Interpolation, the one DTED accepts."
            p["algorithm"].value = "Bilinear Interpolation"

        # The DTED group means nothing for a GeoTIFF output.
        for name in DTED_GROUP:
            p[name].enabled = is_dted and name != "dted_summary"
        p["dted_naming_template"].enabled = is_dted and p["dted_naming"].valueAsText == CUSTOM_NAMING

        # Water bodies are leveled between orthometric datums only, with
        # flattening on: otherwise the neighbors, the table and the share
        # have no effect.
        water = bool(p["flatten"].value) and "WGS84" not in (
            p["source_datum"].valueAsText, p["target_datum"].valueAsText
        )
        for name in WATER_GROUP:
            p[name].enabled = water

        if p["min_patch_size"].value is None:
            _NOTES["min_patch_size"] = f"Minimum Patch Size was set to {DEFAULT_MIN_PATCH_SIZE}, the default."
            p["min_patch_size"].value = DEFAULT_MIN_PATCH_SIZE

        try:
            if not is_dted or not (
                p["dted_index"].valueAsText or p["dted_profile"].valueAsText or _override_pairs(p["dted_overrides"])
            ):
                if p["dted_summary"].value:
                    p["dted_summary"].value = ""
                return
            source, coverage, _issues = _dted_metadata_for(p)
            summary = coverage.summary() if coverage is not None else source.describe()
        except Exception as e:  # validation must never take the dialog down
            summary = f"not readable: {e}"
        if p["dted_summary"].valueAsText != summary:
            p["dted_summary"].value = summary
        return

    def _check_dted_metadata(self, p, converting):
        """Errors and warnings on the DTED index, profile and override parameters.

        *converting* says whether a header is made from scratch, when every
        required field needs a source; a DTED-to-DTED run takes them from the
        input's header."""
        index_given = bool(p["dted_index"].valueAsText)
        profile_given = bool(p["dted_profile"].valueAsText)
        pairs = _override_pairs(p["dted_overrides"])
        if not (index_given or profile_given or pairs):
            return
        try:
            EGMTrans.parse_overrides(pairs)
        except ValueError as e:
            p["dted_overrides"].setErrorMessage(str(e))
            return
        try:
            _source, coverage, issues = _dted_metadata_for(p)
        except MetadataFileError as e:
            p[e.source].setErrorMessage(str(e))
            return
        except Exception as e:
            p["dted_index" if index_given else "dted_profile"].setErrorMessage(str(e))
            return
        # ArcGIS keeps one message per parameter, so the warnings of a parameter are joined.
        warned = {}
        for issue in issues:
            target = {"INDEX": "dted_index", "PROFILE": "dted_profile", "OVERRIDE": "dted_overrides"}.get(
                issue.record, "dted_profile" if profile_given else "dted_index"
            )
            if issue.severity == "error":
                p[target].setErrorMessage(str(issue))
            elif issue.severity == "warning":
                warned.setdefault(target, []).append(str(issue))
        if coverage is not None and coverage.missing_required and converting:
            # Blocked only when no cell has a source for the field; a NULL in some
            # rows is a warning, since the run stops before writing when it meets them.
            whole = [name for name, n in coverage.missing_required.items() if n >= coverage.cells]
            partial = [(name, n) for name, n in coverage.missing_required.items() if n < coverage.cells]
            if whole:
                p["dted_profile" if (profile_given or not index_given) else "dted_index"].setErrorMessage(
                    "Nothing supplies the required header field(s) " + ", ".join(whole)
                    + ": add them to the profile, the index or the overrides (for the compilation date, a DTED "
                    "Header Overrides row compilation_date = today)."
                )
            if partial:
                shown = "; ".join(f"{name} is NULL in {n:,} of {coverage.cells:,} cells" for name, n in partial)
                warned.setdefault("dted_index", []).append(
                    f"{shown}, and nothing else supplies it: a run that includes those cells stops before writing. "
                    "Fill the index, or add the field to the profile or the overrides."
                )
        for target, texts in warned.items():
            p[target].setWarningMessage("\n".join(texts))

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter. This method is called after internal validation."""
        p = _by_name(parameters)
        input_value = p["input"].valueAsText or ""
        output_value = p["output"].valueAsText or ""
        output_format = _format_of(p)
        level = _level_of(p)
        is_dted = level is not None
        input_is_dted = input_value.lower().endswith(DTED_EXTENSIONS)

        for name, note in list(_NOTES.items()):
            p[name].setWarningMessage(note)
        _NOTES.clear()

        if input_is_dted:
            input_level = int(input_value[-1])
            if not is_dted:
                p["output_format"].setErrorMessage(
                    f"The input is DTED level {input_level}, which is written as DTED at its level; choose "
                    f"DTED{input_level} as the Output Format."
                )
            elif level != input_level:
                p["output_format"].setErrorMessage(
                    f"The input is DTED level {input_level}: a DTED file keeps its level. Choose DTED{input_level}."
                )

        if output_value and not os.path.isdir(output_value):
            extension = os.path.splitext(output_value)[1].lower()
            if extension in GEOTIFF_EXTENSIONS and is_dted:
                p["output"].setErrorMessage(
                    f"The output is a GeoTIFF name but the Output Format is {output_format}; name it .dt{level}, "
                    f"give a folder, or choose GeoTIFF."
                )
            elif extension in DTED_EXTENSIONS and not is_dted:
                p["output"].setErrorMessage(
                    f"The output is a DTED name but the Output Format is GeoTIFF; choose DTED{extension[-1]}."
                )
            elif extension in DTED_EXTENSIONS and extension != f".dt{level}":
                p["output"].setErrorMessage(f"The output is DTED level {extension[-1]}, not {output_format}.")
            elif extension and extension not in GEOTIFF_EXTENSIONS + DTED_EXTENSIONS:
                p["output"].setWarningMessage(
                    f"{output_value} is treated as a folder; a single output file ends in .tif, .tiff, .dt0, .dt1 "
                    f"or .dt2."
                )

        if is_dted:
            problem = EGMTrans.dted_target_problem(p["target_datum"].valueAsText or "EGM96")
            if problem:
                p["target_datum"].setErrorMessage(problem)
            if (p["algorithm"].valueAsText or "Bilinear Interpolation") not in DTED_ALGORITHMS:
                p["algorithm"].setErrorMessage(
                    "DTED output requires Bilinear Interpolation: DTED tiles are edge-matched, and only "
                    "bilinear gives the same correction at a shared post whatever the tile extent."
                )

        source = p["source_datum"].valueAsText
        if source and input_value and os.path.isfile(input_value):
            declared = _declared_datum(input_value)
            if declared and source not in declared:
                p["source_datum"].setWarningMessage(
                    f"The input declares {declared}; Source Datum is {source}. The run ignores the file's header "
                    f"and shifts the heights from {source}."
                )

        if is_dted:
            naming = p["dted_naming"].valueAsText or next(iter(NAMING_LABELS))
            template = _naming_value(p)
            if naming == CUSTOM_NAMING and not template:
                p["dted_naming_template"].setErrorMessage(
                    "Give a template with {stem}, {dir}, {cell}, {lat}, {lon} or {level}, for example "
                    "DTED/{lon}/{lat}; the extension is added."
                )
            elif template:
                try:
                    EGMTrans.dted_naming_template(template)
                except ValueError as e:
                    p["dted_naming_template" if naming == CUSTOM_NAMING else "dted_naming"].setErrorMessage(str(e))
                else:
                    try:
                        from egmtrans.file_utils import dted_output_name

                        example = dted_output_name(template, "tile.tif", ".", "N49E006", level)
                        p["dted_naming"].setWarningMessage(f"Example: a cell N49E006 is written as {example}")
                    except ValueError as e:
                        p["dted_naming"].setErrorMessage(str(e))

        containment = p["containment"].value
        if containment is not None and not 0.0 <= containment <= 1.0:
            p["containment"].setErrorMessage("Minimum Containment must be between 0 and 1.")
        try:
            self._check_dted_metadata(p, is_dted and not input_is_dted)
        except Exception as e:  # a failure here would disable Run with no explanation
            p["dted_profile"].setErrorMessage(f"The DTED metadata could not be checked: {e}")
        return

    def execute(self, parameters, messages):
        """The source code of the tool."""
        p = _by_name(parameters)
        # A raster layer's data source, or the file or folder; a band is read as its file.
        input_path = _input_path(p["input"])

        output_path = p["output"].valueAsText
        output_format = _format_of(p)
        dted_level = FORMAT_LEVELS.get(output_format)
        source_datum = p["source_datum"].valueAsText
        target_datum = p["target_datum"].valueAsText
        algorithm_text = p["algorithm"].valueAsText
        min_patch_size = p["min_patch_size"].value if p["min_patch_size"].value is not None else DEFAULT_MIN_PATCH_SIZE
        abs_horiz_accuracy = p["abs_horiz_accuracy"].value
        flatten = bool(p["flatten"].value)
        create_mask = bool(p["create_mask"].value)
        save_log = bool(p["save_log"].value)
        skip_existing = bool(p["skip_existing"].value)
        context_folder = p["context_folder"].valueAsText
        water_levels = p["water_levels"].valueAsText
        containment = p["containment"].value if p["containment"].value is not None else DEFAULT_CONTAINMENT
        dted_index = p["dted_index"].valueAsText
        dted_profile = p["dted_profile"].valueAsText
        dted_naming = _naming_value(p) or NAMING_LABELS["DTED standard"]
        try:
            overrides = EGMTrans.parse_overrides(_override_pairs(p["dted_overrides"]))
        except ValueError as e:
            arcpy.AddError(f"DTED Header Overrides: {e}")
            return

        # Resolve input/output and derive the log path with the same helper the
        # CLI uses, so the two entry points cannot disagree about file vs. folder.
        try:
            io_paths = EGMTrans.resolve_io_paths(input_path, output_path, dted_level=dted_level)
            EGMTrans.prepare_output_target(io_paths)
            if dted_level is not None:
                EGMTrans.dted_naming_template(dted_naming)
        except (ValueError, OSError) as e:
            arcpy.AddError(str(e))
            return
        output_path = io_paths.output_path

        # The DTED metadata index and profile, checked before anything is written.
        try:
            dted_metadata = _load_metadata(dted_index, dted_profile, overrides)
        except (MetadataFileError, OSError, ValueError, RuntimeError) as e:
            arcpy.AddError(str(e))
            return
        if not dted_metadata.empty:
            issues = dted_metadata.validate(dted_level)
            errors = [issue for issue in issues if issue.severity == 'error']
            for issue in errors:
                arcpy.AddError(str(issue))
            if errors:
                arcpy.AddError("The DTED metadata index is not valid; see 'egmtrans dted-index validate'.")
                return
            for issue in issues:
                if issue.severity == 'warning':
                    arcpy.AddWarning(str(issue))

        EGMTrans.setup_logger(io_paths.log_path if save_log else None, save_log, is_arc_mode=True)
        arcpy.AddMessage(EGMTrans.versions_line())
        _warn_if_stale()

        algorithm = ALGORITHM_CODES.get(algorithm_text, "bilinear")

        arcpy.AddMessage(f"Input: {input_path}")
        arcpy.AddMessage(f"Output: {output_path}")
        arcpy.AddMessage(f"Output Format: {output_format}")
        arcpy.AddMessage(f"Source Datum: {source_datum}")
        arcpy.AddMessage(f"Target Datum: {target_datum}")
        arcpy.AddMessage(f"Interpolation Algorithm: {algorithm}")
        arcpy.AddMessage(f"Minimum Patch Size: {min_patch_size}")
        arcpy.AddMessage(f"Retain Flat Areas: {flatten}")
        arcpy.AddMessage(f"Create Mask: {create_mask}")
        arcpy.AddMessage(f"Save Log File: {save_log}")
        arcpy.AddMessage(f"Skip Existing Cells: {skip_existing}")
        arcpy.AddMessage(f"Neighboring Tiles: {context_folder}")
        arcpy.AddMessage(f"Water Levels Table: {water_levels}")
        arcpy.AddMessage(f"Minimum Containment: {containment}")
        if dted_level is not None:
            arcpy.AddMessage(f"Absolute Horizontal Accuracy: {abs_horiz_accuracy}")
            arcpy.AddMessage(f"DTED Metadata Index: {dted_index}")
            arcpy.AddMessage(f"DTED Product Profile: {dted_profile}")
            arcpy.AddMessage(f"DTED Output Naming: {dted_naming}")
            arcpy.AddMessage(
                "DTED Header Overrides: " + (", ".join(f"{k}={v}" for k, v in overrides.items()) or "none")
            )
            if not dted_metadata.empty:
                arcpy.AddMessage(f"DTED header fields: {dted_metadata.coverage(dted_level, abs_horiz_accuracy).summary()}")
        arcpy.AddMessage(f'{"="*80}\n')

        # The geoid grids this transform reads, and no others: on a closed
        # network that holds the two 1' grids nothing is downloaded.
        from egmtrans.config import get_datums_dir, required_grids
        from egmtrans.download import ensure_grids
        try:
            downloaded = ensure_grids(
                datums_dir=get_datums_dir(), filenames=required_grids(source_datum, target_datum),
                message_func=arcpy.AddMessage,
            )
            if downloaded:
                arcpy.AddMessage(f"Downloaded {len(downloaded)} geoid grid file(s).\n")
        except Exception as e:
            arcpy.AddError(
                f"The geoid grid files are missing and could not be downloaded: {e}\n"
                f"Download them from https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1 "
                f"and place the .tif files in the datums folder (their SHA-256 are in datums/README.md)."
            )
            EGMTrans.end_logger(save_log=save_log)
            return

        written = []
        stopped_early = False
        failed = 0
        try:
            if io_paths.mode == 'file' and not context_folder and not water_levels and dted_level is None:
                ok = EGMTrans.process_file(
                    io_paths.input_path, output_path, source_datum, target_datum, flatten, create_mask,
                    min_patch_size, algorithm, abs_horiz_accuracy, save_log, arc_mode=True,
                    min_containment=containment, dted_metadata=dted_metadata,
                )
                written = [output_path] if ok and os.path.isfile(output_path) else []
                if not ok:
                    failed = 1
            else:
                # The same two-pass runner as the command line: water bodies that
                # span tiles get one level, context tiles are analyzed but not
                # written, and GeoTIFF inputs become DTED cells when a level is given.
                result = EGMTrans.run_batch(
                    io_paths, source_datum, target_datum, flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log, arc_mode=True,
                    context_folders=[context_folder] if context_folder else [], water_levels=water_levels,
                    min_containment=containment, dted_metadata=dted_metadata,
                    dted_level=dted_level, dted_naming=dted_naming, skip_existing=skip_existing,
                    should_stop=lambda: bool(getattr(arcpy.env, "isCancelled", False)),
                )
                written = list(result.outputs)
                failed = len(result.failed)
                if result.cancelled:
                    arcpy.AddWarning("The run was cancelled; see the messages above for how far it got.")
                elif result.exit_code and not result.failed:
                    stopped_early = True
                    arcpy.AddError("The run stopped before writing; see the messages above.")
                elif result.failed:
                    arcpy.AddError(f"{len(result.failed)} DEM(s) were not transformed; see the messages above.")
        except Exception as e:
            arcpy.AddError(f"An error occurred: {str(e)}")
            from egmtrans.logging_setup import log_traceback

            log_traceback()
            failed = failed or 1

        arcpy.AddMessage(" ")
        if failed or stopped_early:
            arcpy.AddMessage("Processing stopped with errors.")
        else:
            arcpy.AddMessage("Processing completed.")
        EGMTrans.end_logger(save_log=save_log)

        # When exactly one GeoTIFF was written, calculate its statistics and
        # add it to the map; the derived output is the last parameter. A DTED
        # file is left alone: a layer would lock it and write a sidecar.
        if len(written) == 1 and os.path.isfile(written[0]):
            if written[0].lower().endswith(DTED_EXTENSIONS):
                messages.addMessage(
                    f"{os.path.basename(written[0])} is not added to the map: a layer would lock the DTED file "
                    f"and write a sidecar beside it. Add it from the Catalog pane when the delivery is complete."
                )
                return
            try:
                messages.addMessage("Calculating statistics before loading to map...")
                arcpy.management.CalculateStatistics(written[0])
                messages.addMessage("Statistics calculated successfully.")
            except Exception:
                pass  # ArcGIS Pro will calculate stats on-the-fly for display

            try:
                result_layer = arcpy.management.MakeRasterLayer(
                    written[0], os.path.basename(written[0]))
                arcpy.SetParameter(list(p).index("output_layer"), result_layer.getOutput(0))
            except Exception as e:
                arcpy.AddWarning(f"Could not create output layer for map display: {e}")
        elif not hasattr(p["input"].value, 'dataSource'):
            messages.addMessage("Several files were written, or none. Skipping automatic layer addition to map.")

        return

    def postExecute(self, parameters):
        """This method takes place after outputs are processed and
        added to the display."""
        return


class DtedSelfTest:
    """Convert built-in synthetic tiles to DTED and compare the bytes with the reference."""

    def __init__(self):
        self.label = "DTED Self-Test"
        self.description = (
            "Converts two built-in synthetic tiles to DTED2, DTED1 and DTED0 from EGM2008 to EGM96 and compares the "
            "SHA-256 of every header and record block with the pinned reference. A match shows that this host "
            "reproduces the reference bytes, so the DTED it makes from real tiles matches any other host that matches."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        keep = arcpy.Parameter(
            displayName="Keep Files in Folder",
            name="keep",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        return [keep]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        from egmtrans.config import verify_grids
        from egmtrans.dted.selftest import SOURCE_DATUM, TARGET_DATUM, run_selftest

        keep = _by_name(parameters)["keep"].valueAsText
        EGMTrans.setup_logger(None, False, is_arc_mode=True)
        arcpy.AddMessage(EGMTrans.versions_line())
        _warn_if_stale()
        try:
            verify_grids(SOURCE_DATUM, TARGET_DATUM)
        except FileNotFoundError as e:
            arcpy.AddError(str(e))
            return
        try:
            result = run_selftest(keep, keep=bool(keep))
        except Exception as e:
            arcpy.AddError(f"The self-test could not run: {e}")
            return
        finally:
            EGMTrans.end_logger(save_log=False)
        for item in result.items:
            state = "matches the reference" if item.ok else "DIFFERS from the reference"
            (arcpy.AddMessage if item.ok else arcpy.AddError)(
                f"{item.name}: header {item.header[:16]}..., records {item.records[:16]}...: {state}"
            )
        if result.ok:
            arcpy.AddMessage("Self-test passed: this host reproduces the reference bytes.")
        else:
            arcpy.AddError("Self-test FAILED: this host does not reproduce the reference bytes.")
        return

    def postExecute(self, parameters):
        return


class DtedHeaderReport:
    """Report and validate the MIL-PRF-89020B header of DTED files."""

    def __init__(self):
        self.label = "DTED Header Report"
        self.description = (
            "Report every field of the UHL, DSI and ACC records of DTED files, with the level evidence, a summary "
            "and the MIL-PRF-89020B findings, as text, JSON, CSV or Markdown."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = []

        # A raster layer or dataset first, so the browse dialog's OK selects a
        # DTED file it shows as a raster; a file for the DTED0 companions.
        input_param = arcpy.Parameter(
            displayName="DTED File or Folder",
            name="input",
            datatype=["GPRasterLayer", "DEFile", "DEFolder"],
            parameterType="Required",
            direction="Input")
        params.append(input_param)

        report_format = arcpy.Parameter(
            displayName="Report Format",
            name="report_format",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        report_format.filter.list = ["text", "json", "csv", "md"]
        report_format.value = "text"
        params.append(report_format)

        output_file = arcpy.Parameter(
            displayName="Report File",
            name="output_file",
            datatype="DEFile",
            parameterType="Optional",
            direction="Output")
        params.append(output_file)

        check_data = arcpy.Parameter(
            displayName="Check Elevation Records",
            name="check_data",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        check_data.value = False
        params.append(check_data)

        zero_based = arcpy.Parameter(
            displayName="Count Byte Positions From 0",
            name="zero_based",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        zero_based.value = False
        params.append(zero_based)

        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        _show_dataset(_by_name(parameters)["input"])
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        from egmtrans.cli_dted import report_extension
        from egmtrans.dted.companions import COMPANION_EXTENSIONS
        from egmtrans.dted.harvest import DTED_EXTENSIONS as CELL_EXTENSIONS, find_files
        from egmtrans.dted.report import build_report, render_report
        from egmtrans.dted.validate import count, validate_file

        p = _by_name(parameters)
        input_path = _input_path(p["input"])
        report_format = p["report_format"].valueAsText or "text"
        output_file = p["output_file"].valueAsText
        check_data = bool(p["check_data"].value)
        zero_based = bool(p["zero_based"].value)

        try:
            files = find_files([input_path], CELL_EXTENSIONS + COMPANION_EXTENSIONS)
        except FileNotFoundError as e:
            arcpy.AddError(f"Not found: {e}")
            return
        if not files:
            arcpy.AddError(f"No DTED file (.dt0, .dt1, .dt2, or a DTED0 .avg, .min, .max) under {input_path}.")
            return

        chunks = []
        for path in files:
            try:
                header, issues = validate_file(path, check_data=check_data, extension=report_extension(path))
            except (OSError, ValueError) as e:
                arcpy.AddError(f"{path}: {e}")
                continue
            report = build_report(path, header, issues, zero_based=zero_based)
            chunks.append(render_report(report, report_format))
            errors, warnings = count(issues, 'error'), count(issues, 'warning')
            line = f"{os.path.basename(path)}: {errors} error(s), {warnings} warning(s)"
            (arcpy.AddError if errors else arcpy.AddWarning if warnings else arcpy.AddMessage)(line)
            for issue in issues:
                if issue.severity == 'error':
                    arcpy.AddError(f"    {issue}")
                elif issue.severity == 'warning':
                    arcpy.AddWarning(f"    {issue}")
            if report_format == "text" and not output_file:
                for text_line in chunks[-1].splitlines():
                    arcpy.AddMessage(text_line)

        if report_format == "csv":
            text = chunks[0] + "".join(c.split("\n", 1)[1] for c in chunks[1:]) if chunks else ""
        elif report_format == "json" and len(chunks) > 1:
            text = "[\n" + ",\n".join(c.rstrip("\n") for c in chunks) + "\n]\n"
        else:
            text = "\n".join(chunks)
        if output_file:
            with open(output_file, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            arcpy.AddMessage(f"Report written to {output_file}")
        return

    def postExecute(self, parameters):
        return


class DtedDmed:
    """Write the DMED volume file of a DTED delivery."""

    def __init__(self):
        self.label = "Build DMED"
        self.description = (
            "Write the DMED volume file of MIL-PRF-89020B 3.9.5 for a DTED delivery laid out as DTED/E006/N49.dt2: "
            "the bounding rectangle of the cells, then for every cell of the rectangle its edition, its match/merge "
            "version and the minimum, maximum, mean and standard deviation of the posts of each 15-minute area. "
            "The file is named DMED and written beside the DTED folder."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        folder = arcpy.Parameter(
            displayName="Delivery Folder",
            name="folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        dmed_file = arcpy.Parameter(
            displayName="DMED File",
            name="dmed_file",
            datatype="DEFile",
            parameterType="Optional",
            direction="Output")
        check_only = arcpy.Parameter(
            displayName="Check Only",
            name="check_only",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        check_only.value = False
        return [folder, dmed_file, check_only]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        from egmtrans.dted.dmed import DmedError, locate_tree

        p = _by_name(parameters)
        folder = p["folder"].valueAsText
        if folder and os.path.isdir(folder):
            try:
                locate_tree(folder)
            except DmedError as e:
                p["folder"].setErrorMessage(str(e))
        return

    def execute(self, parameters, messages):
        from egmtrans.dted.dmed import DmedError, check_dmed, write_dmed

        p = _by_name(parameters)
        folder = p["folder"].valueAsText
        dmed_file = p["dmed_file"].valueAsText or None
        check_only = bool(p["check_only"].value)
        EGMTrans.setup_logger(None, False, is_arc_mode=True)
        arcpy.AddMessage(EGMTrans.versions_line())
        _warn_if_stale()
        try:
            if check_only:
                problems = check_dmed(folder, dmed_file)
                for problem in problems:
                    arcpy.AddError(problem)
                if problems:
                    arcpy.AddError("The DMED does not match the cells.")
                else:
                    arcpy.AddMessage("The DMED matches the cells.")
                return
            result = write_dmed(folder, dmed_file)
        except DmedError as e:
            arcpy.AddError(str(e))
            return
        except OSError as e:
            arcpy.AddError(f"The DMED could not be written: {e}")
            return
        finally:
            EGMTrans.end_logger(save_log=False)
        rectangle = result.rectangle
        arcpy.AddMessage(
            f"Wrote {result.path}: {result.record_count} records of 394 bytes for {len(result.cells)} cell(s) in a "
            f"rectangle of {rectangle.east - rectangle.west} x {rectangle.north - rectangle.south} degrees."
        )
        return

    def postExecute(self, parameters):
        return
