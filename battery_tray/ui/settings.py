"""Settings dialog: edit the user-tunable config (poll rate, font, color, debug).

The dialog only knows how to read/write a :class:`Config`; persisting the result
and refreshing the running tray is the caller's job (see ``app._open_settings``).
The one exception is "Start with Windows": it is not part of ``Config`` --
Windows' ``Run`` key is its only store (see :mod:`battery_tray.autostart`) -- so the dialog
reads it on open and writes it itself on OK.

The font picker lists monospaced faces by default (the indicator wants
fixed-width digits) and previews each one in its own face; the "Show all fonts"
checkbox widens it to every installed face. Face names are resolved back to a
``.ttf``/``.otf`` file so PIL can load them. Every font, text-size, outline or
battery-icon change is also offered to the caller for a live tray repaint,
without waiting for OK.
"""

from __future__ import annotations

import os
import winreg
from collections.abc import Callable
from dataclasses import dataclass

import wx
import wx.adv
from PIL import ImageFont

from .. import autostart
from ..build_info import version_string
from ..config import LOW_THRESHOLD, MID_THRESHOLD, TEXT_SIZE_MAX, TEXT_SIZE_MIN, TRAY_ICONS_MAX, Config
from ..i18n import LANGUAGES, tr
from ..logging_setup import open_log
from ..resources import icon_path


@dataclass(frozen=True)
class PreviewHooks:
    """Callbacks the dialog fires so the caller can repaint the tray live.

    Each fires every time its control changes (and on "Reset to defaults");
    ``font`` receives the picked font file, or ``None`` when the selection
    clears. The caller must drop the previews once the dialog returns.
    """

    font: Callable[[str | None], None] | None = None
    size: Callable[[int], None] | None = None
    outline: Callable[[bool], None] | None = None
    battery_icon: Callable[[bool], None] | None = None


def open_settings(
    parent: wx.Window,
    config: Config,
    preview: PreviewHooks | None = None,
    on_test_notification: Callable[[], None] | None = None,
) -> bool:
    """Show the modal settings dialog, centered on screen.

    On OK the edited values are written back onto ``config`` in place and
    ``True`` is returned; on Cancel nothing changes and ``False`` is returned.
    ``preview`` hooks, when given, repaint the tray live as the user edits
    (see :class:`PreviewHooks`). ``on_test_notification`` backs the "Test"
    button next to the low-battery notification checkbox.
    """
    dialog = _SettingsDialog(parent, config, preview or PreviewHooks(), on_test_notification)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return False
        dialog.apply_to(config)
        return True
    finally:
        dialog.Destroy()


def _font_files() -> dict[str, str]:
    """Map a lowercased font face name to its absolute font file (Windows).

    Built from the per-machine and per-user font registry keys. The regular
    weight of a family wins because its value name (e.g. ``"Consolas
    (TrueType)"``) strips to the bare face name, while ``"Consolas Bold"`` does
    not collide with it.
    """
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    result: dict[str, str] = {}
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, R"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, R"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not value:
                    continue
                face = name.split(" (")[0].strip().lower()
                file = value if os.path.isabs(value) else os.path.join(fonts_dir, value)
                result.setdefault(face, file)
    return result


def _collect_faces() -> tuple[list[str], set[str], dict[str, str], dict[str, str]]:
    """Every installed face plus the lookups the picker and previews need.

    Returns the sorted face list, the lowercased monospaced faces, and the
    face-to-file / file-to-face maps (built from :func:`_font_files`).
    """
    file_map = _font_files()
    all_faces = sorted(
        face
        for face in wx.FontEnumerator.GetFacenames()
        if not face.startswith("@") and face.lower() in file_map
    )
    mono_faces = {
        face.lower()
        for face in wx.FontEnumerator.GetFacenames(fixedWidthOnly=True)
        if not face.startswith("@")
    }
    face_to_path = {face: file_map[face.lower()] for face in all_faces}
    path_to_face = {
        os.path.basename(path).lower(): face for face, path in face_to_path.items()
    }
    return all_faces, mono_faces, face_to_path, path_to_face


def _rgb(picker: wx.ColourPickerCtrl) -> tuple[int, int, int]:
    colour = picker.GetColour()
    return (colour.Red(), colour.Green(), colour.Blue())


