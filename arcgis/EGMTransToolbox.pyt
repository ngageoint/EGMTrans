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
        self.tools = [Tool]

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

        # Resolve input/output and derive the log path with the same helper the
        # CLI uses, so the two entry points cannot disagree about file vs. folder.
        try:
            io_paths = EGMTrans.resolve_io_paths(input_path, output_path)
            EGMTrans.prepare_output_target(io_paths)
        except (ValueError, OSError) as e:
            arcpy.AddError(str(e))
            return
        output_path = io_paths.output_path

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
                EGMTrans.process_file(io_paths.input_path, output_path, source_datum, target_datum, flatten, create_mask, min_patch_size, algorithm, abs_horiz_accuracy, save_log, arc_mode=True, min_containment=containment)
            else:
                # The same two-pass runner as the command line: water bodies that
                # span tiles get one level, context tiles are analyzed but not written.
                result = EGMTrans.run_batch(
                    io_paths, source_datum, target_datum, flatten, create_mask, min_patch_size, algorithm,
                    abs_horiz_accuracy, save_log, arc_mode=True,
                    context_folders=[context_folder] if context_folder else [], water_levels=water_levels,
                    min_containment=containment,
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
                arcpy.SetParameter(13, result_layer.getOutput(0))
            except Exception as e:
                arcpy.AddWarning(f"Could not create output layer for map display: {e}")
        elif not hasattr(input_param.value, 'dataSource'):
            messages.addMessage("Output is a folder. Skipping automatic layer addition to map.")

        return

    def postExecute(self, parameters):
        """This method takes place after outputs are processed and
        added to the display."""
        return