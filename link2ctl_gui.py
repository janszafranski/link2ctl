#!/usr/bin/env python3
"""GTK4/libadwaita window for link2ctl.

The preview is `v4l2src ! jpegdec ! videoconvert ! appsink`, pushed into a
Gtk.Picture as a Gdk.MemoryTexture. Everything else drives the same Camera
object the CLI uses, so the window and `link2ctl set` cannot disagree about
what a control does.
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from link2ctl import (                                      # noqa: E402
    APP_ID, ARCSEC_PER_DEGREE, CTRL_BOOLEAN, CTRL_INTEGER, CTRL_MENU,
    GESTURE_BITS, GESTURE_MASK, V4L2_CTRL_FLAG_INACTIVE, Camera, XUError,
    apply_preset, capture_preset, load_presets, save_presets,
)

# Shown on the Position page, in this order; everything else lands on Image.
PTZ_SLUGS = ("pan_absolute", "tilt_absolute", "zoom_absolute")

PREVIEW_PIPELINE = (
    "v4l2src device={device} ! image/jpeg,width=1280,height=720 ! jpegdec ! "
    "videoconvert ! video/x-raw,format=RGB ! "
    "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
)


class Preview:
    """GStreamer capture feeding a Gtk.Picture."""

    def __init__(self, picture: Gtk.Picture, on_error):
        self.picture = picture
        self.on_error = on_error
        self.pipeline = None

    @property
    def running(self) -> bool:
        return self.pipeline is not None

    def start(self, device: str):
        self.stop()
        try:
            self.pipeline = Gst.parse_launch(PREVIEW_PIPELINE.format(device=device))
        except GLib.Error as exc:
            self.on_error(f"could not build the preview pipeline: {exc.message}")
            return
        self.pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            self.on_error("could not open the camera for preview; "
                          "another application may be using it")

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.picture.set_paintable(None)

    def _on_bus_error(self, _bus, message):
        err, _debug = message.parse_error()
        self.stop()
        self.on_error(err.message)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = GLib.Bytes.new(info.data)
        finally:
            buf.unmap(info)
        # videoconvert pads rows to a 4-byte boundary; derive the real stride
        # from the buffer rather than assuming width * 3.
        stride = data.get_size() // height
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8,
                                        data, stride)
        GLib.idle_add(self.picture.set_paintable, texture,
                      priority=GLib.PRIORITY_DEFAULT_IDLE)
        return Gst.FlowReturn.OK


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, cam: Camera):
        super().__init__(application=app, title="Insta360 Link 2",
                         default_width=1080, default_height=720)
        self.cam = cam
        self.rows: dict[str, Gtk.Widget] = {}
        self._updating = False

        self.toasts = Adw.ToastOverlay()
        self.stack = stack = Adw.ViewStack()
        stack.add_titled_with_icon(self._position_page(), "position", "Position",
                                   "camera-photo-symbolic")
        stack.add_titled_with_icon(self._image_page(), "image", "Image",
                                   "applications-graphics-symbolic")
        stack.add_titled_with_icon(self._camera_page(), "camera", "Camera",
                                   "preferences-system-symbolic")

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=stack,
                                                 policy=Adw.ViewSwitcherPolicy.WIDE))
        self.preview_button = Gtk.ToggleButton(icon_name="media-playback-start-symbolic",
                                               tooltip_text="Live preview", active=True)
        self.preview_button.connect("toggled", self._on_preview_toggled)
        header.pack_start(self.preview_button)
        reset = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-read camera")
        reset.connect("clicked", lambda *_: self.refresh())
        header.pack_end(reset)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # Tiled into a narrow column the preview and the controls cannot sit
        # side by side without one of them being clipped, so stack them.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 920px"))
        narrow.add_setter(self.position_box, "orientation", Gtk.Orientation.VERTICAL)
        # Stacked, a greedy preview would push the arrow pad off the bottom.
        narrow.add_setter(self.preview_frame, "vexpand", False)
        self.add_breakpoint(narrow)

        self.preview = Preview(self.picture, self._error)
        self.preview.start(cam.path)
        self.refresh()
        GLib.timeout_add_seconds(2, self._tick)

    # -- pages -------------------------------------------------------------- #

    def _position_page(self) -> Gtk.Widget:
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN,
                                   hexpand=True, vexpand=True)
        # Without a floor the controls column wins the fight for width and the
        # preview collapses to a sliver.
        frame = self.preview_frame = Gtk.Frame(child=self.picture, width_request=480,
                                               height_request=300)
        frame.add_css_class("view")

        pad = Gtk.Grid(row_spacing=6, column_spacing=6, halign=Gtk.Align.CENTER)
        arrows = (("up", "go-up-symbolic", 1, 0), ("left", "go-previous-symbolic", 0, 1),
                  ("right", "go-next-symbolic", 2, 1), ("down", "go-down-symbolic", 1, 2))
        for direction, icon, col, row in arrows:
            button = Gtk.Button(icon_name=icon, width_request=52, height_request=52)
            button.connect("clicked", self._on_nudge, direction)
            pad.attach(button, col, row, 1, 1)
        home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text="Centre",
                          width_request=52, height_request=52)
        home.connect("clicked", self._on_home)
        pad.attach(home, 1, 1, 1, 1)

        self.step = Gtk.SpinButton.new_with_range(1, 45, 1)
        self.step.set_value(5)
        step_row = Adw.ActionRow(title="Step size", subtitle="degrees per nudge")
        step_row.add_suffix(self.step)

        group = Adw.PreferencesGroup(title="Position")
        group.add(step_row)
        for slug in PTZ_SLUGS:
            row = self._row_for(slug)
            if row is not None:
                group.add(row)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       width_request=380)
        side.append(pad)
        side.append(group)
        side.append(self._presets_group())

        self.position_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.position_box.append(frame)
        self.position_box.append(side)
        # One scroller for the whole page. Putting the controls column in its
        # own nested scroller collapses it to nothing once the page stacks.
        return Gtk.ScrolledWindow(child=self.position_box,
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _presets_group(self) -> Adw.PreferencesGroup:
        self.presets_group = Adw.PreferencesGroup(title="Presets")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Save current position",
                         valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", self._on_preset_add)
        self.presets_group.set_header_suffix(add)
        self._reload_presets()
        return self.presets_group

    def _reload_presets(self):
        for row in getattr(self, "_preset_rows", []):
            self.presets_group.remove(row)
        self._preset_rows = []
        presets = load_presets()
        if not presets:
            row = Adw.ActionRow(title="No presets yet",
                                subtitle="Aim the camera, then press +")
            row.set_sensitive(False)
            self.presets_group.add(row)
            self._preset_rows.append(row)
            return
        for name, snapshot in sorted(presets.items()):
            pan = snapshot.get("pan_absolute", 0) / ARCSEC_PER_DEGREE
            tilt = snapshot.get("tilt_absolute", 0) / ARCSEC_PER_DEGREE
            zoom = snapshot.get("zoom_absolute", 100) / 100
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(name),
                subtitle=f"pan {pan:+.0f}°, tilt {tilt:+.0f}°, {zoom:.1f}×",
                activatable=True)
            row.connect("activated", self._on_preset_recall, name)
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.connect("clicked", self._on_preset_delete, name)
            row.add_suffix(delete)
            self.presets_group.add(row)
            self._preset_rows.append(row)

    def _image_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        exposure = Adw.PreferencesGroup(title="Exposure and colour")
        focus = Adw.PreferencesGroup(title="Focus")
        for slug in self.cam.controls:
            if slug in PTZ_SLUGS:
                continue
            row = self._row_for(slug)
            if row is not None:
                (focus if "focus" in slug else exposure).add(row)
        page.add(exposure)
        page.add(focus)

        reset = Gtk.Button(label="Restore defaults", halign=Gtk.Align.CENTER,
                           margin_top=12)
        reset.connect("clicked", self._on_restore_defaults)
        group = Adw.PreferencesGroup()
        group.add(reset)
        page.add(group)
        return page

    def _camera_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        self.gesture_group = Adw.PreferencesGroup(
            title="Gesture control",
            description="This firmware exposes three gesture groups. Which "
                        "physical gesture belongs to which group is not "
                        "documented — toggle one and try it.")
        self.gesture_switches = {}
        for bit in GESTURE_BITS:
            row = Adw.SwitchRow(title=f"Gesture group {bit}")
            row.connect("notify::active", self._on_gesture_toggled)
            self.gesture_group.add(row)
            self.gesture_switches[bit] = row
        page.add(self.gesture_group)

        audio = Adw.PreferencesGroup(title="Microphone")
        self.denoise_row = Adw.SwitchRow(
            title="Noise cancellation",
            subtitle="Applies to the camera's built-in microphone")
        self.denoise_row.connect("notify::active", self._on_denoise_toggled)
        audio.add(self.denoise_row)
        page.add(audio)

        self.info_group = Adw.PreferencesGroup(title="Camera")
        self.info_rows = {}
        for key, title in (("mode", "AI framing mode"), ("iso", "ISO"),
                           ("shutter", "Shutter"), ("serial", "Serial number"),
                           ("device", "Video device")):
            row = Adw.ActionRow(title=title)
            label = Gtk.Label(label="—", valign=Gtk.Align.CENTER)
            label.add_css_class("dim-label")
            row.add_suffix(label)
            self.info_rows[key] = label
            self.info_group.add(row)
        page.add(self.info_group)

        note = Adw.PreferencesGroup(
            title="Not available from Linux",
            description="Background blur, background replacement, green screen "
                        "and beauty filters are not camera features — the "
                        "Windows app renders them on the PC and feeds the "
                        "result to a virtual camera. The AI framing mode is set "
                        "on the camera itself and is reported above, but the "
                        "command that changes it is not yet understood; see "
                        "PROTOCOL.md.")
        page.add(note)
        return page

    # -- generic control rows ----------------------------------------------- #

    def _row_for(self, slug: str):
        ctrl = self.cam.controls.get(slug)
        if ctrl is None or not ctrl.writable:
            return None
        title = ctrl.name
        if ctrl.type == CTRL_BOOLEAN:
            row = Adw.SwitchRow(title=title)
            row.connect("notify::active", self._on_switch, slug)
        elif ctrl.type == CTRL_MENU:
            model = Gtk.StringList()
            for label in ctrl.menu.values():
                model.append(label)
            row = Adw.ComboRow(title=title, model=model)
            row.connect("notify::selected", self._on_combo, slug)
        elif ctrl.type == CTRL_INTEGER:
            if ctrl.is_angle:
                lo, hi, step = (ctrl.min / ARCSEC_PER_DEGREE,
                                ctrl.max / ARCSEC_PER_DEGREE, 1)
                subtitle = "degrees"
            elif slug == "zoom_absolute":
                lo, hi, step, subtitle = ctrl.min / 100, ctrl.max / 100, 0.1, "×"
            else:
                lo, hi, step, subtitle = ctrl.min, ctrl.max, ctrl.step, None
            row = Adw.SpinRow.new_with_range(lo, hi, step)
            row.set_title(title)
            if subtitle:
                row.set_subtitle(subtitle)
            if slug == "zoom_absolute":
                row.set_digits(1)
            row.connect("notify::value", self._on_spin, slug)
        else:
            return None
        self.rows[slug] = row
        return row

    # -- reading the camera back into the widgets --------------------------- #

    def refresh(self):
        self._updating = True
        try:
            for slug, row in self.rows.items():
                ctrl = self.cam.controls[slug]
                value = self.cam.try_get(slug)
                if value is None:
                    continue
                self.cam.refresh_control(slug)
                row.set_sensitive(not ctrl.flags & V4L2_CTRL_FLAG_INACTIVE)
                if isinstance(row, Adw.SwitchRow):
                    row.set_active(bool(value))
                elif isinstance(row, Adw.ComboRow):
                    keys = list(ctrl.menu)
                    if value in keys:
                        row.set_selected(keys.index(value))
                elif isinstance(row, Adw.SpinRow):
                    row.set_value(self._to_display(slug, value))
            try:
                mask = self.cam.get_gestures()
                for bit, row in self.gesture_switches.items():
                    row.set_active(bool(mask >> bit & 1))
                self.denoise_row.set_active(self.cam.get_denoise())
                self.gesture_group.set_sensitive(True)
            except XUError:
                self.gesture_group.set_sensitive(False)
        finally:
            self._updating = False
        self._tick()

    def _tick(self) -> bool:
        mode = self.cam.video_mode[1]
        iso, shutter = self.cam.iso, self.cam.shutter
        self.info_rows["mode"].set_label(mode)
        self.info_rows["iso"].set_label(str(iso) if iso else "—")
        self.info_rows["shutter"].set_label(f"1/{shutter}s" if shutter else "—")
        self.info_rows["serial"].set_label(self.cam.serial)
        self.info_rows["device"].set_label(self.cam.path)
        return GLib.SOURCE_CONTINUE

    def _to_display(self, slug: str, value: int) -> float:
        if self.cam.controls[slug].is_angle:
            return value / ARCSEC_PER_DEGREE
        if slug == "zoom_absolute":
            return value / 100
        return value

    def _from_display(self, slug: str, value: float) -> int:
        if self.cam.controls[slug].is_angle:
            return round(value * ARCSEC_PER_DEGREE)
        if slug == "zoom_absolute":
            return round(value * 100)
        return round(value)

    # -- handlers ----------------------------------------------------------- #

    def _write(self, slug: str, value: int):
        try:
            self.cam.set(slug, value)
        except (OSError, SystemExit) as exc:
            self._error(f"{slug}: {exc}")

    def _on_spin(self, row, _param, slug):
        if not self._updating:
            self._write(slug, self._from_display(slug, row.get_value()))

    def _on_switch(self, row, _param, slug):
        if self._updating:
            return
        self._write(slug, 1 if row.get_active() else 0)
        # Turning auto focus or auto white balance off un-greys its manual
        # partner, so re-read everything rather than just this row.
        GLib.timeout_add(150, lambda: (self.refresh(), GLib.SOURCE_REMOVE)[1])

    def _on_combo(self, row, _param, slug):
        if self._updating:
            return
        keys = list(self.cam.controls[slug].menu)
        index = row.get_selected()
        if 0 <= index < len(keys):
            self._write(slug, keys[index])

    def _on_nudge(self, _button, direction):
        slug = "pan_absolute" if direction in ("left", "right") else "tilt_absolute"
        if slug not in self.cam.controls:
            return
        sign = 1 if direction in ("right", "up") else -1
        delta = sign * round(self.step.get_value() * ARCSEC_PER_DEGREE)
        self._write(slug, self.cam.get(slug) + delta)
        self.refresh()

    def _on_home(self, _button):
        self.cam.home()
        self.refresh()

    def _on_preset_add(self, _button):
        dialog = Adw.AlertDialog(heading="Save preset",
                                 body="Remembers the current position, zoom and "
                                      "image settings.")
        entry = Gtk.Entry(placeholder_text="Name", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def done(_dialog, response):
            name = entry.get_text().strip()
            if response == "save" and name:
                presets = load_presets()
                presets[name] = capture_preset(self.cam)
                save_presets(presets)
                self._reload_presets()
                self._toast(f"Saved “{name}”")

        dialog.connect("response", done)
        dialog.present(self)

    def _on_preset_recall(self, _row, name):
        apply_preset(self.cam, load_presets().get(name, {}))
        GLib.timeout_add(600, lambda: (self.refresh(), GLib.SOURCE_REMOVE)[1])
        self._toast(f"Recalled “{name}”")

    def _on_preset_delete(self, _button, name):
        presets = load_presets()
        if presets.pop(name, None) is not None:
            save_presets(presets)
            self._reload_presets()
            self._toast(f"Deleted “{name}”")

    def _on_gesture_toggled(self, *_args):
        if self._updating:
            return
        mask = 0
        for bit, row in self.gesture_switches.items():
            if row.get_active():
                mask |= 1 << bit
        try:
            actual = self.cam.set_gestures(mask & GESTURE_MASK)
        except XUError as exc:
            self._error(str(exc))
            return
        if actual != mask:
            self._updating = True
            for bit, row in self.gesture_switches.items():
                row.set_active(bool(actual >> bit & 1))
            self._updating = False

    def _on_denoise_toggled(self, *_args):
        if self._updating:
            return
        try:
            self.cam.set_denoise(self.denoise_row.get_active())
        except XUError as exc:
            self._error(str(exc))

    def _on_restore_defaults(self, _button):
        for slug, ctrl in self.cam.controls.items():
            if slug not in PTZ_SLUGS and ctrl.writable:
                try:
                    self.cam.set(slug, ctrl.default)
                except OSError:
                    pass
        self.refresh()
        self._toast("Image settings restored")

    def _on_preview_toggled(self, button):
        if button.get_active():
            self.preview.start(self.cam.path)
        else:
            self.preview.stop()

    def _toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def _error(self, text: str):
        self.preview_button.set_active(self.preview.running)
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))


class Application(Adw.Application):
    def __init__(self, device: str | None):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.device = device
        self.cam = None

    def do_activate(self):
        if self.cam is None:
            try:
                self.cam = Camera.open_first(self.device)
            except SystemExit as exc:
                dialog = Adw.AlertDialog(heading="No camera found", body=str(exc))
                dialog.add_response("quit", "Quit")
                dialog.connect("response", lambda *_: self.quit())
                window = Adw.ApplicationWindow(application=self, title="link2ctl")
                window.set_content(Adw.ToolbarView(content=Adw.StatusPage(
                    title="No Insta360 Link 2 found", icon_name="camera-disabled-symbolic")))
                window.present()
                dialog.present(window)
                return
        Window(self, self.cam).present()

    def do_shutdown(self):
        if self.cam is not None:
            self.cam.close()
        Adw.Application.do_shutdown(self)


def run_gui(device: str | None = None) -> int:
    Gst.init(None)
    return Application(device).run([])


if __name__ == "__main__":
    sys.exit(run_gui(sys.argv[1] if len(sys.argv) > 1 else None))