class _FontPicker(wx.adv.OwnerDrawnComboBox):
    """Read-only combo that previews each face in its own font."""

    def __init__(self, parent: wx.Window, faces: list[str]):
        super().__init__(parent, choices=faces, style=wx.CB_READONLY)
        self._faces = faces

    def set_faces(self, faces: list[str]) -> None:
        """Replace the list of faces, keeping the owner-drawn preview working."""
        self._faces = faces
        self.Set(faces)

    def OnDrawItem(  # noqa: N802 (wx override)
        self, dc: wx.DC, rect: wx.Rect, item: int, flags: int
    ) -> None:
        if item == wx.NOT_FOUND:
            return
        face = self._faces[item]
        dc.SetFont(
            wx.Font(11, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL, faceName=face)
        )
        # The closed control also arrives with the SELECTED flag; it paints its
        # own light field background, so the text must stay dark there --
        # highlight white would turn white-on-white.
        selected = bool(flags & wx.adv.ODCB_PAINTING_SELECTED)
        in_control = bool(flags & wx.adv.ODCB_PAINTING_CONTROL)
        colour = (
            wx.SYS_COLOUR_HIGHLIGHTTEXT if selected and not in_control else wx.SYS_COLOUR_WINDOWTEXT
        )
        dc.SetTextForeground(wx.SystemSettings.GetColour(colour))
        dc.DrawText(face, rect.x + 4, rect.y + (rect.height - dc.GetCharHeight()) // 2)

    def OnMeasureItem(self, item: int) -> int:  # noqa: N802 (wx override)
        return 24


class _SettingsDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        config: Config,
        preview: PreviewHooks,
        on_test_notification: Callable[[], None] | None = None,
    ):
        title = tr("{name} {version} settings", name=config.display_name, version=version_string())
        super().__init__(parent, title=title)
        self.SetIcon(wx.Icon(icon_path("app.ico")))
        self._preview = preview

        self._all_faces, self._mono_faces, self._face_to_path, self._path_to_face = _collect_faces()

        # Three titled sections; the label columns line up within each one.
        look, notify, general = (self._new_grid() for _ in range(3))

        self._build_font_section(look)

        self._build_size_row(look, config.text_size)

        self._text_outline = self._add_checkbox(
            look,
            "Text outline:",
            config.text_outline,
            "Draw a contrasting outline around the tray digits.\n"
            "Keeps them readable on a same-colored taskbar.",
        )
        self._text_outline.Bind(wx.EVT_CHECKBOX, self._on_outline)

        self._build_color_row(look, config)

        self._dynamic_color = self._add_checkbox(
            look,
            "Color by charge level:",
            config.dynamic_color,
            tr(
                "Color the battery percent by charge: the font color\n"
                "above {mid}%, then the two colors picked next to it\n"
                "at ≤ {mid}% and ≤ {low}%.",
                mid=MID_THRESHOLD,
                low=LOW_THRESHOLD,
            ),
        )
        self._dynamic_color.Bind(wx.EVT_CHECKBOX, self._on_dynamic_color)
        self._battery_icon = self._add_checkbox(
            look,
            "Show battery icon:",
            config.battery_icon,
            "Draw a battery filled to the charge level instead of\n"
            "the percent digits. The exact number moves to the\n"
            "tray tooltip.",
        )
        self._battery_icon.Bind(wx.EVT_CHECKBOX, self._on_battery_icon)
        self._build_tray_icons_row(look, config.tray_icons)
        self._build_notify_rows(notify, config, on_test_notification)

        self._build_poll_row(general, config.poll_rate)
        self._build_language_row(general, config.language)
        self._app_name = config.app_name
        self._autostart_was = autostart.is_enabled(config.app_name)
        self._autostart = self._add_checkbox(
            general,
            "Start with Windows:",
            self._autostart_was,
            "Run the program when you sign in to Windows.\n"
            "Also shown on the Startup tab of Task Manager.",
        )
        self._debug = self._add_checkbox(general, "Debug logging:", config.debug)

        reset = wx.Button(self, label=tr("Reset to defaults"))
        reset.Bind(wx.EVT_BUTTON, self._on_reset)
        show_log = wx.Button(self, label=tr("Open log"))
        show_log.SetToolTip(tr("Open app.log -- attach it when reporting a problem."))
        show_log.Bind(wx.EVT_BUTTON, lambda _evt: open_log(config.app_name))
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        # By id, not GetAffirmativeButton(): wxPython 4.2 (the release build's
        # pinned version) has only the setters.
        for button_id, label in ((wx.ID_OK, "OK"), (wx.ID_CANCEL, "Cancel")):
            button = self.FindWindowById(button_id, self)
            if button is not None:
                button.SetLabel(tr(label))

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        bottom.Add(reset, 0)
        bottom.Add(show_log, 0, wx.LEFT, 8)
        bottom.AddStretchSpacer()
        bottom.Add(buttons, 0)

        outer = wx.BoxSizer(wx.VERTICAL)
        self._align_label_columns((look, notify, general))
        for title, grid in (
            ("Tray icon", look),
            ("Notifications", notify),
            ("General", general),
        ):
            box = wx.StaticBoxSizer(wx.VERTICAL, self, tr(title))
            box.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
            outer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        outer.AddSpacer(12)
        outer.Add(bottom, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(outer)
        self.SetMinSize(self.GetSize())
        self.CentreOnScreen()

        self._restore_font_selection(config)
        # Hide after fitting: the dialog keeps the width of the shown state, so
        # toggling the checkbox never resizes the window under the cursor.
        self._update_bands()
        # Validate the chosen font before the OK button closes the dialog.
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _align_label_columns(grids: tuple[wx.FlexGridSizer, ...]) -> None:
        """Give every section's label column the widest label's width, so the
        controls of all three sections start on one vertical line."""
        labels = [
            item.GetWindow()
            for grid in grids
            for index, item in enumerate(grid.GetChildren())
            if index % 2 == 0 and item.GetWindow() is not None
        ]
        width = max(label.GetBestSize().width for label in labels)
        for label in labels:
            label.SetMinSize(wx.Size(width, -1))

    @staticmethod
    def _new_grid() -> wx.FlexGridSizer:
        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=12)
        grid.AddGrowableCol(1, 1)
        return grid

    def _add_checkbox(
        self, grid: wx.FlexGridSizer, label: str, value: bool, tip: str | None = None
    ) -> wx.CheckBox:
        """Append a labelled checkbox row; the tooltip covers both cells."""
        text = wx.StaticText(self, label=tr(label))
        box = wx.CheckBox(self)
        box.SetValue(value)
        if tip:
            tip = tr(tip)
            text.SetToolTip(tip)
            box.SetToolTip(tip)
        grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(box, 0, wx.ALIGN_CENTER_VERTICAL)
        return box

    def _build_font_section(self, grid: wx.FlexGridSizer) -> None:
        """Append the font picker row and the "Show all fonts" checkbox."""
        self._font_label = wx.StaticText(self, label=tr("Font (monospaced):"))
        grid.Add(self._font_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self._font = _FontPicker(self, list(self._all_faces))
        self._font.Bind(wx.EVT_COMBOBOX, self._on_font_selected)

        # Next to the picker, not a row of its own: it only filters that list.
        self._all_fonts = wx.CheckBox(self, label=tr("Show all fonts"))
        self._all_fonts.SetToolTip(
            tr(
                "List every installed font, not just monospaced ones.\n"
                "The digits are measured in the chosen face, so any font fits."
            )
        )
        self._all_fonts.Bind(wx.EVT_CHECKBOX, self._on_all_fonts)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self._font, 1, wx.ALIGN_CENTER_VERTICAL)
        row.Add(self._all_fonts, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        grid.Add(row, 1, wx.EXPAND)

    def _build_notify_rows(
        self, grid: wx.FlexGridSizer, config: Config, on_test: Callable[[], None] | None
    ) -> None:
        """Append the low-battery notification switch, its levels and repeat interval."""
        levels = tr("{high}% and {low}%", high=config.notify_level_high, low=config.notify_level_low)
        tip = tr("Show a Windows notification when the charge drops to {levels}.", levels=levels)
        text = wx.StaticText(self, label=tr("Low battery notification:"))
        text.SetToolTip(tip)
        self._low_battery_notify = wx.CheckBox(self)
        self._low_battery_notify.SetValue(config.low_battery_notify)
        self._low_battery_notify.SetToolTip(tip)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self._low_battery_notify, 0, wx.ALIGN_CENTER_VERTICAL)
        if on_test is not None:
            test = wx.Button(self, label=tr("Test"))
            test.SetToolTip(tr("Show the notification now to check the Windows notification settings."))
            test.Bind(wx.EVT_BUTTON, lambda _evt: on_test())
            row.Add(test, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(row, 0, wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(self, label=tr("Notify at (%):")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._notify_high = wx.SpinCtrl(self, min=1, max=99, initial=config.notify_level_high)
        self._notify_low = wx.SpinCtrl(self, min=1, max=99, initial=config.notify_level_low)
        levels_row = wx.BoxSizer(wx.HORIZONTAL)
        levels_row.Add(self._notify_high, 0, wx.ALIGN_CENTER_VERTICAL)
        and_label = wx.StaticText(self, label=tr("and"))
        levels_row.Add(and_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        levels_row.Add(self._notify_low, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(levels_row, 0, wx.ALIGN_CENTER_VERTICAL)

        repeat_tip = tr(
            "While the charge stays at or below the lower level, repeat the\n"
            "notification every this many minutes. 0 turns repeats off."
        )
        repeat_label = wx.StaticText(self, label=tr("Repeat below lower level (min):"))
        repeat_label.SetToolTip(repeat_tip)
        self._notify_repeat = wx.SpinCtrl(self, min=0, max=1440, initial=config.notify_repeat_minutes)
        self._notify_repeat.SetToolTip(repeat_tip)
        grid.Add(repeat_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._notify_repeat, 0, wx.ALIGN_CENTER_VERTICAL)

    def _build_tray_icons_row(self, grid: wx.FlexGridSizer, tray_icons: int) -> None:
        """Append the "how many devices at once" spinner."""
        tip = tr(
            "Show up to this many connected devices at once, one tray icon\n"
            "each. With several, the tray menu pins devices by checkbox."
        )
        label = wx.StaticText(self, label=tr("Devices in tray:"))
        label.SetToolTip(tip)
        self._tray_icons = wx.SpinCtrl(self, min=1, max=TRAY_ICONS_MAX, initial=tray_icons)
        self._tray_icons.SetMinSize(self.FromDIP(wx.Size(80, -1)))
        self._tray_icons.SetToolTip(tip)
        grid.Add(label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self._tray_icons, 0, wx.ALIGN_CENTER_VERTICAL)

    def _build_poll_row(self, grid: wx.FlexGridSizer, poll_rate: int) -> None:
        """Append the poll interval spinner, sized for its numbers rather than the row."""
        grid.Add(wx.StaticText(self, label=tr("Poll interval (s):")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._poll = wx.SpinCtrl(self, min=1, max=3600, initial=int(poll_rate))
        self._poll.SetMinSize(self.FromDIP(wx.Size(80, -1)))
        grid.Add(self._poll, 0, wx.ALIGN_CENTER_VERTICAL)

    def _build_language_row(self, grid: wx.FlexGridSizer, language: str) -> None:
        """Append the interface language picker (applies when the dialog closes)."""
        grid.Add(wx.StaticText(self, label=tr("Language:")), 0, wx.ALIGN_CENTER_VERTICAL)
        names = [tr(name) if code == "auto" else name for code, name in LANGUAGES.items()]
        self._language = wx.Choice(self, choices=names)
        codes = list(LANGUAGES)
        self._language.SetSelection(codes.index(language) if language in codes else 0)
        grid.Add(self._language, 0, wx.ALIGN_CENTER_VERTICAL)

    def _build_size_row(self, grid: wx.FlexGridSizer, text_size: int) -> None:
        """Append the text-size slider row with its live percent label."""
        grid.Add(wx.StaticText(self, label=tr("Text size:")), 0, wx.ALIGN_CENTER_VERTICAL)
        size_value = min(TEXT_SIZE_MAX, max(TEXT_SIZE_MIN, text_size))
        self._size = wx.Slider(
            self, minValue=TEXT_SIZE_MIN, maxValue=TEXT_SIZE_MAX, value=size_value,
            style=wx.SL_HORIZONTAL,
        )
        self._size.SetToolTip(
            tr("Scale the tray digits.\n100% fills the icon; lower values shrink.")
        )
        self._size_label = wx.StaticText(self, label=f"{size_value}%")
        # Reserve the widest value's width so the row never jumps; measured in
        # the label's own font, so it survives DPI scaling (a fixed 40px clipped
        # the "%" at 150%).
        widest = self._size_label.GetTextExtent(f"{TEXT_SIZE_MAX}%").width
        self._size_label.SetMinSize((widest, -1))
        self._size.Bind(wx.EVT_SLIDER, self._on_size_changed)
        size_row = wx.BoxSizer(wx.HORIZONTAL)
        size_row.Add(self._size, 1, wx.ALIGN_CENTER_VERTICAL)
        size_row.Add(self._size_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        grid.Add(size_row, 1, wx.EXPAND)

    def _build_color_row(self, grid: wx.FlexGridSizer, config: Config) -> None:
        """Append the font color picker with its low-band pickers."""
        grid.Add(wx.StaticText(self, label=tr("Font color:")), 0, wx.ALIGN_CENTER_VERTICAL)
        self._color = wx.ColourPickerCtrl(self, colour=wx.Colour(*config.foreground_color))
        self._mid_color = wx.ColourPickerCtrl(self, colour=wx.Colour(*config.mid_color))
        self._low_color = wx.ColourPickerCtrl(self, colour=wx.Colour(*config.low_color))
        # The low-band pickers sit next to the font color -- it is the color of
        # the top band -- and only show up while "Color by charge level" is on.
        colors = wx.BoxSizer(wx.HORIZONTAL)
        colors.Add(self._color, 0, wx.ALIGN_CENTER_VERTICAL)
        self._band_widgets: list[wx.Window] = []
        for threshold, picker in ((MID_THRESHOLD, self._mid_color), (LOW_THRESHOLD, self._low_color)):
            text = wx.StaticText(self, label=f"≤ {threshold}%:")
            colors.Add(text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
            colors.Add(picker, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
            self._band_widgets += [text, picker]
        grid.Add(colors, 1, wx.EXPAND)

    def _restore_font_selection(self, config: Config) -> None:
        """Show the saved font, widening the list for a proportional face."""
        # Start widened if the saved font is a proportional face, so it shows.
        current_face = self._path_to_face.get(os.path.basename(config.font).lower())
        self._all_fonts.SetValue(
            bool(current_face) and current_face.lower() not in self._mono_faces
        )
        self._populate_faces()
        self._select_font(config.font)

    def _select_font(self, font: str) -> None:
        face = self._path_to_face.get(os.path.basename(font).lower())
        if face:
            self._font.SetStringSelection(face)

    def _populate_faces(self) -> None:
        """Fill the picker with all faces or just the monospaced ones."""
        show_all = self._all_fonts.GetValue()
        faces = (
            list(self._all_faces)
            if show_all
            else [face for face in self._all_faces if face.lower() in self._mono_faces]
        )
        keep = self._font.GetStringSelection()
        self._font.set_faces(faces)
        if keep in faces:
            self._font.SetStringSelection(keep)
        self._font_label.SetLabel(tr("Font:") if show_all else tr("Font (monospaced):"))

    def _update_bands(self) -> None:
        """Show the low-band pickers only while "Color by charge level" is on."""
        show = self._dynamic_color.GetValue()
        for widget in self._band_widgets:
            widget.Show(show)
        self.Layout()

    # --- events -------------------------------------------------------------

    def _on_font_selected(self, evt: wx.CommandEvent) -> None:
        self._emit_font_preview()
        evt.Skip()

    def _emit_font_preview(self) -> None:
        """Offer the current font pick to the live tray preview, if any."""
        if self._preview.font is None:
            return
        face = self._font.GetStringSelection()
        path = self._face_to_path.get(face) if face else None
        if path is not None:
            try:
                ImageFont.truetype(path, 16)
            except OSError:
                path = None  # uninstalled mid-dialog; fall back to the current font
        self._preview.font(path)

    def _on_all_fonts(self, evt: wx.CommandEvent) -> None:
        self._populate_faces()
        self._emit_font_preview()  # the selection may have cleared
        evt.Skip()

    def _on_size_changed(self, evt: wx.CommandEvent) -> None:
        self._size_label.SetLabel(f"{self._size.GetValue()}%")
        self._emit_size_preview()
        evt.Skip()

    def _emit_size_preview(self) -> None:
        """Offer the current size pick to the live tray preview, if any."""
        if self._preview.size is not None:
            self._preview.size(self._size.GetValue())

    def _on_outline(self, evt: wx.CommandEvent) -> None:
        self._emit_outline_preview()
        evt.Skip()

    def _emit_outline_preview(self) -> None:
        """Offer the current outline pick to the live tray preview, if any."""
        if self._preview.outline is not None:
            self._preview.outline(self._text_outline.GetValue())

    def _on_battery_icon(self, evt: wx.CommandEvent) -> None:
        self._emit_battery_icon_preview()
        evt.Skip()

    def _emit_battery_icon_preview(self) -> None:
        """Offer the current battery-icon pick to the live tray preview, if any."""
        if self._preview.battery_icon is not None:
            self._preview.battery_icon(self._battery_icon.GetValue())

    def _on_dynamic_color(self, evt: wx.CommandEvent) -> None:
        self._update_bands()
        evt.Skip()

    def _on_reset(self, _evt: wx.CommandEvent) -> None:
        defaults = Config()
        self._poll.SetValue(defaults.poll_rate)
        self._all_fonts.SetValue(False)
        self._populate_faces()
        self._select_font(defaults.font)
        self._emit_font_preview()
        self._size.SetValue(defaults.text_size)
        self._size_label.SetLabel(f"{defaults.text_size}%")
        self._emit_size_preview()
        self._text_outline.SetValue(defaults.text_outline)
        self._emit_outline_preview()
        self._color.SetColour(wx.Colour(*defaults.foreground_color))
        self._mid_color.SetColour(wx.Colour(*defaults.mid_color))
        self._low_color.SetColour(wx.Colour(*defaults.low_color))
        self._dynamic_color.SetValue(defaults.dynamic_color)
        self._update_bands()
        self._battery_icon.SetValue(defaults.battery_icon)
        self._emit_battery_icon_preview()
        self._tray_icons.SetValue(defaults.tray_icons)
        self._low_battery_notify.SetValue(defaults.low_battery_notify)
        self._notify_high.SetValue(defaults.notify_level_high)
        self._notify_low.SetValue(defaults.notify_level_low)
        self._notify_repeat.SetValue(defaults.notify_repeat_minutes)
        self._language.SetSelection(list(LANGUAGES).index(defaults.language))
        self._debug.SetValue(defaults.debug)
        # Autostart is left alone: it is a system integration, not a
        # look-and-feel default, and it never lived in Config.

    def _on_ok(self, evt: wx.CommandEvent) -> None:
        face = self._font.GetStringSelection()
        if face:
            try:
                ImageFont.truetype(self._face_to_path[face], 16)
            except OSError:
                wx.MessageBox(
                    tr("Could not load font {face!r}.\nPlease choose another.", face=face),
                    tr("Invalid font"),
                    wx.OK | wx.ICON_ERROR,
                    self,
                )
                return  # keep the dialog open so the user can fix it
        if not self._apply_autostart():
            return
        evt.Skip()  # let the standard OK handler close the dialog

    def _apply_autostart(self) -> bool:
        """Write a changed autostart choice; ``False`` keeps the dialog open."""
        wanted = self._autostart.GetValue()
        if wanted == self._autostart_was:
            return True  # unchanged: leave the entry exactly as Windows has it
        try:
            autostart.set_enabled(self._app_name, wanted)
        except OSError as exc:
            wx.MessageBox(
                tr("Could not change the autostart setting:\n{exc}", exc=exc),
                tr("Start with Windows"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return False
        return True

    # --- result -------------------------------------------------------------

    def apply_to(self, config: Config) -> None:
        config.poll_rate = self._poll.GetValue()
        face = self._font.GetStringSelection()
        if face:
            config.font = self._face_to_path[face]
        config.text_size = self._size.GetValue()
        config.text_outline = self._text_outline.GetValue()
        config.foreground_color = _rgb(self._color)
        config.mid_color = _rgb(self._mid_color)
        config.low_color = _rgb(self._low_color)
        config.dynamic_color = self._dynamic_color.GetValue()
        config.battery_icon = self._battery_icon.GetValue()
        config.tray_icons = self._tray_icons.GetValue()
        config.low_battery_notify = self._low_battery_notify.GetValue()
        high, low = self._notify_high.GetValue(), self._notify_low.GetValue()
        config.notify_level_high, config.notify_level_low = max(high, low), min(high, low)
        config.notify_repeat_minutes = self._notify_repeat.GetValue()
        config.language = list(LANGUAGES)[self._language.GetSelection()]
        config.debug = self._debug.GetValue()
