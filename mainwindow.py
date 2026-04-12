"""Contains the main window of the plugin."""

from contextlib import suppress
from datetime import datetime as dt
import json
import logging
import os
import re
import sys
import time

import pcbnew as kicad_pcbnew
import wx  # pylint: disable=import-error
from wx import adv  # pylint: disable=import-error
import wx.dataview as dv  # pylint: disable=import-error

from .corrections import CorrectionManagerDialog
from .datamodel import PartListDataModel
from .derive_params import params_for_part
from .events import (
    EVT_ASSIGN_PARTS_EVENT,
    EVT_DOWNLOAD_COMPLETED_EVENT,
    EVT_DOWNLOAD_PROGRESS_EVENT,
    EVT_DOWNLOAD_STARTED_EVENT,
    EVT_LOGBOX_APPEND_EVENT,
    EVT_MESSAGE_EVENT,
    EVT_POPULATE_FOOTPRINT_LIST_EVENT,
    EVT_UNZIP_COMBINING_PROGRESS_EVENT,
    EVT_UNZIP_COMBINING_STARTED_EVENT,
    EVT_UNZIP_EXTRACTING_COMPLETED_EVENT,
    EVT_UNZIP_EXTRACTING_PROGRESS_EVENT,
    EVT_UNZIP_EXTRACTING_STARTED_EVENT,
    EVT_UPDATE_SETTING,
    LogboxAppendEvent,
)
from .fabrication import Fabrication
from .helpers import (
    PLUGIN_PATH,
    GetScaleFactor,
    HighResWxSize,
    get_is_dnp,
    getVersion,
    loadBitmapScaled,
    set_lcsc_value,
    toggle_exclude_from_bom,
    toggle_exclude_from_pos,
)
from .library import Library, LibraryState
from .partdetails import PartDetailsDialog
from .partmapper import PartMapperManagerDialog
from .partselector import PartSelectorDialog
from .schematicexport import SchematicExport
from .settings import SettingsDialog
from .store import Store

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

ID_GENERATE = 0
ID_LAYERS = 1
ID_CORRECTIONS = 2
ID_MAPPINGS = 3
ID_DOWNLOAD = 4
ID_SETTINGS = 5
ID_SELECT_PART = 6
ID_REMOVE_LCSC_NUMBER = 7
ID_SELECT_ALIKE = 8
ID_TOGGLE_BOM_POS = 9
ID_TOGGLE_BOM = 10
ID_TOGGLE_POS = 11
ID_PART_DETAILS = 12
ID_HIDE_BOM = 13
ID_HIDE_POS = 14
ID_SAVE_MAPPINGS = 15
ID_EXPORT_TO_SCHEMATIC = 16
ID_CONTEXT_MENU_COPY_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_PASTE_LCSC = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_ROT_BY_NAME = wx.NewIdRef()
ID_CONTEXT_MENU_FIND_MAPPING = wx.NewIdRef()
ID_CONTEXT_MENU_ADD_MAPPING = wx.NewIdRef()


class KicadProvider:
    """KiCad implementation of the provider, see standalone_impl.py for the stub version."""

    def get_pcbnew(self):
        """Get the pcbnew instance."""
        return kicad_pcbnew


