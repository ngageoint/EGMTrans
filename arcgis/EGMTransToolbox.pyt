#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
within the ArcGIS Pro environment.
"""
import arcpy # type: ignore
from importlib import reload
import os
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


def _dted_metadata_for(parameters):
    """The run's DTED metadata (index, profile and overrides), its coverage of
    the header fields and its validation issues, for the parameters as they
    stand. Raises ValueError or OSError when a file or an override is wrong."""
    index = parameters[13].valueAsText or None
    profile = parameters[14].valueAsText or None
    level = int(parameters[15].valueAsText) if parameters[15].valueAsText else None
    abs_horiz = parameters[6].value
    pairs = tuple(_override_pairs(parameters[17]))
    key = (_file_key(index), _file_key(profile), level, abs_horiz, pairs)
    if _METADATA_CACHE.get("key") == key:
        return _METADATA_CACHE["value"]
    overrides = EGMTrans.parse_overrides(pairs)
    source = EGMTrans.DtedMetadataSource.load(index, profile, overrides)
    issues = source.validate(level) if not source.empty else []
    coverage = source.coverage(level, abs_horiz) if not source.empty else None
    _METADATA_CACHE["key"] = key
    _METADATA_CACHE["value"] = (source, coverage, issues)
    return _METADATA_CACHE["value"]


def _warn_if_stale():
    """ArcGIS Pro reloads the shim, not the package: a session that loaded an
    earlier egmtrans would keep writing that version's bytes."""
    try:
        import egmtrans
        from egmtrans import _version

        on_disk = {}
        with open(_version.__file__) as handle:
            exec(handle.read(), on_disk)
        if on_disk.get("__version__") != egmtrans.__version__:
            arcpy.AddWarning(
                f"EGMTrans {egmtrans.__version__} is loaded in this session, but version {on_disk.get('__version__')} "
                f"is on disk. Restart ArcGIS Pro to run the version on disk."
            )
    except Exception:
        pass


class Toolbox:
    def __init__(self):
        """Define the toolbox (the name of the toolbox is the name of the
        .pyt file)."""
        self.label = "EGM Transformation Tools"
        self.alias = "egmtrans"
        self.icon = "../img/icons/EGMTrans_32.png"

        # List of tool classes associated with this toolbox
        self.tools = [Tool, DtedHeaderReport, DtedSelfTest]

