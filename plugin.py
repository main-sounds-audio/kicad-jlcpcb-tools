"""Contains the Action Plugin."""

import os

from pcbnew import ActionPlugin  # pylint: disable=import-error

from .mainwindow import JLCPCBTools

_open_window = None


class JLCPCBPlugin(ActionPlugin):
    """JLCPCBPlugin instance of ActionPlugin."""

    def defaults(self):
        """Define defaults."""
        # pylint: disable=attribute-defined-outside-init
        self.name = "JLCPCB Tools"
        self.category = "Fabrication data generation"
        self.description = (
            "Generate JLCPCB-compatible Gerber, Excellon, BOM and CPL files"
        )
        self.show_toolbar_button = True
        path, _ = os.path.split(os.path.abspath(__file__))
        self.icon_file_name = os.path.join(path, "jlcpcb-icon.png")
        self._pcbnew_frame = None

    def Run(self):
        """Overwrite Run. Raises the existing window if already open."""
        global _open_window
        if _open_window is not None:
            try:
                if _open_window.IsShown():
                    _open_window.Raise()
                    _open_window.SetFocus()
                    return
            except Exception:
                pass
        _open_window = JLCPCBTools(None)
        _open_window.Center()
        _open_window.Show()
        # Defer focus so the event loop has processed Show before we activate.
        # Without this, macOS renders the window as "inactive" (grey icons)
        # until the user clicks into it.
        import wx as _wx
        _wx.CallLater(150, lambda: (
            _open_window.Raise(),
            _open_window.SetFocus(),
        ) if _open_window and _open_window.IsShown() else None)