class JLCPCBTools(wx.Frame):
    """JLCPCBTools main window."""

    def __init__(self, parent, kicad_provider=KicadProvider()):
        while not wx.GetApp():
            time.sleep(1)
        wx.Frame.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title=f"JLCPCB Tools [ {getVersion()} ]",
            pos=wx.DefaultPosition,
            size=wx.Size(1300, 800),
            style=wx.DEFAULT_FRAME_STYLE | wx.RESIZE_BORDER,
        )
        self.pcbnew = kicad_provider.get_pcbnew()
        self.window = wx.GetTopLevelParent(self)
        self.SetSize(HighResWxSize(self.window, wx.Size(1300, 800)))
        self.scale_factor = GetScaleFactor(self.window)
        self.project_path = os.path.split(self.pcbnew.GetBoard().GetFileName())[0]
        self.board_name = os.path.split(self.pcbnew.GetBoard().GetFileName())[1]
        self.schematic_name = f"{self.board_name.split('.')[0]}.kicad_sch"
        self.hide_bom_parts = False
        self.hide_pos_parts = False
        self.library: Library
        self.store: Store
        self.settings = {}
        self.load_settings()
        self.auto_select_alike = bool(
            self.settings.get("general", {}).get("select_alike_auto", False)
        )
        self.select_alike_in_progress = False
        self.Bind(wx.EVT_CLOSE, self.quit_dialog)

        # ---------------------------------------------------------------------
        # ---------------------------- Hotkeys --------------------------------
        # ---------------------------------------------------------------------
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)

        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        accel = wx.AcceleratorTable(entries)
        self.SetAcceleratorTable(accel)

        # ---------------------------------------------------------------------
        # -------------------- Horizontal top toolbar (custom panel) ----------
        # ---------------------------------------------------------------------
        # Built as a wx.Panel so font size is fully under our control,
        # matching the right-side panel.

        from .helpers import is_dark_mode as _is_dark
        _dark = _is_dark()
        _tb_fg = wx.WHITE if _dark else wx.BLACK
        _font_size = int(self.settings.get("general", {}).get("font_size", 11))
        _toolbar_font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        _toolbar_font.SetPointSize(_font_size)

        self.upper_toolbar = wx.Panel(self, style=wx.NO_BORDER)
        _top_sizer = wx.BoxSizer(wx.HORIZONTAL)

        def _make_top_btn(icon_file, label, tooltip, handler):
            """A vertical icon+label button for the top bar."""
            bmp = loadBitmapScaled(icon_file, self.scale_factor)
            img = wx.StaticBitmap(self.upper_toolbar, wx.ID_ANY, bmp)
            img.SetToolTip(wx.ToolTip(tooltip))
            img.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            lbl = wx.StaticText(self.upper_toolbar, label=label)
            lbl.SetForegroundColour(_tb_fg)
            lbl.SetFont(_toolbar_font)
            lbl.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            col = wx.BoxSizer(wx.VERTICAL)
            col.Add(img, 0, wx.ALIGN_CENTER | wx.TOP, 4)
            col.Add(lbl, 0, wx.ALIGN_CENTER | wx.BOTTOM, 4)
            _top_sizer.Add(col, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
            img.Bind(wx.EVT_LEFT_UP, handler)
            lbl.Bind(wx.EVT_LEFT_UP, handler)
            return img

        def _top_sep():
            line = wx.StaticLine(self.upper_toolbar, style=wx.LI_VERTICAL)
            _top_sizer.Add(line, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 4)

        self.generate_button = _make_top_btn(
            "fabrication.png", "Generate",
            "Generate fabrication files for JLCPCB",
            self.generate_fabrication_data)

        _top_sep()

        # Layer selector — keep as a real ComboBox (no bitmap needed)
        _layer_lbl = wx.StaticText(self.upper_toolbar, label="Layers:")
        _layer_lbl.SetFont(_toolbar_font)
        _layer_lbl.SetForegroundColour(_tb_fg)
        self.layer_selection = wx.Choice(self.upper_toolbar, ID_LAYERS)
        self.layer_selection.SetFont(_toolbar_font)
        for option in ["Auto","1 Layer","2 Layer","4 Layer","6 Layer","8 Layer",
                        "10 Layer","12 Layer","14 Layer","16 Layer","18 Layer","20 Layer"]:
            self.layer_selection.Append(option)
        self.layer_selection.SetSelection(0)
        _layer_row = wx.BoxSizer(wx.HORIZONTAL)
        _layer_row.Add(_layer_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        _layer_row.Add(self.layer_selection, 0, wx.ALIGN_CENTER_VERTICAL)
        _top_sizer.Add(_layer_row, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)

        _top_sizer.AddStretchSpacer(1)

        _top_sep()

        self.correction_button = _make_top_btn(
            "mdi-format-rotate-90.png", "Corrections",
            "Manage part corrections", self.manage_corrections)

        self.mapping_button = _make_top_btn(
            "mdi-selection.png", "Mappings",
            "Manage part mappings", self.manage_mappings)

        _top_sep()

        self.download_button = _make_top_btn(
            "mdi-cloud-download-outline.png", "Download",
            "Download latest JLCPCB parts database", self.update_library)

        self.settings_button = _make_top_btn(
            "mdi-cog-outline.png", "Settings",
            "Manage settings", self.manage_settings)

        self.upper_toolbar.SetSizer(_top_sizer)

        # ---------------------------------------------------------------------
        # ------------------ Right side toolbar List --------------------------
        # ---------------------------------------------------------------------
        # Right-side panel — uses wx.Panel + BitmapButton so that text colour
        # is fully under our control (wx.ToolBar on macOS ignores SetForegroundColour).
        # ---------------------------------------------------------------------

        from .helpers import is_dark_mode as _is_dark
        _dark = _is_dark()
        _btn_fg = wx.WHITE if _dark else wx.BLACK

        _font_size = int(self.settings.get("general", {}).get("font_size", 11))
        _toolbar_font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        _toolbar_font.SetPointSize(_font_size)

        self.right_toolbar_panel = wx.ScrolledWindow(
            self,
            style=wx.VSCROLL | wx.NO_BORDER,
        )
        self.right_toolbar_panel.SetScrollRate(0, 10)
        self.right_toolbar_panel.SetMinSize(wx.Size(int(self.scale_factor * 180), -1))

        _panel_sizer = wx.BoxSizer(wx.VERTICAL)

        # Track toggle state manually for hide buttons (StaticBitmap has no toggle state)
        self._hide_bom_state = False
        self._hide_pos_state = False

        def _make_btn(icon_file, label, tooltip, handler, toggle=False):
            """Create a labelled static-bitmap button.

            Using wx.StaticBitmap instead of wx.BitmapButton avoids macOS
            dimming the image when the button/window doesn't have focus.
            """
            bmp = loadBitmapScaled(icon_file, self.scale_factor)
            img = wx.StaticBitmap(self.right_toolbar_panel, wx.ID_ANY, bmp)
            img.SetToolTip(wx.ToolTip(tooltip))
            img.SetCursor(wx.Cursor(wx.CURSOR_HAND))

            lbl = wx.StaticText(self.right_toolbar_panel, label=label)
            lbl.SetForegroundColour(_btn_fg)
            lbl.SetFont(_toolbar_font)
            lbl.SetCursor(wx.Cursor(wx.CURSOR_HAND))

            row = wx.BoxSizer(wx.VERTICAL)
            row.Add(img, 0, wx.ALIGN_CENTER | wx.TOP, 8)
            row.Add(lbl, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 3)
            _panel_sizer.Add(row, 0, wx.EXPAND)

            img.Bind(wx.EVT_LEFT_UP, handler)
            lbl.Bind(wx.EVT_LEFT_UP, handler)
            return img

        # Order: most-used actions first
        self.select_part_button = _make_btn(
            "mdi-database-search-outline.png", "Assign LCSC number",
            "Assign a LCSC number to a footprint", self.select_part)

        self.remove_lcsc_number_button = _make_btn(
            "mdi-close-box-outline.png", "Remove LCSC number",
            "Remove a LCSC number from a footprint", self.remove_lcsc_number)

        self.select_alike_button = _make_btn(
            "mdi-checkbox-multiple-marked.png", "Select alike parts",
            "Select footprints that are alike", self.select_alike_parts)

        self.part_details_button = _make_btn(
            "mdi-text-box-search-outline.png", "Part details",
            "Show details of an assigned LCSC part", self.get_part_details)

        self.save_all_button = _make_btn(
            "mdi-content-save-settings.png", "Save mappings",
            "Save all mappings", self.save_all_mappings)

        self.export_schematic_button = _make_btn(
            "mdi-application-export.png", "Export to schematic",
            "Export mappings to schematic", self.export_to_schematic)

        # Less-used actions below
        self.toggle_bom_pos_button = _make_btn(
            "bom-pos.png", "Toggle BOM & POS",
            "Toggle exclude from BOM and POS attribute", self.toggle_bom_pos)

        self.toggle_bom_button = _make_btn(
            "mdi-format-list-bulleted.png", "Toggle BOM",
            "Toggle exclude from BOM attribute", self.toggle_bom)

        self.toggle_pos_button = _make_btn(
            "mdi-crosshairs-gps.png", "Toggle POS",
            "Toggle exclude from POS attribute", self.toggle_pos)

        self.hide_bom_button = _make_btn(
            "mdi-eye-off-outline.png", "Hide excluded BOM",
            "Hide excluded BOM parts", self.OnBomHide, toggle=True)

        self.hide_pos_button = _make_btn(
            "mdi-eye-off-outline.png", "Hide excluded POS",
            "Hide excluded POS parts", self.OnPosHide, toggle=True)

        self.right_toolbar_panel.SetSizer(_panel_sizer)

        # Keep a reference as self.right_toolbar so the rest of the code that
        # calls enable_part_specific_toolbar_buttons still works.
        self.right_toolbar = self.right_toolbar_panel

        # ---------------------------------------------------------------------
        # ----------------------- Footprint List ------------------------------
        # ---------------------------------------------------------------------

        table_sizer = wx.BoxSizer(wx.HORIZONTAL)
        table_sizer.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 600)))

        table_scroller = wx.ScrolledWindow(self, style=wx.HSCROLL | wx.VSCROLL)
        table_scroller.SetScrollRate(20, 20)

        self.footprint_list = dv.DataViewCtrl(
            table_scroller,
            style=wx.BORDER_THEME | dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_MULTIPLE,
        )

        reference = self.footprint_list.AppendTextColumn(
            "Ref", 0, width=50, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        value = self.footprint_list.AppendTextColumn(
            "Value (Name)",
            1,
            width=150,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        footprint = self.footprint_list.AppendTextColumn(
            "Footprint",
            2,
            width=250,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        params = self.footprint_list.AppendTextColumn(
            "LCSC Params",
            11,
            width=150,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        lcsc = self.footprint_list.AppendTextColumn(
            "LCSC", 3, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        type = self.footprint_list.AppendTextColumn(
            "Type", 4, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        stock = self.footprint_list.AppendTextColumn(
            "Stock", 5, width=100, mode=dv.DATAVIEW_CELL_INERT, align=wx.ALIGN_CENTER
        )
        bom = self.footprint_list.AppendIconTextColumn(
            "BOM", 6, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        pos = self.footprint_list.AppendIconTextColumn(
            "POS", 7, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        dnp = self.footprint_list.AppendIconTextColumn(
            "POP", 8, width=50, mode=dv.DATAVIEW_CELL_INERT
        )
        correction = self.footprint_list.AppendTextColumn(
            "Correction",
            9,
            width=120,
            mode=dv.DATAVIEW_CELL_INERT,
            align=wx.ALIGN_CENTER,
        )
        side = self.footprint_list.AppendIconTextColumn(
            "Side", 10, width=50, mode=dv.DATAVIEW_CELL_INERT
        )

        reference.SetSortable(True)
        value.SetSortable(True)
        footprint.SetSortable(True)
        lcsc.SetSortable(True)
        type.SetSortable(True)
        stock.SetSortable(True)
        bom.SetSortable(True)
        pos.SetSortable(False)
        dnp.SetSortable(True)
        correction.SetSortable(True)
        side.SetSortable(True)
        params.SetSortable(True)

        scrolled_sizer = wx.BoxSizer(wx.VERTICAL)
        scrolled_sizer.Add(self.footprint_list, 1, wx.EXPAND)
        table_scroller.SetSizer(scrolled_sizer)

        table_sizer.Add(table_scroller, 20, wx.ALL | wx.EXPAND, 5)

        self.footprint_list.Bind(
            dv.EVT_DATAVIEW_SELECTION_CHANGED, self.OnFootprintSelected
        )

        self.footprint_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.select_part)

        self.footprint_list.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self.OnRightDown)

        table_sizer.Add(self.right_toolbar_panel, 1, wx.EXPAND, 5)
        # ---------------------------------------------------------------------
        # --------------------- Bottom Logbox and Gauge -----------------------
        # ---------------------------------------------------------------------
        self.logbox = wx.TextCtrl(
            self,
            wx.ID_ANY,
            wx.EmptyString,
            wx.DefaultPosition,
            wx.DefaultSize,
            wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.logbox.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 150)))
        self.gauge = wx.Gauge(
            self,
            wx.ID_ANY,
            100,
            wx.DefaultPosition,
            HighResWxSize(self.window, wx.Size(100, -1)),
            wx.GA_HORIZONTAL,
        )
        self.gauge.SetValue(0)
        self.gauge.SetMinSize(HighResWxSize(self.window, wx.Size(-1, 5)))

        # ---------------------------------------------------------------------
        # ---------------------- Main Layout Sizer ----------------------------
        # ---------------------------------------------------------------------

        self.SetSizeHints(HighResWxSize(self.window, wx.Size(1000, -1)), wx.DefaultSize)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.upper_toolbar, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(table_sizer, 20, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.logbox, 0, wx.ALL | wx.EXPAND, 5)
        layout.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        self.SetSizer(layout)
        self.Layout()

        # Start maximised — this sidesteps the macOS menu-bar positioning issue
        # entirely and matches the "Zoom" behaviour the user was using manually.
        self.Maximize()

        # ---------------------------------------------------------------------
        # ------------------------ Custom Events ------------------------------
        # ---------------------------------------------------------------------

        self.Bind(EVT_MESSAGE_EVENT, self.display_message)
        self.Bind(EVT_ASSIGN_PARTS_EVENT, self.assign_parts)
        self.Bind(EVT_POPULATE_FOOTPRINT_LIST_EVENT, self.populate_footprint_list)
        self.Bind(EVT_UPDATE_SETTING, self.update_settings)

        self.Bind(EVT_DOWNLOAD_STARTED_EVENT, self.download_started)
        self.Bind(EVT_DOWNLOAD_PROGRESS_EVENT, self.download_progress)
        self.Bind(EVT_DOWNLOAD_COMPLETED_EVENT, self.download_completed)

        self.Bind(EVT_UNZIP_COMBINING_STARTED_EVENT, self.unzip_combining_started)
        self.Bind(EVT_UNZIP_COMBINING_PROGRESS_EVENT, self.unzip_combining_progress)
        self.Bind(EVT_UNZIP_EXTRACTING_STARTED_EVENT, self.unzip_extracting_started)
        self.Bind(EVT_UNZIP_EXTRACTING_PROGRESS_EVENT, self.unzip_extracting_progress)
        self.Bind(EVT_UNZIP_EXTRACTING_COMPLETED_EVENT, self.unzip_extracting_completed)

        self.Bind(EVT_LOGBOX_APPEND_EVENT, self.logbox_append)

        self.enable_part_specific_toolbar_buttons(False)

        self.init_logger()
        self.partlist_data_model = PartListDataModel(self.scale_factor)
        self.footprint_list.AssociateModel(self.partlist_data_model)

        self.init_data()

    def init_data(self):
        """Initialize the library and populate the main window."""
        self.init_library()
        self.init_fabrication()
        if self.library.state == LibraryState.UPDATE_NEEDED:
            self.library.update()
        else:
            self.init_store()
        self.library.create_mapping_table()

        self.logger.debug("kicad version: %s", kicad_pcbnew.GetBuildVersion())

    def quit_dialog(self, *_):
        """Destroy window on close."""
        self.logger.info("quit_dialog()")
        root = logging.getLogger()
        with suppress(AttributeError):
            root.removeHandler(self.logging_handler1)
        with suppress(AttributeError):
            root.removeHandler(self.logging_handler2)

        self.Destroy()

    def init_library(self):
        """Initialize the parts library."""
        self.library = Library(self)
        meta = self.library.get_parts_db_info()
        if meta is not None:
            last_update = dt.fromisoformat(meta.last_update).strftime("%Y-%m-%d %H:%M")
            self.SetTitle(
                f"JLCPCB Tools [ {getVersion()} ] | Last database update: {last_update}",
            )
            self.logger.debug(
                "JLCPCB version %s, last database update %s, part count %d, size (bytes) %d",
                getVersion(),
                meta.last_update,
                meta.part_count,
                meta.size,
            )
        else:
            self.SetTitle(
                f"JLCPCB Tools [ {getVersion()} ] | Last database update: No DB found",
            )
            self.logger.debug("JLCPCB version %s, no parts db info found", getVersion())

    def init_store(self):
        """Initialize the store of part assignments."""
        self.store = Store(self, self.project_path, self.pcbnew.GetBoard())
        if self.library.state == LibraryState.INITIALIZED:
            self.populate_footprint_list()

    def init_fabrication(self):
        """Initialize the fabrication."""
        self.fabrication = Fabrication(self, self.pcbnew.GetBoard())

    def reset_gauge(self, *_):
        """Initialize the gauge."""
        self.gauge.SetRange(100)
        self.gauge.SetValue(0)

    def download_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def download_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def download_completed(self, *_):
        """Populate the footprint list."""
        self.populate_footprint_list()

    def unzip_combining_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def unzip_combining_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def unzip_extracting_started(self, *_):
        """Initialize the gauge."""
        self.reset_gauge()

    def unzip_extracting_progress(self, e):
        """Update the gauge."""
        self.gauge.SetValue(int(e.value))

    def unzip_extracting_completed(self, *_):
        """Update the gauge."""
        self.reset_gauge()
        self.init_data()

    def assign_parts(self, e):
        """Assign a selected LCSC number to parts."""
        for reference in e.references:
            self.store.set_lcsc(reference, e.lcsc)
            self.store.set_stock(reference, int(e.stock))
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(reference)
            set_lcsc_value(fp, e.lcsc)
            params = params_for_part(self.library.get_part_details(e.lcsc))
            self.partlist_data_model.set_lcsc(
                reference, e.lcsc, e.type, e.stock, params
            )

    def display_message(self, e):
        """Dispaly a message with the data from the event."""
        styles = {
            "info": wx.ICON_INFORMATION,
            "warning": wx.ICON_WARNING,
            "error": wx.ICON_ERROR,
        }
        wx.MessageBox(e.text, e.title, style=styles.get(e.style, wx.ICON_INFORMATION))

    def get_correction(self, part: dict, corrections: list) -> str:
        """Try to find correction data for a given part."""
        # First check if the part name matches
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["reference"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (ref)"
        # Then try to match by value
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["value"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (val)"
        # If there was no match for the part name or value, check if the package matches
        for regex, rotation, offset in corrections:
            if re.search(regex, str(part["footprint"])):
                return f"{str(rotation)}°, {str(offset[0])}/{str(offset[1])} (fpt)"
        return "0°, 0.0/0.0"

    def populate_footprint_list(self, *_):
        """Populate list of footprints."""
        if not self.store:
            self.init_store()
        self.partlist_data_model.RemoveAll()
        details = {}
        corrections = self.library.get_all_correction_data()
        for part in self.store.read_all():
            fp = self.pcbnew.GetBoard().FindFootprintByReference(part["reference"])
            is_dnp = get_is_dnp(fp)
            # Get part stock and type from library, skip if part number was already looked up before
            if part["lcsc"] and part["lcsc"] not in details:
                details[part["lcsc"]] = self.library.get_part_details(part["lcsc"])
            # don't show the part if hide BOM is set
            if self.hide_bom_parts and part["exclude_from_bom"]:
                continue
            # don't show the part if hide POS is set
            if self.hide_pos_parts and part["exclude_from_pos"]:
                continue
            self.partlist_data_model.AddEntry(
                [
                    part["reference"],
                    part["value"],
                    part["footprint"],
                    part["lcsc"],
                    details.get(part["lcsc"], {}).get("type", ""),  # type
                    details.get(part["lcsc"], {}).get("stock", ""),  # stock
                    part["exclude_from_bom"],
                    part["exclude_from_pos"],
                    int(is_dnp),
                    str(self.get_correction(part, corrections)),
                    str(fp.GetLayer()),
                    params_for_part(details.get(part["lcsc"], {})),
                ]
            )

    def OnBomHide(self, *_):
        """Hide all parts from the list that have 'in BOM' set to No."""
        self.hide_bom_parts = not self.hide_bom_parts
        icon = "mdi-eye-outline.png" if self.hide_bom_parts else "mdi-eye-off-outline.png"
        self.hide_bom_button.SetBitmap(loadBitmapScaled(icon, self.scale_factor))
        self.populate_footprint_list()

    def OnPosHide(self, *_):
        """Hide all parts from the list that have 'in pos' set to No."""
        self.hide_pos_parts = not self.hide_pos_parts
        icon = "mdi-eye-outline.png" if self.hide_pos_parts else "mdi-eye-off-outline.png"
        self.hide_pos_button.SetBitmap(loadBitmapScaled(icon, self.scale_factor))
        self.populate_footprint_list()

    def OnFootprintSelected(self, *_):
        """Enable the toolbar buttons when a selection was made."""
        if self.select_alike_in_progress:
            return

        self.enable_part_specific_toolbar_buttons(
            self.footprint_list.GetSelectedItemsCount() > 0
        )

        if self.auto_select_alike and self.footprint_list.GetSelectedItemsCount() == 1:
            self.select_alike_parts()

        # clear the present selections
        selection = self.pcbnew.GetCurrentSelection()
        for selected in selection:
            selected.ClearSelected()

        # select all of the selected items in the footprint_list
        if self.footprint_list.GetSelectedItemsCount() > 0:
            for item in self.footprint_list.GetSelections():
                ref = self.partlist_data_model.get_reference(item)
                fp = self.pcbnew.GetBoard().FindFootprintByReference(ref)
                fp.SetSelected()
            # cause pcbnew to refresh the board with the changes to the selected footprint(s)
            self.pcbnew.Refresh()

    def enable_part_specific_toolbar_buttons(self, state):
        """Control the state of all the buttons that relate to parts in toolbar on the right side."""
        for button in (
            self.select_part_button,
            self.remove_lcsc_number_button,
            self.select_alike_button,
            self.toggle_bom_pos_button,
            self.toggle_bom_button,
            self.toggle_pos_button,
            self.part_details_button,
            self.hide_bom_button,
            self.hide_pos_button,
        ):
            button.Enable(state)

    def toggle_bom_pos(self, *_):
        """Toggle the exclude from BOM/POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            bom = toggle_exclude_from_bom(fp)
            pos = toggle_exclude_from_pos(fp)
            self.store.set_bom(ref, int(bom))
            self.store.set_pos(ref, int(pos))
            self.partlist_data_model.toggle_bom_pos(item)

    def toggle_bom(self, *_):
        """Toggle the exclude from BOM attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            bom = toggle_exclude_from_bom(fp)
            self.store.set_bom(ref, int(bom))
            self.partlist_data_model.toggle_bom(item)

    def toggle_pos(self, *_):
        """Toggle the exclude from POS attribute of a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            pos = toggle_exclude_from_pos(fp)
            self.store.set_pos(ref, int(pos))
            self.partlist_data_model.toggle_pos(item)

    def remove_lcsc_number(self, *_):
        """Remove an assigned a LCSC Part number to a footprint."""
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            self.store.set_lcsc(ref, "")
            self.store.set_stock(ref, None)
            board = self.pcbnew.GetBoard()
            fp = board.FindFootprintByReference(ref)
            set_lcsc_value(fp, "")
            self.partlist_data_model.remove_lcsc_number(item)

    def select_alike_parts(self, *_):
        """Select all alike parts, starting from a single selected part."""
        if self.footprint_list.GetSelectedItemsCount() > 1:
            self.logger.warning("Select only one component, please.")
            return
        selected_item = self.footprint_list.GetSelection()
        self.select_alike_in_progress = True
        try:
            for alike_item in self.partlist_data_model.select_alike(selected_item):
                if not self.footprint_list.IsSelected(alike_item):
                    self.footprint_list.Select(alike_item)
        finally:
            self.select_alike_in_progress = False

    def toggle_select_alike(self, e):
        """Toggle auto-selecting alike parts on selection."""
        self.auto_select_alike = bool(e.IsChecked())
        self.settings.setdefault("general", {})["select_alike_auto"] = (
            self.auto_select_alike
        )
        self.save_settings()
        if self.auto_select_alike and self.footprint_list.GetSelectedItemsCount() == 1:
            self.select_alike_parts()

    def get_part_details(self, *_):
        """Fetch part details from LCSC and show them one after another each in a modal."""
        for item in self.footprint_list.GetSelections():
            if lcsc := self.partlist_data_model.get_lcsc(item):
                self.show_part_details_dialog(lcsc)

    def show_part_details_dialog(self, part):
        """Show the part details modal dialog."""
        wx.BeginBusyCursor()
        try:
            dialog = PartDetailsDialog(self, part)
            dialog.ShowModal()
        finally:
            wx.EndBusyCursor()

    def update_library(self, *_):
        """Update the library from the JLCPCB CSV file."""
        self.library.update()

    def manage_corrections(self, *_):
        """Manage corrections."""
        CorrectionManagerDialog(self, "").ShowModal()

    def manage_mappings(self, *_):
        """Manage footprint mappings."""
        PartMapperManagerDialog(self).ShowModal()

    def manage_settings(self, *_):
        """Manage settings."""
        SettingsDialog(self).ShowModal()

    def update_settings(self, e):
        """Update the settings on change."""
        if e.section not in self.settings:
            self.settings[e.section] = {}
        self.settings[e.section][e.setting] = e.value
        self.save_settings()

        # Refresh library configuration if relevant library settings changed
        if e.section == "library" and e.setting in ["selected_library", "data_path"]:
            self.library.refresh_library_config()

    def logbox_append(self, e):
        """Write text to the logbox."""
        self.logbox.WriteText(e.msg)

    def load_settings(self):
        """Load settings from settings.json."""
        with open(os.path.join(PLUGIN_PATH, "settings.json"), encoding="utf-8") as j:
            self.settings = json.load(j)

    def save_settings(self):
        """Save settings to settings.json."""
        with open(
            os.path.join(PLUGIN_PATH, "settings.json"), "w", encoding="utf-8"
        ) as j:
            json.dump(self.settings, j)

    def select_part(self, *_):
        """Select a part from the library and assign it to the selected footprint(s)."""
        selection = {}
        for item in self.footprint_list.GetSelections():
            ref = self.partlist_data_model.get_reference(item)
            value = self.partlist_data_model.get_value(item)
            footprint = self.partlist_data_model.get_footprint(item)
            if ref.startswith("R"):
                """ Auto remove alphabet unit if applicable """
                if value.endswith("R") or value.endswith("r") or value.endswith("o"):
                    value = value[:-1]
                value += "Ω"
            m = re.search(r"_(\d+)_\d+Metric", footprint)
            if m:
                value += f" {m.group(1)}"
            selection[ref] = value
        PartSelectorDialog(self, selection).ShowModal()

    def count_order_number_placeholders(self):
        """Count the JLC order/serial number placeholders."""
        count = 0
        for drawing in self.pcbnew.GetBoard().GetDrawings():
            if drawing.IsOnLayer(kicad_pcbnew.F_SilkS) or drawing.IsOnLayer(
                kicad_pcbnew.B_SilkS
            ):
                if isinstance(drawing, kicad_pcbnew.PCB_TEXT):
                    if drawing.GetText().strip() == "JLCJLCJLCJLC":
                        self.logger.info(
                            "Found placeholder for order number at %.1f/%.1f.",
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

                if (
                    isinstance(drawing, kicad_pcbnew.PCB_SHAPE)
                    and drawing.GetShape() == kicad_pcbnew.S_RECT
                    and ((hasattr(drawing, "IsFilled") and drawing.IsFilled())
                    or (hasattr(drawing, "IsSolidFill") and drawing.IsSolidFill()))
                ):
                    corners = drawing.GetRectCorners()

                    top_left_x = min([p.x for p in corners], default=0)
                    top_left_y = min([p.y for p in corners], default=0)
                    bottom_right_x = max([p.x for p in corners], default=0)
                    bottom_right_y = max([p.y for p in corners], default=0)
                    width = kicad_pcbnew.ToMM(bottom_right_x - top_left_x)
                    height = kicad_pcbnew.ToMM(bottom_right_y - top_left_y)

                    if (
                        (width == 5 and height == 5)
                        or (width == 8 and height == 8)
                        or (width == 10 and height == 10)
                    ):
                        self.logger.info(
                            "Found placeholder for 2D barcode (%dmm x %dmm) at %.1f/%.1f.",
                            width,
                            height,
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

                    if (width == 10 and height == 2) or (width == 2 and height == 10):
                        self.logger.info(
                            "Found placeholder for serial number at %.1f/%.1f.",
                            kicad_pcbnew.ToMM(drawing.GetCenter().x),
                            kicad_pcbnew.ToMM(drawing.GetCenter().y),
                        )
                        count += 1

        return count

    def generate_fabrication_data(self, *_):
        """Generate fabrication data."""
        warnings = self.fabrication.get_part_consistency_warnings()
        if warnings:
            result = wx.MessageBox(
                "There are items with identical LCSC number but different values in the list:\n"
                + warnings
                + "Continue?",
                "Plausibility check",
                wx.OK | wx.CANCEL | wx.CENTER,
            )
            if result == wx.CANCEL:
                return

        if self.settings.get("general", {}).get("order_number"):
            count = self.count_order_number_placeholders()
            if count == 0:
                result = wx.MessageBox(
                    "JLC order/serial number placeholder not present! Continue?",
                    "JLC order/serial number placeholder",
                    wx.OK | wx.CANCEL | wx.CENTER,
                )
                if result == wx.CANCEL:
                    return
            elif count > 1:
                result = wx.MessageBox(
                    "Multiple order/serial number placeholders present! Continue?",
                    "JLC order/serial number placeholder",
                    wx.OK | wx.CANCEL | wx.CENTER,
                )
                if result == wx.CANCEL:
                    return
        self.fabrication.fill_zones()

        if self.settings.get("gerber", {}).get("run_drc", True):
            drc_errors = self.fabrication.run_drc()
            if drc_errors:
                preview = "\n".join(drc_errors[:10])
                if len(drc_errors) > 10:
                    preview += f"\n\n... and {len(drc_errors) - 10} more error(s)"
                result = wx.MessageBox(
                    f"DRC found {len(drc_errors)} error(s):\n\n{preview}\n\n"
                    "Fix the errors and try again, or click OK to export anyway.",
                    "DRC Errors",
                    wx.OK | wx.CANCEL | wx.ICON_ERROR | wx.CENTER,
                )
                if result == wx.CANCEL:
                    return

        layer_selection = self.layer_selection.GetSelection()
        number = re.search(r"\d+", self.layer_selection.GetString(layer_selection))
        if number:
            layer_count = int(number.group(0))
        else:
            layer_count = None
        self.fabrication.prepare_fab_version()
        self.fabrication.update_pcb_version_text()
        self.fabrication.generate_geber(layer_count)
        self.fabrication.generate_excellon()
        self.fabrication.zip_gerber_excellon()
        self.fabrication.generate_cpl()
        self.fabrication.generate_bom()
        self.fabrication.save_fab_version_cache()
        if self.settings.get("gerber", {}).get("delete_old_versions", False):
            self.fabrication.delete_previous_fab_version()

    def copy_part_lcsc(self, *_):
        """Fetch part details from LCSC and show them in a modal."""
        for item in self.footprint_list.GetSelections():
            if lcsc := self.partlist_data_model.get_lcsc(item):
                if wx.TheClipboard.Open():
                    wx.TheClipboard.SetData(wx.TextDataObject(lcsc))
                    wx.TheClipboard.Close()

    def paste_part_lcsc(self, *_):
        """Paste a lcsc number from the clipboard to the current part."""
        text_data = wx.TextDataObject()
        if wx.TheClipboard.Open():
            success = wx.TheClipboard.GetData(text_data)
            wx.TheClipboard.Close()
        if success:
            if (lcsc := self.sanitize_lcsc(text_data.GetText())) != "":
                for item in self.footprint_list.GetSelections():
                    details = self.library.get_part_details(lcsc)
                    params = params_for_part(details)
                    reference = self.partlist_data_model.get_reference(item)
                    self.partlist_data_model.set_lcsc(
                        reference, lcsc, details["type"], details["stock"], params
                    )
                    self.store.set_lcsc(reference, lcsc)

    def add_correction(self, e):
        """Add part correction for the current part."""
        for item in self.footprint_list.GetSelections():
            if e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE:
                if reference := self.partlist_data_model.get_reference(item):
                    CorrectionManagerDialog(
                        self, "^" + re.escape(reference) + "$"
                    ).ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE:
                if footprint := self.partlist_data_model.get_footprint(item):
                    CorrectionManagerDialog(
                        self, "^" + re.escape(footprint)
                    ).ShowModal()
            elif e.GetId() == ID_CONTEXT_MENU_ADD_ROT_BY_NAME:
                if value := self.partlist_data_model.get_value(item):
                    CorrectionManagerDialog(self, re.escape(value)).ShowModal()

    def save_all_mappings(self, *_):
        """Save all mappings."""
        for item in self.partlist_data_model.get_all():
            value = item[1]
            footprint = item[2]
            lcsc = item[3]
            if footprint != "" and value != "" and lcsc != "":
                if self.library.get_mapping_data(footprint, value):
                    self.library.update_mapping_data(footprint, value, lcsc)
                else:
                    self.library.insert_mapping_data(footprint, value, lcsc)
        self.logger.info("All mappings saved")

    def export_to_schematic(self, *_):
        """Dialog to select schematics."""
        with wx.FileDialog(
            self,
            "Select Schematics",
            self.project_path,
            self.schematic_name,
            "KiCad V6 Schematics (*.kicad_sch)|*.kicad_sch",
            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as openFileDialog:
            if openFileDialog.ShowModal() == wx.CANCEL:
                return
            paths = openFileDialog.GetPaths()
            SchematicExport(self).load_schematic(paths)

    def add_foot_mapping(self, *_):
        """Add a footprint mapping."""
        for item in self.footprint_list.GetSelections():
            footprint = self.partlist_data_model.get_footprint(item)
            value = self.partlist_data_model.get_value(item)
            lcsc = self.partlist_data_model.get_lcsc(item)
            if footprint != "" and value != "" and lcsc != "":
                if self.library.get_mapping_data(footprint, value):
                    self.library.update_mapping_data(footprint, value, lcsc)
                else:
                    self.library.insert_mapping_data(footprint, value, lcsc)

    def search_foot_mapping(self, *_):
        """Search for a footprint mapping."""
        for item in self.footprint_list.GetSelections():
            reference = self.partlist_data_model.get_reference(item)
            footprint = self.partlist_data_model.get_footprint(item)
            value = self.partlist_data_model.get_value(item)
            if footprint != "" and value != "":
                if self.library.get_mapping_data(footprint, value):
                    lcsc = self.library.get_mapping_data(footprint, value)[2]
                    self.store.set_lcsc(reference, lcsc)
                    self.logger.info("Found %s", lcsc)
                    details = self.library.get_part_details(lcsc)
                    params = params_for_part(self.library.get_part_details(lcsc))
                    self.partlist_data_model.set_lcsc(
                        reference, lcsc, details["type"], details["stock"], params
                    )

    def sanitize_lcsc(self, lcsc_PN):
        """Sanitize a given LCSC number using a regex."""
        m = re.search("C\\d+", lcsc_PN, re.IGNORECASE)
        if m:
            return m.group(0)
        return ""

    def OnRightDown(self, *_):
        """Right click context menu for action on parts table."""
        right_click_menu = wx.Menu()

        copy_lcsc = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_COPY_LCSC, "Copy LCSC"
        )
        right_click_menu.Append(copy_lcsc)
        right_click_menu.Bind(wx.EVT_MENU, self.copy_part_lcsc, copy_lcsc)

        paste_lcsc = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_PASTE_LCSC, "Paste LCSC"
        )
        right_click_menu.Append(paste_lcsc)
        right_click_menu.Bind(wx.EVT_MENU, self.paste_part_lcsc, paste_lcsc)

        correction_by_reference = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_ADD_ROT_BY_REFERENCE,
            "Add Correction by reference",
        )
        right_click_menu.Append(correction_by_reference)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_reference)

        correction_by_package = wx.MenuItem(
            right_click_menu,
            ID_CONTEXT_MENU_ADD_ROT_BY_PACKAGE,
            "Add Correction by package",
        )
        right_click_menu.Append(correction_by_package)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_package)

        correction_by_name = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_ADD_ROT_BY_NAME, "Add Correction by name"
        )
        right_click_menu.Append(correction_by_name)
        right_click_menu.Bind(wx.EVT_MENU, self.add_correction, correction_by_name)

        find_mapping = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_FIND_MAPPING, "Find LCSC from Mappings"
        )
        right_click_menu.Append(find_mapping)
        right_click_menu.Bind(wx.EVT_MENU, self.search_foot_mapping, find_mapping)

        add_mapping = wx.MenuItem(
            right_click_menu, ID_CONTEXT_MENU_ADD_MAPPING, "Add Footprint Mapping"
        )
        right_click_menu.Append(add_mapping)
        right_click_menu.Bind(wx.EVT_MENU, self.add_foot_mapping, add_mapping)

        self.footprint_list.PopupMenu(right_click_menu)
        right_click_menu.Destroy()  # destroy to avoid memory leak

    def init_logger(self):
        """Initialize logger to log into textbox."""
        root = logging.getLogger()
        # Clear any existing handlers that might be problematic
        root.handlers.clear()
        root.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(funcName)s -  %(message)s",
            datefmt="%Y.%m.%d %H:%M:%S",
        )
        # Only add stderr handler if stderr is available
        if sys.stderr is not None:
            self.logging_handler1 = logging.StreamHandler(sys.stderr)
            self.logging_handler1.setLevel(logging.DEBUG)
            self.logging_handler1.setFormatter(formatter)
            root.addHandler(self.logging_handler1)

        self.logging_handler2 = LogBoxHandler(self)
        self.logging_handler2.setLevel(logging.DEBUG)
        self.logging_handler2.setFormatter(formatter)
        root.addHandler(self.logging_handler2)

        self.logger = logging.getLogger(__name__)

    def __del__(self):
        """Cleanup."""
        pass


class LogBoxHandler(logging.StreamHandler):
    """Logging class for the logging textbox at th ebottom of the mainwindow."""

    def __init__(self, event_destination):
        logging.StreamHandler.__init__(self)
        self.event_destination = event_destination

    def emit(self, record):
        """Marshal the event over to the main thread."""
        msg = self.format(record)
        wx.QueueEvent(self.event_destination, LogboxAppendEvent(msg=f"{msg}\n"))
