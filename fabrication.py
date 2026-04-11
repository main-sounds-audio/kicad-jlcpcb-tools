"""Handles the generation of the Gerber files, the BOM and the POS file."""

import csv
from importlib import import_module
import logging
import math
import os
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from pcbnew import (  # pylint: disable=import-error
    EXCELLON_WRITER,
    PCB_PLOT_PARAMS,
    PCB_VIA,
    PLOT_CONTROLLER,
    PLOT_FORMAT_GERBER,
    VECTOR2I,
    ZONE_FILLER,
    B_Cu,
    B_Mask,
    B_Paste,
    B_SilkS,
    Edge_Cuts,
    F_Cu,
    F_Mask,
    F_Paste,
    F_SilkS,
    FromMM,
    Refresh,
    ToMM,
    wxPoint,
)

# Compatibility hack for V6 / V7 / V7.99
try:
    from pcbnew import DRILL_MARKS_NO_DRILL_SHAPE  # pylint: disable=import-error

    NO_DRILL_SHAPE = DRILL_MARKS_NO_DRILL_SHAPE
except ImportError:
    NO_DRILL_SHAPE = PCB_PLOT_PARAMS.NO_DRILL_SHAPE


class Fabrication:
    """Contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent, board):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = board
        self.corrections = []
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.outputdir = os.path.join(self.path, "jlcpcb", "production_files")
        Path(self.outputdir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Version string helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _alpha_to_int(s):
        """Convert an Excel-style column label (A, B, Z, AA…) to a 0-based int."""
        s = s.upper()
        val = 0
        for ch in s:
            val = val * 26 + (ord(ch) - ord('A') + 1)
        return val - 1

    @staticmethod
    def _int_to_alpha(n):
        """Convert a 0-based int to an Excel-style column label (A, B, Z, AA…)."""
        result = ""
        n += 1
        while n > 0:
            n, rem = divmod(n - 1, 26)
            result = chr(ord('A') + rem) + result
        return result

    def _version_style(self):
        return self.parent.settings.get("gerber", {}).get("version_style", "integer")

    def _parse_version(self, s):
        """Parse a version string to a sortable float (or -1 if unrecognised)."""
        style = self._version_style()
        if style == "integer":
            return int(s) if s.isdigit() else -1
        if style in ("decimal1", "decimal2"):
            try:
                return float(s)
            except ValueError:
                return -1.0
        if style == "alpha":
            if s.isalpha():
                return float(self._alpha_to_int(s))
            return -1.0
        return -1

    def _format_version(self, v):
        """Format a numeric version value as a string for use in filenames."""
        style = self._version_style()
        if style == "integer":
            return str(int(round(v)))
        if style == "decimal1":
            return f"{v:.1f}"
        if style == "decimal2":
            return f"{v:.2f}"
        if style == "alpha":
            return self._int_to_alpha(int(round(v)))
        return str(int(round(v)))

    def _version_increment_value(self):
        """Return the numeric increment for the current style."""
        style = self._version_style()
        if style == "decimal1":
            return 0.1
        if style == "decimal2":
            return 0.01
        return 1.0  # integer and alpha both step by 1

    def delete_previous_fab_version(self):
        """Delete output files from the previous version once a new one has been written."""
        if not hasattr(self, "_prev_fab_version_str"):
            return
        stem = Path(self.filename).stem
        for prefix, ext in [("GERBER", "zip"), ("CPL", "csv"), ("BOM", "csv")]:
            f = os.path.join(self.outputdir, f"{prefix}-{stem}.{self._prev_fab_version_str}.{ext}")
            try:
                if os.path.exists(f):
                    os.remove(f)
                    self.logger.info("Deleted old fab file: %s", os.path.basename(f))
            except Exception as exc:
                self.logger.warning("Could not delete %s: %s", f, exc)

    def prepare_fab_version(self):
        """Scan output directory, cache the version string for this export run."""
        auto = self.parent.settings.get("gerber", {}).get("auto_version", True)
        if not auto:
            self._fab_version_str = self._format_version(0)
            self._prev_fab_version_str = None
            self.logger.info("Auto-version disabled — using fixed version %s", self._fab_version_str)
            return

        stem = Path(self.filename).stem
        prefix = f"GERBER-{stem}."
        highest = -1.0
        highest_str = None
        try:
            for name in os.listdir(self.outputdir):
                if name.startswith(prefix) and name.endswith(".zip"):
                    middle = name[len(prefix):-len(".zip")]
                    val = self._parse_version(middle)
                    if val > highest:
                        highest = val
                        highest_str = middle
        except FileNotFoundError:
            pass

        if highest_str is None:
            next_val = 0.0
        else:
            next_val = highest + self._version_increment_value()

        self._prev_fab_version_str = highest_str
        self._fab_version_str = self._format_version(next_val)
        self.logger.info("Fab output version: %s (prev: %s)", self._fab_version_str, highest_str)

    def next_fab_version(self):
        """Return the cached version string for this export run."""
        return getattr(self, "_fab_version_str", "0")

    def update_pcb_version_text(self):
        """Find PCB text items containing the previous version string and update them.

        Requires a previous version to exist (i.e. from the second export onwards).
        On the first export, place the version text manually on the board — every
        subsequent export will keep it in sync automatically.  Saves the board if
        any text items were changed.
        """
        if not self.parent.settings.get("gerber", {}).get("update_pcb_text", True):
            return
        if not hasattr(self, "_prev_fab_version_str") or not self._prev_fab_version_str:
            self.logger.info(
                "No previous version — PCB text update skipped "
                "(add version text to the board manually for the first export)"
            )
            return

        old_ver = self._prev_fab_version_str
        new_ver = self._fab_version_str
        updated = []

        for item in self.board.GetDrawings():
            try:
                cls = item.GetClass()
            except Exception:
                continue
            if cls not in ("PCB_TEXT", "PTEXT", "PCB_TEXTBOX"):
                continue
            try:
                text = item.GetText()
            except Exception:
                continue
            if old_ver in text:
                new_text = text.replace(old_ver, new_ver)
                item.SetText(new_text)
                updated.append((text, new_text))
                self.logger.info("Updated PCB text: %r → %r", text, new_text)

        if updated:
            try:
                self.board.Save(self.board.GetFileName())
                self.logger.info(
                    "Saved board — updated %d version text item(s)", len(updated)
                )
            except Exception as exc:
                self.logger.warning(
                    "Could not save board after updating version text: %s", exc
                )

    def fill_zones(self):
        """Refill copper zones following user prompt."""
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            filler = ZONE_FILLER(self.board)
            zones = self.board.Zones()
            filler.Fill(zones)
            Refresh()

    def _find_kicad_cli(self):
        """Locate the kicad-cli executable, returning its path or None."""
        import shutil
        candidates = [
            "kicad-cli",
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            "/usr/local/bin/kicad-cli",
            "/usr/bin/kicad-cli",
        ]
        for c in candidates:
            found = shutil.which(c) or (os.path.isfile(c) and c)
            if found:
                return found
        return None

    def run_drc(self):
        """Run DRC via kicad-cli after zone fill and return a list of error descriptions.

        Saves the board to an isolated temp directory in /tmp so that KiCad's
        background project-file and backup creation never touches the project
        folder.  The entire temp directory is removed with shutil.rmtree on exit.
        """
        import json
        import shutil
        import subprocess
        import tempfile

        kicad_cli = self._find_kicad_cli()
        if not kicad_cli:
            self.logger.warning("kicad-cli not found — DRC skipped")
            return []

        tmp_dir = tempfile.mkdtemp(prefix="jlcpcb_drc_")
        try:
            tmp_board = os.path.join(tmp_dir, "board_check.kicad_pcb")
            tmp_report = os.path.join(tmp_dir, "drc_report.json")

            self.board.Save(tmp_board)
            subprocess.run(
                [kicad_cli, "pcb", "drc",
                 "--format", "json",
                 "--severity-error",
                 "--output", tmp_report,
                 tmp_board],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if not os.path.exists(tmp_report):
                self.logger.warning("kicad-cli produced no DRC output")
                return []
            with open(tmp_report, encoding="utf-8") as f:
                data = json.load(f)
            errors = [
                v.get("description", "Unknown DRC error")
                for v in data.get("violations", [])
                if v.get("severity") == "error"
            ]
            self.logger.info("DRC completed: %d error(s) found", len(errors))
            return errors
        except Exception as exc:
            self.logger.warning("DRC check could not be completed: %s", exc)
            return []
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        original = footprint.GetOrientation()
        # `.AsDegrees()` added in KiCAD 6.99
        try:
            rotation = original.AsDegrees()
        except AttributeError:
            # we need to divide by 10 to get 180 out of 1800 for example.
            # This might be a bug in 5.99 / 6.0 RC
            rotation = original / 10
        if footprint.GetLayer() != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
        # First check if the part name matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetReference())):
                return self.rotate(footprint, rotation, correction)
        # Then try to match by value
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetValue())):
                return self.rotate(footprint, rotation, correction)
        # Then if the package matches
        for regex, correction, _ in self.corrections:
            if re.search(regex, str(footprint.GetFPID().GetLibItemName())):
                return self.rotate(footprint, rotation, correction)
        # If no correction matches, return the original rotation
        return rotation

    def rotate(self, footprint, rotation, correction):
        """Calculate the actual correction."""
        rotation = (rotation + int(correction)) % 360
        self.logger.info(
            "Fixed rotation of %s (%s / %s) on %s Layer by %d degrees",
            footprint.GetReference(),
            footprint.GetValue(),
            footprint.GetFPID().GetLibItemName(),
            "Top" if footprint.GetLayer() == 0 else "Bottom",
            correction,
        )
        return rotation

    def reposition(self, footprint, position, offset):
        """Adjust the position of the footprint, returning the new position as a wxPoint."""
        if offset[0] != 0 or offset[1] != 0:
            original = footprint.GetOrientation()
            # `.AsRadians()` added in KiCAD 6.99
            try:
                rotation = original.AsDegrees()
            except AttributeError:
                # we need to divide by 10 to get 180 out of 1800 for example.
                # This might be a bug in 5.99 / 6.0 RC
                rotation = original / 10
            if footprint.GetLayer() != 0:
                # bottom angles need to be mirrored on Y-axis
                rotation = (180 - rotation) % 360
            offset_x = FromMM(offset[0]) * math.cos(math.radians(rotation)) + FromMM(offset[1]) * math.sin(math.radians(rotation))
            offset_y = - FromMM(offset[0]) * math.sin(math.radians(rotation)) + FromMM(offset[1]) * math.cos(math.radians(rotation))
            if footprint.GetLayer() != 0:
                # mirrored coordinate system needs to be taken into account on the bottom
                offset_x = -offset_x
            self.logger.info(
                "Fixed position of %s (%s / %s) on %s Layer by %f/%f",
                footprint.GetReference(),
                footprint.GetValue(),
                footprint.GetFPID().GetLibItemName(),
                "Top" if footprint.GetLayer() == 0 else "Bottom",
                offset[0],
                offset[1],
            )
            return wxPoint(
                position.x + offset_x, position.y + offset_y
            )
        return position

    def fix_position(self, footprint, position):
        """Fix the position of footprints in order to be correct for JLCPCB."""
        # First check if the part name matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetReference())):
                return self.reposition(footprint, position, correction)
        # Then try to match by value
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetValue())):
                return self.reposition(footprint, position, correction)
        # Then if the package matches
        for regex, _, correction in self.corrections:
            if re.search(regex, str(footprint.GetFPID().GetLibItemName())):
                return self.reposition(footprint, position, correction)
        # If no correction matches, return the original position
        return position

    def get_position(self, footprint):
        """Calculate position based on center of bounding box."""
        try:
            pads = footprint.Pads()
            bbox = pads[0].GetBoundingBox()
            for pad in pads:
                bbox.Merge(pad.GetBoundingBox())
            return bbox.GetCenter()
        except:
            self.logger.info(
                "WARNING footprint %s: original position used", footprint.GetReference()
            )
            return footprint.GetPosition()

    def generate_geber(self, layer_count=None):
        """Generate Gerber files."""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py

        pctl = PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        popt.SetPlotReference(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)

        popt.SetCreateGerberJobFile(False)

        popt.SetSubtractMaskFromSilk(True)

        popt.SetUseAuxOrigin(True)

        # Tented vias or not, selcted by user in settings
        # Only possible via settings in KiCAD < 8.99
        # In KiCAD 8.99 this must be set in the layer settings of KiCAD
        if hasattr(PCB_VIA, "SetPlotViaOnMaskLayer"):
            popt.SetPlotViaOnMaskLayer(
                not self.parent.settings.get("gerber", {}).get("tented_vias", True)
            )

        popt.SetUseGerberX2format(True)

        popt.SetIncludeGerberNetlistInfo(True)

        popt.SetDisableGerberMacros(False)

        popt.SetDrillMarksType(NO_DRILL_SHAPE)

        popt.SetPlotFrameRef(False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", F_Cu, "Top layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("MaskTop", F_Mask, "Mask top"),
            ("PasteTop", F_Paste, "Paste top"),
        ]
        plot_plan_bottom = [
            ("CuBottom", B_Cu, "Bottom layer"),
            ("SilkBottom", B_SilkS, "Silk bottom"),
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("EdgeCuts", Edge_Cuts, "Edges"),
            ("PasteBottom", B_Paste, "Paste bottom"),
        ]

        plot_plan = []

        # Single sided PCB
        if layer_count == 1:
            plot_plan = plot_plan_top + plot_plan_bottom[-2:]
        # Double sided PCB
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        # Everything with inner layers
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (
                        f"CuIn{layer}",
                        getattr(import_module("pcbnew"), f"In{layer}_Cu"),
                        f"Inner layer {layer}",
                    )
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        # Add all JLC prefixed layers - layers must have "JLC_" in their name
        jlc_layers_to_plot = []
        enabled_layer_ids = list(self.board.GetEnabledLayers().Seq())
        for enabled_layer_id in enabled_layer_ids:
            layer_name_string = str(self.board.GetLayerName(enabled_layer_id)).upper()
            if "JLC_" in layer_name_string:
                plotter_info = (layer_name_string, enabled_layer_id, layer_name_string)
                jlc_layers_to_plot.append(plotter_info)
        plot_plan += jlc_layers_to_plot

        for layer_info in plot_plan:
            if layer_info[1] <= B_Cu:
                popt.SetSkipPlotNPTH_Pads(True)
            else:
                popt.SetSkipPlotNPTH_Pads(False)
            pctl.SetLayer(layer_info[1])
            pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
            if pctl.PlotLayer() is False:
                self.logger.error("Error plotting %s", layer_info[2])
            self.logger.info("Successfully plotted %s", layer_info[2])
        pctl.ClosePlot()

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = EXCELLON_WRITER(self.board)
        mirror = False
        minimalHeader = False
        offset = self.board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        drlwriter.SetFormat(False)
        genDrl = True
        genMap = True
        drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap)
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        version = self.next_fab_version()
        zipname = f"GERBER-{Path(self.filename).stem}.{version}.zip"
        with ZipFile(
            os.path.join(self.outputdir, zipname),
            "w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
        ) as zipfile:
            for folderName, _, filenames in os.walk(self.gerberdir):
                for filename in filenames:
                    if not filename.endswith(("gbr", "drl", "pdf")):
                        continue
                    filePath = os.path.join(folderName, filename)
                    zipfile.write(filePath, os.path.basename(filePath))
        self.logger.info(
            "Finished generating ZIP file %s", os.path.join(self.outputdir, zipname)
        )

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        version = self.next_fab_version()
        cplname = f"CPL-{Path(self.filename).stem}.{version}.csv"
        self.corrections = self.parent.library.get_all_correction_data()
        aux_orgin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        with open(
            os.path.join(self.outputdir, cplname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            footprints = sorted(self.board.Footprints(), key=lambda x: x.GetReference())
            for fp in footprints:
                part = self.parent.store.get_part(fp.GetReference())
                if not part:  # No matching part in the database, continue
                    continue
                if part["exclude_from_pos"] == 1:
                    continue
                if not add_without_lcsc and not part["lcsc"]:
                    continue
                try:  # Kicad <= 8.0
                    position = self.get_position(fp) - aux_orgin
                except TypeError:  # Kicad 8.99
                    x1, y1 = self.get_position(fp)
                    x2, y2 = aux_orgin
                    position = VECTOR2I(x1 - x2, y1 - y2)
                position = self.fix_position(fp, position)
                writer.writerow(
                    [
                        part["reference"],
                        part["value"],
                        part["footprint"],
                        ToMM(position.x),
                        ToMM(position.y) * -1,
                        self.fix_rotation(fp),
                        "top" if fp.GetLayer() == 0 else "bottom",
                    ]
                )
        self.logger.info(
            "Finished generating CPL file %s", os.path.join(self.outputdir, cplname)
        )

    def generate_bom(self):
        """Generate BOM file."""
        version = self.next_fab_version()
        bomname = f"BOM-{Path(self.filename).stem}.{version}.csv"
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_cpl", True
        )
        with open(
            os.path.join(self.outputdir, bomname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC", "Quantity"])
            for part in self.parent.store.read_bom_parts():
                components = part["refs"].split(",")
                for component in components:
                    for fp in self.board.Footprints():
                        if (
                            fp.GetReference() == component
                            and hasattr(fp, "IsDNP")
                            and callable(fp.IsDNP)
                            and fp.IsDNP()
                        ):
                            components.remove(component)
                            part["refs"] = ",".join(components)
                            self.logger.info(
                                "Component %s has 'Do not place' enabled: removing from BOM",
                                component,
                            )
                if not add_without_lcsc and not part["lcsc"]:
                    self.logger.info(
                        "Component %s has no LCSC number assigned and the setting Add parts without LCSC is disabled: removing from BOM",
                        component,
                    )
                    continue
                writer.writerow(
                    [
                        part["value"],
                        part["refs"],
                        part["footprint"],
                        part["lcsc"],
                        len(components),
                    ]
                )
        self.logger.info(
            "Finished generating BOM file %s", os.path.join(self.outputdir, bomname)
        )

    def get_part_consistency_warnings(self) -> str:
        """Check the plausibility of the parts, there should be just one value per LCSC number.

        Returns an empty sting if all parts are ok, otherwise a otherwise a overview of parts that share a LCSC number but have different values.
        """
        lcsc_numbers = {}
        for item in self.parent.store.read_bom_parts():
            if not item["lcsc"]:
                continue
            if item["lcsc"] not in lcsc_numbers:
                lcsc_numbers[item["lcsc"]] = [
                    {"refs": item["refs"], "values": item["value"]}
                ]
            else:
                lcsc_numbers[item["lcsc"]].append(
                    {"refs": item["refs"], "values": item["value"]}
                )
        filtered = {key: value for key, value in lcsc_numbers.items() if len(value) > 1}
        result = ""
        for lcsc, items in filtered.items():
            result += f"{lcsc}:\n"
            for item in items:
                result += f"  - {item['refs']} -> {item['values']}\n"
        return result
