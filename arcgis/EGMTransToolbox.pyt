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

class Toolbox:
    def __init__(self):
        """Define the toolbox (the name of the toolbox is the name of the
        .pyt file)."""
        self.label = "EGM Transformation Tools"
        self.alias = "egmtrans"
        self.icon = "../img/icons/EGMTrans_32.png"

        # List of tool classes associated with this toolbox
        self.tools = [Tool, DtedHeaderReport]

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
        """Modify the values and properties of parameters before internal
        validation is performed.  This method is called whenever a parameter
        has been changed."""
        return

    def updateMessages(self, parameters):
        """Modify the messages created by internal validation for each tool
        parameter. This method is called after internal validation."""
        input_value = parameters[0].valueAsText or ""
        algorithm = parameters[4].valueAsText or "Bilinear Interpolation"
        if input_value.lower().endswith((".dt0", ".dt1", ".dt2")) and algorithm != "Bilinear Interpolation":
            parameters[4].setErrorMessage(
                "DTED output requires Bilinear Interpolation: DTED tiles are edge-matched, and only "
                "bilinear gives the same correction at a shared post whatever the tile extent."
            )
        containment = parameters[12].value
        if containment is not None and not 0.0 <= containment <= 1.0:
            parameters[12].setErrorMessage("Minimum Containment must be between 0 and 1.")
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

        # Resolve input/output and derive the log path with the same helper the
        # CLI uses, so the two entry points cannot disagree about file vs. folder.
        try:
            io_paths = EGMTrans.resolve_io_paths(input_path, output_path)
            EGMTrans.prepare_output_target(io_paths)
        except (ValueError, OSError) as e:
            arcpy.AddError(str(e))
            return
        output_path = io_paths.output_path

        # The DTED metadata index and profile, checked before anything is written.
        try:
            dted_metadata = EGMTrans.DtedMetadataSource.load(dted_index, dted_profile)
        except (OSError, ValueError, RuntimeError) as e:
            arcpy.AddError(str(e))
            return
        if not dted_metadata.empty:
            issues = dted_metadata.validate()
            errors = [issue for issue in issues if issue.severity == 'error']
            for issue in errors:
                arcpy.AddError(str(issue))
            if errors:
                arcpy.AddError("The DTED metadata index is not valid; see the DTED Header Report tool.")
                return

        EGMTrans.setup_logger(io_paths.log_path if save_log else None, save_log, is_arc_mode=True)

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

        try:
            if io_paths.mode == 'file' and not context_folder and not water_levels:
                EGMTrans.process_file(
                    io_paths.input_path, output_path, source_datum, target_datum, flatten, create_mask,
                    min_patch_size, algorithm, abs_horiz_accuracy, save_log, arc_mode=True,
                    min_containment=containment, dted_metadata=dted_metadata,
                )
            else:
                # The same two-pass runner as the command line: water bodies that
                # span tiles get one level, context tiles are analyzed but not written.
                result = EGMTrans.run_batch(
                    io_paths, source_datum, target_datum, flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log, arc_mode=True,
                    context_folders=[context_folder] if context_folder else [], water_levels=water_levels,
                    min_containment=containment, dted_metadata=dted_metadata,
                )
                if result.exit_code:
                    arcpy.AddError(f"{len(result.failed)} DEM(s) were not transformed; see the messages above.")
        except Exception as e:
            arcpy.AddError(f"An error occurred: {str(e)}")

        arcpy.AddMessage(" ")
        arcpy.AddMessage("Processing completed.")
        EGMTrans.end_logger(save_log=save_log)

        # After processing, check if the output path is a single file.
        # If so, calculate stats and create a layer to add to the map.
        if os.path.isfile(output_path):
            try:
                messages.addMessage("Calculating statistics before loading to map...")
                arcpy.management.CalculateStatistics(output_path)
                messages.addMessage("Statistics calculated successfully.")
            except Exception:
                pass  # ArcGIS Pro will calculate stats on-the-fly for display

            try:
                result_layer = arcpy.management.MakeRasterLayer(
                    output_path, os.path.basename(output_path))
                arcpy.SetParameter(15, result_layer.getOutput(0))
            except Exception as e:
                arcpy.AddWarning(f"Could not create output layer for map display: {e}")
        elif not hasattr(input_param.value, 'dataSource'):
            messages.addMessage("Output is a folder. Skipping automatic layer addition to map.")

        return

    def postExecute(self, parameters):
        """This method takes place after outputs are processed and
        added to the display."""
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