class Tool:
    def __init__(self):
        """Define the tool (tool name is the name of the class)."""
        self.label = "EGMTrans Tool"
        self.description = "Transform vertical datum between WGS 84 ellipsoid, EGM96, and EGM2008 for DTED and GeoTIFF files."
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
        
        source_datum = arcpy.Parameter(
            displayName="Source Datum",
            name="source_datum",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        source_datum.filter.list = ["WGS84", "EGM96", "EGM2008"]
        params.append(source_datum)
        
        target_datum = arcpy.Parameter(
            displayName="Target Datum",
            name="target_datum",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        target_datum.filter.list = ["WGS84", "EGM96", "EGM2008"]
        params.append(target_datum)

        algorithm_text = arcpy.Parameter(
            displayName="Interpolation Algorithm",
            name="algorithm",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        algorithm_text.filter.list = ["Bilinear Interpolation", "Thin Plate Spline", "Delaunay Triangulation"]
        algorithm_text.value = "Bilinear Interpolation"
        params.append(algorithm_text)

        min_patch_size = arcpy.Parameter(
            displayName="Minimum Patch Size (pixels)",
            name="min_patch_size",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        min_patch_size.value = 16
        params.append(min_patch_size)

        abs_horiz_accuracy = arcpy.Parameter(
            displayName="Absolute Horizontal Accuracy (applied only if missing)",
            name="abs_horiz_accuracy",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input")
        params.append(abs_horiz_accuracy)

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

        context_folder = arcpy.Parameter(
            displayName="Context Folder (neighboring tiles analyzed but not transformed)",
            name="context_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input")
        params.append(context_folder)

        water_levels = arcpy.Parameter(
            displayName="Water Levels Table (from an earlier run)",
            name="water_levels",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        water_levels.filter.list = ["csv"]
        params.append(water_levels)

        containment = arcpy.Parameter(
            displayName="Minimum Containment (share of a flat area's boundary above it, for it to be water)",
            name="containment",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        containment.value = 0.8
        params.append(containment)

        dted_index = arcpy.Parameter(
            displayName="DTED Metadata Index (GeoPackage or GeoParquet; fills the DTED header per cell)",
            name="dted_index",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        dted_index.filter.list = ["gpkg", "parquet"]
        params.append(dted_index)

        dted_profile = arcpy.Parameter(
            displayName="DTED Product Profile (TOML of header constants)",
            name="dted_profile",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        dted_profile.filter.list = ["toml"]
        params.append(dted_profile)

        dted_level = arcpy.Parameter(
            displayName="DTED Level (write every GeoTIFF input as DTED of this level, one file per cell)",
            name="dted_level",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        dted_level.filter.list = ["0", "1", "2"]
        params.append(dted_level)

        dted_naming = arcpy.Parameter(
            displayName="DTED Output Naming (stem, cell, dted, or a template with {stem} {dir} {cell} {lat} {lon} {level})",
            name="dted_naming",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        dted_naming.value = "stem"
        params.append(dted_naming)

        dted_overrides = arcpy.Parameter(
            displayName="DTED Header Overrides (a header field and the value to write in every cell)",
            name="dted_overrides",
            datatype="GPValueTable",
            parameterType="Optional",
            direction="Input")
        dted_overrides.columns = [["GPString", "Header field"], ["GPString", "Value"]]
        dted_overrides.filters[0].type = "ValueList"
        dted_overrides.filters[0].list = list(EGMTrans.HEADER_FIELD_NAMES)
        params.append(dted_overrides)

        # Display only: validation writes the summary of the header fields here.
        dted_summary = arcpy.Parameter(
            displayName="DTED Header Fields (from the index, the profile and the overrides; read only)",
            name="dted_summary",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
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
        """Fill the read-only summary of the DTED header fields from the index,
        the profile and the overrides as they are chosen."""
        try:
            if not (parameters[13].valueAsText or parameters[14].valueAsText or _override_pairs(parameters[17])):
                if parameters[18].value:
                    parameters[18].value = ""
                return
            source, coverage, _issues = _dted_metadata_for(parameters)
            summary = coverage.summary() if coverage is not None else source.describe()
        except Exception as e:  # validation must never take the dialog down
            summary = f"not readable: {e}"
        if parameters[18].valueAsText != summary:
            parameters[18].value = summary
        return

    def _check_dted_metadata(self, parameters, converting):
        """Errors and warnings on the DTED index, profile and override parameters.

        *converting* says whether a header is made from scratch, when every
        required field needs a source; a DTED-to-DTED run takes them from the
        input's header."""
        index_given = bool(parameters[13].valueAsText)
        profile_given = bool(parameters[14].valueAsText)
        pairs = _override_pairs(parameters[17])
        if not (index_given or profile_given or pairs):
            return
        try:
            EGMTrans.parse_overrides(pairs)
        except ValueError as e:
            parameters[17].setErrorMessage(str(e))
            return
        try:
            _source, coverage, issues = _dted_metadata_for(parameters)
        except Exception as e:
            parameters[13 if index_given else 14].setErrorMessage(str(e))
            return
        for issue in issues:
            if issue.severity == "error":
                parameters[13 if issue.record == "INDEX" else 14].setErrorMessage(str(issue))
        if coverage is None:
            return
        if coverage.missing_required and converting:
            parameters[14 if (profile_given or not index_given) else 13].setErrorMessage(
                "Nothing supplies the required header field(s) " + ", ".join(coverage.missing_required)
                + ": add them to the profile, the index or the overrides."
            )
        if coverage.null_accuracy_cells and index_given:
            parameters[13].setWarningMessage(
                f"{coverage.null_accuracy_cells} cell(s) of the index have a NULL accuracy; the header will say NA."
            )

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter. This method is called after internal validation."""
        input_value = parameters[0].valueAsText or ""
        output_value = parameters[1].valueAsText or ""
        algorithm = parameters[4].valueAsText or "Bilinear Interpolation"
        level = parameters[15].valueAsText
        dted_extensions = (".dt0", ".dt1", ".dt2")
        # DTED is written for a DTED input, a .dtN output name, or a level.
        writes_dted = (
            input_value.lower().endswith(dted_extensions) or output_value.lower().endswith(dted_extensions)
            or bool(level)
        )
        if writes_dted and algorithm != "Bilinear Interpolation":
            parameters[4].setErrorMessage(
                "DTED output requires Bilinear Interpolation: DTED tiles are edge-matched, and only "
                "bilinear gives the same correction at a shared post whatever the tile extent."
            )
        if level and output_value and not os.path.isdir(output_value):
            extension = os.path.splitext(output_value)[1].lower()
            if extension in (".tif", ".tiff"):
                parameters[1].setErrorMessage("A DTED level was given, but the output is a GeoTIFF file.")
            elif extension in dted_extensions and extension != f".dt{level}":
                parameters[1].setErrorMessage(f"The output is not DTED level {level}.")
        naming = parameters[16].valueAsText
        if naming:
            try:
                EGMTrans.dted_naming_template(naming)
            except ValueError as e:
                parameters[16].setErrorMessage(str(e))
        containment = parameters[12].value
        if containment is not None and not 0.0 <= containment <= 1.0:
            parameters[12].setErrorMessage("Minimum Containment must be between 0 and 1.")
        try:
            self._check_dted_metadata(parameters, writes_dted and not input_value.lower().endswith(dted_extensions))
        except Exception as e:  # a failure here would disable Run with no explanation
            parameters[14].setErrorMessage(f"The DTED metadata could not be checked: {e}")
        return

    def execute(self, parameters, messages):
        """The source code of the tool."""
        input_param = parameters[0]

        # Check if the input is a raster layer and get its data source path
        if hasattr(input_param.value, 'dataSource'):
            input_path = input_param.value.dataSource
        else:
            input_path = input_param.valueAsText

        output_path = parameters[1].valueAsText
        source_datum = parameters[2].valueAsText
        target_datum = parameters[3].valueAsText
        algorithm_text = parameters[4].valueAsText
        min_patch_size = parameters[5].value
        abs_horiz_accuracy = parameters[6].value
        flatten = parameters[7].value
        create_mask = parameters[8].value
        save_log = parameters[9].value
        context_folder = parameters[10].valueAsText
        water_levels = parameters[11].valueAsText
        containment = parameters[12].value if parameters[12].value is not None else 0.8
        dted_index = parameters[13].valueAsText
        dted_profile = parameters[14].valueAsText
        dted_level = int(parameters[15].valueAsText) if parameters[15].valueAsText else None
        dted_naming = parameters[16].valueAsText or "stem"
        try:
            overrides = EGMTrans.parse_overrides(_override_pairs(parameters[17]))
        except ValueError as e:
            arcpy.AddError(f"DTED Header Overrides: {e}")
            return

        # Resolve input/output and derive the log path with the same helper the
        # CLI uses, so the two entry points cannot disagree about file vs. folder.
        try:
            io_paths = EGMTrans.resolve_io_paths(input_path, output_path, dted_level=dted_level)
            EGMTrans.prepare_output_target(io_paths)
            EGMTrans.dted_naming_template(dted_naming)
        except (ValueError, OSError) as e:
            arcpy.AddError(str(e))
            return
        output_path = io_paths.output_path

        # The DTED metadata index and profile, checked before anything is written.
        try:
            dted_metadata = EGMTrans.DtedMetadataSource.load(dted_index, dted_profile, overrides)
        except (OSError, ValueError, RuntimeError) as e:
            arcpy.AddError(str(e))
            return
        if not dted_metadata.empty:
            issues = dted_metadata.validate(dted_level)
            errors = [issue for issue in issues if issue.severity == 'error']
            for issue in errors:
                arcpy.AddError(str(issue))
            if errors:
                arcpy.AddError("The DTED metadata index is not valid; see the DTED Header Report tool.")
                return

        EGMTrans.setup_logger(io_paths.log_path if save_log else None, save_log, is_arc_mode=True)
        arcpy.AddMessage(EGMTrans.versions_line())
        _warn_if_stale()

        algorithm_dict = {
            "Bilinear Interpolation": "bilinear",
            "Thin Plate Spline": "spline",
            "Delaunay Triangulation": "delaunay"
        }
        algorithm = algorithm_dict.get(algorithm_text, "bilinear")

        arcpy.AddMessage(f"Input: {input_path}")
        arcpy.AddMessage(f"Output: {output_path}")
        arcpy.AddMessage(f"Source Datum: {source_datum}")
        arcpy.AddMessage(f"Target Datum: {target_datum}")
        arcpy.AddMessage(f"Interpolation Algorithm: {algorithm}")
        arcpy.AddMessage(f"Minimum Patch Size: {min_patch_size}")
        arcpy.AddMessage(f"Retain Flat Areas: {flatten}")
        arcpy.AddMessage(f"Create Mask: {create_mask}")
        arcpy.AddMessage(f"Absolute Horizontal Accuracy: {abs_horiz_accuracy}")
        arcpy.AddMessage(f"Save Log File: {save_log}")
        arcpy.AddMessage(f"Context Folder: {context_folder}")
        arcpy.AddMessage(f"Water Levels Table: {water_levels}")
        arcpy.AddMessage(f"Minimum Containment: {containment}")
        arcpy.AddMessage(f"DTED Metadata Index: {dted_index}")
        arcpy.AddMessage(f"DTED Product Profile: {dted_profile}")
        arcpy.AddMessage(f"DTED Level: {dted_level}")
        arcpy.AddMessage(f"DTED Output Naming: {dted_naming}")
        arcpy.AddMessage("DTED Header Overrides: " + (", ".join(f"{k}={v}" for k, v in overrides.items()) or "none"))
        if not dted_metadata.empty:
            arcpy.AddMessage(f"DTED header fields: {dted_metadata.coverage(dted_level, abs_horiz_accuracy).summary()}")
        arcpy.AddMessage(f'{"="*80}\n')

        # Download geoid grid files on first run if they are missing.
        from egmtrans.download import ensure_grids
        try:
            downloaded = ensure_grids(message_func=arcpy.AddMessage)
            if downloaded:
                arcpy.AddMessage(f"Downloaded {len(downloaded)} geoid grid file(s).\n")
        except Exception as e:
            arcpy.AddError(
                f"Failed to download geoid grid files: {e}\n"
                f"Download manually from: "
                f"https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1\n"
                f"Place the .tif files in the datums/ folder."
            )
            return

        written = [output_path] if os.path.isfile(output_path) else []
        try:
            if io_paths.mode == 'file' and not context_folder and not water_levels and dted_level is None:
                ok = EGMTrans.process_file(
                    io_paths.input_path, output_path, source_datum, target_datum, flatten, create_mask,
                    min_patch_size, algorithm, abs_horiz_accuracy, save_log, arc_mode=True,
                    min_containment=containment, dted_metadata=dted_metadata,
                )
                written = [output_path] if ok and os.path.isfile(output_path) else []
            else:
                # The same two-pass runner as the command line: water bodies that
                # span tiles get one level, context tiles are analyzed but not
                # written, and GeoTIFF inputs become DTED cells when a level is given.
                result = EGMTrans.run_batch(
                    io_paths, source_datum, target_datum, flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log, arc_mode=True,
                    context_folders=[context_folder] if context_folder else [], water_levels=water_levels,
                    min_containment=containment, dted_metadata=dted_metadata,
                    dted_level=dted_level, dted_naming=dted_naming,
                )
                written = list(result.outputs)
                if result.exit_code:
                    arcpy.AddError(f"{len(result.failed)} DEM(s) were not transformed; see the messages above.")
        except Exception as e:
            arcpy.AddError(f"An error occurred: {str(e)}")

        arcpy.AddMessage(" ")
        arcpy.AddMessage("Processing completed.")
        EGMTrans.end_logger(save_log=save_log)

        # When exactly one file was written, calculate its statistics and add
        # it to the map; the derived output is the last parameter.
        if len(written) == 1 and os.path.isfile(written[0]):
            try:
                messages.addMessage("Calculating statistics before loading to map...")
                arcpy.management.CalculateStatistics(written[0])
                messages.addMessage("Statistics calculated successfully.")
            except Exception:
                pass  # ArcGIS Pro will calculate stats on-the-fly for display

            try:
                result_layer = arcpy.management.MakeRasterLayer(
                    written[0], os.path.basename(written[0]))
                arcpy.SetParameter(len(parameters) - 1, result_layer.getOutput(0))
            except Exception as e:
                arcpy.AddWarning(f"Could not create output layer for map display: {e}")
        elif not hasattr(input_param.value, 'dataSource'):
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
            displayName="Keep the tiles and DTED files in this folder (optional)",
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

        keep = parameters[0].valueAsText
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

        input_param = arcpy.Parameter(
            displayName="DTED File or Folder",
            name="input",
            datatype=["DEFile", "DEFolder"],
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
            displayName="Report File (optional; the report is also shown in the messages)",
            name="output_file",
            datatype="DEFile",
            parameterType="Optional",
            direction="Output")
        params.append(output_file)

        check_data = arcpy.Parameter(
            displayName="Check the elevation records (sentinels, counts, checksums, voids)",
            name="check_data",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input")
        check_data.value = False
        params.append(check_data)

        zero_based = arcpy.Parameter(
            displayName="Count byte positions from 0 instead of 1",
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
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        from egmtrans.dted.harvest import DTED_EXTENSIONS, find_files
        from egmtrans.dted.report import build_report, render_report
        from egmtrans.dted.validate import count, validate_file

        input_path = parameters[0].valueAsText
        report_format = parameters[1].valueAsText or "text"
        output_file = parameters[2].valueAsText
        check_data = bool(parameters[3].value)
        zero_based = bool(parameters[4].value)

        try:
            files = find_files([input_path], DTED_EXTENSIONS)
        except FileNotFoundError as e:
            arcpy.AddError(f"Not found: {e}")
            return
        if not files:
            arcpy.AddError(f"No DTED file (.dt0, .dt1, .dt2) under {input_path}.")
            return

        chunks = []
        for path in files:
            try:
                header, issues = validate_file(path, check_data=check_data)
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