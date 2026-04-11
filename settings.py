"""Contains the settings dialog."""

import logging

import wx  # pylint: disable=import-error

from .events import UpdateSetting
from .helpers import HighResWxSize, loadBitmapScaled

# Fixed icon size in the settings grid (pixels, before DPI scaling) — 75% bigger than original
ICON_SIZE = 56


class SettingsDialog(wx.Dialog):
    """Dialog for plugin settings."""

    def __init__(self, parent):
        wx.Dialog.__init__(
            self,
            parent,
            id=wx.ID_ANY,
            title="JLCPCB tools settings",
            pos=wx.DefaultPosition,
            size=HighResWxSize(parent.window, wx.Size(900, 650)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self.logger = logging.getLogger(__name__)
        self.parent = parent

        # ---------------------------------------------------------------------
        # Hotkeys
        # ---------------------------------------------------------------------
        quitid = wx.NewId()
        self.Bind(wx.EVT_MENU, self.quit_dialog, id=quitid)
        entries = [wx.AcceleratorEntry(), wx.AcceleratorEntry(), wx.AcceleratorEntry()]
        entries[0].Set(wx.ACCEL_CTRL, ord("W"), quitid)
        entries[1].Set(wx.ACCEL_CTRL, ord("Q"), quitid)
        entries[2].Set(wx.ACCEL_SHIFT, wx.WXK_ESCAPE, quitid)
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

        # ---------------------------------------------------------------------
        # Layout helpers
        # ---------------------------------------------------------------------
        # The settings are laid out as two side-by-side panels, each with its
        # own icon + checkbox grid.  This keeps everything aligned without
        # requiring a single giant grid that's hard to balance.
        icon_px = int(ICON_SIZE * self.parent.scale_factor)

        # Font size — read from saved settings so it matches the toolbars
        _font_size = int(self.parent.settings.get("general", {}).get("font_size", 11))
        _font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        _font.SetPointSize(_font_size)

        def _icon(filename):
            """Return a StaticBitmap whose bitmap is scaled exactly to icon_px × icon_px.

            loadBitmapScaled handles dark-mode pixel conversion; we then rescale
            the result to the target size regardless of the source image dimensions.
            """
            bmp = loadBitmapScaled(filename, self.parent.scale_factor, static=True)
            # Rescale to the desired display size
            img = bmp.ConvertToImage()
            img = img.Scale(icon_px, icon_px, wx.IMAGE_QUALITY_HIGH)
            bmp_scaled = wx.Bitmap(img)
            widget = wx.StaticBitmap(self, wx.ID_ANY, bmp_scaled)
            widget.SetMinSize(wx.Size(icon_px, icon_px))
            return widget

        def _checkbox(label, name, tooltip):
            """Return a CheckBox with a stable label."""
            cb = wx.CheckBox(self, id=wx.ID_ANY, label=label, name=name)
            cb.SetToolTip(wx.ToolTip(tooltip))
            cb.SetFont(_font)
            cb.Bind(wx.EVT_CHECKBOX, self.update_settings)
            return cb

        # Left panel grid (col 0 = icon, col 1 = checkbox)
        left_grid = wx.FlexGridSizer(cols=2, vgap=12, hgap=10)
        left_grid.AddGrowableCol(1, 1)

        # Right panel grid
        right_grid = wx.FlexGridSizer(cols=2, vgap=12, hgap=10)
        right_grid.AddGrowableCol(1, 1)

        # Which grid each row goes into — alternating fills both columns evenly
        _left_rows = []
        _right_rows = []

        def _add_left(icon_widget, ctrl_widget):
            left_grid.Add(icon_widget, 0, wx.ALIGN_CENTER | wx.LEFT, 8)
            left_grid.Add(ctrl_widget, 0, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND | wx.RIGHT, 8)

        def _add_right(icon_widget, ctrl_widget):
            right_grid.Add(icon_widget, 0, wx.ALIGN_CENTER | wx.LEFT, 8)
            right_grid.Add(ctrl_widget, 0, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND | wx.RIGHT, 8)

        # Shorthand — first 5 go left, rest go right
        _col = [0]
        def _add_row(icon_widget, ctrl_widget):
            if _col[0] < 5:
                _add_left(icon_widget, ctrl_widget)
            else:
                _add_right(icon_widget, ctrl_widget)
            _col[0] += 1

        # ---------------------------------------------------------------------
        # Tented vias
        # ---------------------------------------------------------------------
        self.tented_vias_image = _icon("tented.png")
        self.tented_vias_setting = _checkbox(
            "Tented vias",
            "gerber_tented_vias",
            "Cover vias with soldermask",
        )
        _add_row(self.tented_vias_image, self.tented_vias_setting)

        # ---------------------------------------------------------------------
        # Fill zones
        # ---------------------------------------------------------------------
        self.fill_zones_image = _icon("fill-zones.png")
        self.fill_zones_setting = _checkbox(
            "Fill zones before export",
            "gerber_fill_zones",
            "Refill copper zones before generating Gerbers",
        )
        _add_row(self.fill_zones_image, self.fill_zones_setting)

        # ---------------------------------------------------------------------
        # Run DRC  — mdi-check.png (tick / pass icon)
        # ---------------------------------------------------------------------
        self.run_drc_image = _icon("mdi-check.png")
        self.run_drc_setting = _checkbox(
            "Run DRC before export",
            "gerber_run_drc",
            "Run design rule check before generating fabrication files",
        )
        _add_row(self.run_drc_image, self.run_drc_setting)

        # ---------------------------------------------------------------------
        # Plot values
        # ---------------------------------------------------------------------
        self.plot_values_image = _icon("plot_values.png")
        self.plot_values_setting = _checkbox(
            "Plot values on silkscreen",
            "gerber_plot_values",
            "Include component values on silkscreen layers",
        )
        _add_row(self.plot_values_image, self.plot_values_setting)

        # ---------------------------------------------------------------------
        # Plot references
        # ---------------------------------------------------------------------
        self.plot_references_image = _icon("plot_refs.png")
        self.plot_references_setting = _checkbox(
            "Plot references on silkscreen",
            "gerber_plot_references",
            "Include reference designators on silkscreen layers",
        )
        _add_row(self.plot_references_image, self.plot_references_setting)

        # ---------------------------------------------------------------------
        # LCSC priority
        # ---------------------------------------------------------------------
        self.lcsc_priority_image = _icon("schematic.png")
        self.lcsc_priority_setting = _checkbox(
            "Schematic LCSC numbers have priority",
            "general_lcsc_priority",
            "LCSC numbers from the schematic override the parts database",
        )
        _add_row(self.lcsc_priority_image, self.lcsc_priority_setting)

        # ---------------------------------------------------------------------
        # Parts without LCSC in BOM/CPL
        # ---------------------------------------------------------------------
        self.lcsc_bom_cpl_image = _icon("bom.png")
        self.lcsc_bom_cpl_setting = _checkbox(
            "Add parts without LCSC number to BOM/CPL",
            "gerber_lcsc_bom_cpl",
            "Include parts that have no LCSC number in the BOM and CPL",
        )
        _add_row(self.lcsc_bom_cpl_image, self.lcsc_bom_cpl_setting)

        # ---------------------------------------------------------------------
        # Order number placeholder  — order_number.png
        # ---------------------------------------------------------------------
        self.order_number_image = _icon("order_number.png")
        self.order_number_setting = _checkbox(
            "Check for order/serial number placeholder",
            "general_order_number",
            "Warn if the JLCJLCJLCJLC order number placeholder is missing",
        )
        _add_row(self.order_number_image, self.order_number_setting)

        # ---------------------------------------------------------------------
        # Auto-increment version  — mdi-lead-pencil.png (edit/version icon)
        # The row contains the checkbox; below it (same grid cell, second row)
        # sits the increment spinner.
        # ---------------------------------------------------------------------
        self.auto_version_image = _icon("mdi-lead-pencil.png")

        self.auto_version_setting = _checkbox(
            "Auto-increment output version number",
            "gerber_auto_version",
            "Append an incrementing version number to each export (e.g. BoardName.3.zip)",
        )

        self.version_style_label = wx.StaticText(self, label="Version style:")
        self.version_style_label.SetFont(_font)

        # Map display label → settings key
        self._version_style_choices = [
            ("1, 2, 3  (integer)",   "integer"),
            ("1.0, 1.1, 1.2  (×0.1)", "decimal1"),
            ("1.00, 1.01, 1.02  (×0.01)", "decimal2"),
            ("A, B, C  (alphabetic)", "alpha"),
        ]
        self.version_style_ctrl = wx.Choice(
            self, id=wx.ID_ANY,
            choices=[c[0] for c in self._version_style_choices],
        )
        self.version_style_ctrl.SetFont(_font)
        self.version_style_ctrl.SetToolTip(
            wx.ToolTip("How output file version numbers are formatted and incremented")
        )
        self.version_style_ctrl.Bind(wx.EVT_CHOICE, self.on_version_style_changed)

        increment_row = wx.BoxSizer(wx.HORIZONTAL)
        increment_row.Add(self.version_style_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        increment_row.Add(self.version_style_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)

        # Stack checkbox + spinner in a vertical sizer for the right column
        auto_version_ctrl_col = wx.BoxSizer(wx.VERTICAL)
        auto_version_ctrl_col.Add(self.auto_version_setting, 0, wx.ALIGN_CENTER_VERTICAL)
        auto_version_ctrl_col.Add(increment_row, 0, wx.TOP | wx.LEFT, 4)

        _add_row(self.auto_version_image, auto_version_ctrl_col)

        # ---------------------------------------------------------------------
        # Delete old versions  — mdi-trash-can-outline.png
        # ---------------------------------------------------------------------
        self.delete_old_versions_image = _icon("mdi-trash-can-outline.png")
        self.delete_old_versions_setting = _checkbox(
            "Delete previous version after export",
            "gerber_delete_old_versions",
            "Remove the previous version's zip, BOM and CPL after a successful export",
        )
        _add_row(self.delete_old_versions_image, self.delete_old_versions_setting)

        # ---------------------------------------------------------------------
        # Font size
        # ---------------------------------------------------------------------
        self.font_size_image = _icon("mdi-magnify.png")

        self.font_size_label = wx.StaticText(self, label="UI font size:")
        self.font_size_label.SetFont(_font)
        self.font_size_ctrl = wx.SpinCtrl(
            self, id=wx.ID_ANY, value="11", min=7, max=24, initial=11
        )
        self.font_size_ctrl.SetFont(_font)
        self.font_size_ctrl.SetToolTip(
            wx.ToolTip("Point size for toolbar and settings text (takes effect on next open)")
        )
        self.font_size_ctrl.Bind(wx.EVT_SPINCTRL, self.on_font_size_changed)

        font_size_note = wx.StaticText(self, label="(restarts plugin to apply)")
        font_size_note.SetFont(_font.Smaller())

        font_size_ctrl_col = wx.BoxSizer(wx.VERTICAL)
        font_size_row = wx.BoxSizer(wx.HORIZONTAL)
        font_size_row.Add(self.font_size_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        font_size_row.Add(self.font_size_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        font_size_ctrl_col.Add(font_size_row, 0)
        font_size_ctrl_col.Add(font_size_note, 0, wx.TOP, 2)

        _add_right(self.font_size_image, font_size_ctrl_col)

        # ---------------------------------------------------------------------
        # Main layout — two side-by-side grids
        # ---------------------------------------------------------------------
        columns = wx.BoxSizer(wx.HORIZONTAL)
        columns.Add(left_grid, 1, wx.ALL | wx.EXPAND, 16)
        columns.Add(wx.StaticLine(self, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 16)
        columns.Add(right_grid, 1, wx.ALL | wx.EXPAND, 16)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(columns, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Layout()
        self.Centre(wx.BOTH)

        self.load_settings()

    # ------------------------------------------------------------------
    # Update helpers — just set the value, labels never change
    # ------------------------------------------------------------------

    def update_tented_vias(self, value):
        self.tented_vias_setting.SetValue(value)

    def update_fill_zones(self, value):
        self.fill_zones_setting.SetValue(value)

    def update_run_drc(self, value):
        self.run_drc_setting.SetValue(value)

    def update_plot_values(self, value):
        self.plot_values_setting.SetValue(value)

    def update_plot_references(self, value):
        self.plot_references_setting.SetValue(value)

    def update_lcsc_priority(self, value):
        self.lcsc_priority_setting.SetValue(value)

    def update_lcsc_bom_cpl(self, value):
        self.lcsc_bom_cpl_setting.SetValue(value)

    def update_order_number(self, value):
        self.order_number_setting.SetValue(value)

    def update_auto_version(self, value):
        self.auto_version_setting.SetValue(value)
        self.version_style_ctrl.Enable(value)
        self.version_style_label.Enable(value)

    def update_delete_old_versions(self, value):
        self.delete_old_versions_setting.SetValue(value)

    def update_version_style(self, style_key):
        """Select the correct item in the version style dropdown."""
        keys = [c[1] for c in self._version_style_choices]
        idx = keys.index(style_key) if style_key in keys else 0
        self.version_style_ctrl.SetSelection(idx)

    def update_font_size(self, value):
        self.font_size_ctrl.SetValue(int(value))

    def on_font_size_changed(self, event):
        """Persist font size and close/reopen plugin to apply."""
        size = self.font_size_ctrl.GetValue()
        wx.PostEvent(
            self.parent,
            UpdateSetting(section="general", setting="font_size", value=size),
        )
        self.parent.save_settings()

    def on_version_style_changed(self, event):
        """Persist the chosen version style."""
        idx = self.version_style_ctrl.GetSelection()
        style_key = self._version_style_choices[idx][1]
        wx.PostEvent(
            self.parent,
            UpdateSetting(section="gerber", setting="version_style", value=style_key),
        )

    def load_settings(self):
        """Load settings and initialise all controls."""
        g = self.parent.settings.get("gerber", {})
        gen = self.parent.settings.get("general", {})
        self.update_tented_vias(g.get("tented_vias", True))
        self.update_fill_zones(g.get("fill_zones", True))
        self.update_run_drc(g.get("run_drc", True))
        self.update_plot_values(g.get("plot_values", True))
        self.update_plot_references(g.get("plot_references", True))
        self.update_lcsc_priority(gen.get("lcsc_priority", False))
        self.update_lcsc_bom_cpl(g.get("lcsc_bom_cpl", True))
        self.update_order_number(gen.get("order_number", False))
        self.update_auto_version(g.get("auto_version", True))
        self.update_version_style(g.get("version_style", "integer"))
        self.update_delete_old_versions(g.get("delete_old_versions", False))
        self.update_font_size(gen.get("font_size", 11))

    def update_settings(self, event):
        """Persist a changed setting."""
        section, name = event.GetEventObject().GetName().split("_", 1)
        value = event.GetEventObject().GetValue()
        getattr(self, f"update_{name}")(value)
        wx.PostEvent(
            self.parent,
            UpdateSetting(section=section, setting=name, value=value),
        )

    def quit_dialog(self, *_):
        """Close this dialog."""
        self.Destroy()
        self.EndModal(0)
