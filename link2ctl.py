#!/usr/bin/env python3
"""link2ctl — a native Linux controller for the Insta360 Link 2.

The Link 2 ships with Windows-only configuration software. Everything that
software does to the camera itself goes over two channels, and both are
reachable from Linux without a driver, a daemon or root:

  * standard UVC controls (pan, tilt, zoom, focus, white balance, image) via
    plain V4L2 ioctls on /dev/videoN;
  * three vendor UVC Extension Units via UVCIOC_CTRL_QUERY on the same fd.

The extension units are located by GUID from the USB descriptors rather than by
hard-coded unit number, and every selector length comes from GET_LEN on the
device, so a firmware update that renumbers things does not silently corrupt
writes. The selector names come from the vendor's own protobuf `ControlSelector`
enum, recovered from the Windows build; see PROTOCOL.md.

  link2ctl                      open the window
  link2ctl info                 device identity, XU map, live telemetry
  link2ctl list                 every V4L2 control with its range
  link2ctl get [NAME ...]       read controls
  link2ctl set NAME=VALUE ...   write controls
  link2ctl ptz [--pan D] [--tilt D] [--zoom X] [--home] [--nudge DIR]
  link2ctl preset list|save NAME|recall NAME|rm NAME
  link2ctl gestures [on|off|MASK]
  link2ctl denoise [on|off]
  link2ctl monitor              live ISO/shutter/mode readout
  link2ctl xu get UNIT SEL | xu set UNIT SEL HEX

Angles are given in degrees; V4L2 carries them as arcseconds internally.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import glob
import json
import os
import struct
import sys
import time
import uuid

APP_ID = "dev.jan.Link2Ctl"
VENDOR_ID, PRODUCT_ID = 0x2E1A, 0x4C04

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "link2ctl")
PRESET_FILE = os.path.join(CONFIG_DIR, "presets.json")

ARCSEC_PER_DEGREE = 3600


# --------------------------------------------------------------------------- #
# V4L2
# --------------------------------------------------------------------------- #

VIDIOC_QUERYCAP = 0x80685600
VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_QUERYMENU = 0xC02C5625
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C

V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000
V4L2_CTRL_FLAG_DISABLED = 0x0001
V4L2_CTRL_FLAG_READ_ONLY = 0x0004
V4L2_CTRL_FLAG_INACTIVE = 0x0010

CTRL_INTEGER, CTRL_BOOLEAN, CTRL_MENU, CTRL_BUTTON, CTRL_CTRL_CLASS = 1, 2, 3, 4, 6

# Controls the UI shows as angles rather than raw arcseconds.
ANGLE_CONTROLS = ("pan_absolute", "tilt_absolute")


class Control:
    """One V4L2 control, as reported by VIDIOC_QUERYCTRL."""

    def __init__(self, cid, ctype, name, minimum, maximum, step, default, flags):
        self.id = cid
        self.type = ctype
        self.name = name
        self.min = minimum
        self.max = maximum
        self.step = step or 1
        self.default = default
        self.flags = flags
        self.menu: dict[int, str] = {}

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "_").replace(",", "")

    @property
    def writable(self) -> bool:
        return not self.flags & (V4L2_CTRL_FLAG_READ_ONLY | V4L2_CTRL_FLAG_DISABLED)

    @property
    def is_angle(self) -> bool:
        return self.slug in ANGLE_CONTROLS

    def clamp(self, value: int) -> int:
        value = max(self.min, min(self.max, int(value)))
        # The gimbal only accepts multiples of its step; round rather than
        # truncate so "set tilt to the maximum" does not land one step short.
        if self.step > 1:
            value = self.min + round((value - self.min) / self.step) * self.step
            value = max(self.min, min(self.max, value))
        return value

    def format(self, value: int) -> str:
        if self.type == CTRL_BOOLEAN:
            return "on" if value else "off"
        if self.type == CTRL_MENU:
            return f"{value} ({self.menu.get(value, '?')})"
        if self.is_angle:
            return f"{value / ARCSEC_PER_DEGREE:+.1f}deg"
        return str(value)


def _ioctl(fd, request, payload: bytes) -> bytes:
    buf = ctypes.create_string_buffer(payload, len(payload))
    fcntl.ioctl(fd, request, buf)
    return buf.raw


# --------------------------------------------------------------------------- #
# UVC extension units
# --------------------------------------------------------------------------- #

# struct uvc_xu_control_query { u8 unit, selector, query; u16 size; u8 *data; }
UVCIOC_CTRL_QUERY = 0xC0107521
XU_SET_CUR, XU_GET_CUR, XU_GET_LEN, XU_GET_INFO = 0x01, 0x81, 0x85, 0x86
XU_INFO_GET, XU_INFO_SET = 0x01, 0x02

XU_MAIN_GUID = uuid.UUID("faf1672d-b71b-4793-8c91-7b1c9b7f95f8")
XU_TRACK_GUID = uuid.UUID("e307e649-4618-a3ff-82fc-2d8b5f216773")
XU_FRAME_GUID = uuid.UUID("a8bd5df2-1a98-474e-8dd0-d92672d194fa")

# Selector names come straight from the vendor's ControlSelector protobuf enum.
# The numbers are the enum values, which are also the UVC control selectors --
# cross-checked against this hardware (serial at 12, USB-mode at 17, noise
# cancel at 7, absolute pan/tilt at 26).
XU_MAIN_SELECTORS = {
    1: "EXEC_SCRIPT", 2: "VIDEO_MODE", 3: "DEVICE_INFO", 4: "PTZ_CMD",
    5: "GESTURE_STATUS", 6: "GESTURE_BIND", 7: "NOISE_CANCEL",
    8: "FIRMWARE_UPGRADE/BLEND_DRAW", 9: "EXPOSURE_VALUE", 10: "TAKE_PICTURE",
    11: "DEVICE_STATUS", 12: "DEVICE_SN", 13: "DEVICE_LICENSE",
    14: "DEVICE_PARAM", 15: "AF_MODE/DOWNLOAD_FILE",
    16: "EXPOSURE_CURVE/UPLOAD_FILE", 17: "USB_MODE_SWITCH", 18: "TRACK_SPEED",
    19: "LAYOUT_STYLE", 20: "HEAD_LIST", 21: "TRACK_TARGET",
    22: "PANTILT_RELATIVE", 23: "MOBVOI_PUBKEY", 24: "BIAS", 25: "ISO",
    26: "PANTILT_ABSOLUTE", 27: "FUNC_ENABLE", 28: "VIDEO_RES",
    29: "EXPOSURE_TIME_ABSOLUTE", 30: "AE_MODE",
}

SEL_VIDEO_MODE = 2
SEL_GESTURE_STATUS = 5
SEL_NOISE_CANCEL = 7
SEL_DEVICE_STATUS = 11
SEL_DEVICE_SN = 12
SEL_USB_MODE_SWITCH = 17
SEL_ISO = 25
SEL_EXPOSURE_TIME = 29
SEL_AE_MODE = 30

# Only 1 and 2 are worth exposing. The firmware stores 0, 4 and 8 happily but
# they behave as undocumented auto variants that settle on 1/33s, which bands
# under 50 Hz mains -- see PROTOCOL.md.
AE_MANUAL = 1
AE_AUTO = 2
AE_MODES = {0: "auto (variant 0)", AE_MANUAL: "manual", AE_AUTO: "auto",
            4: "auto (variant 4)", 8: "auto (variant 8)"}

# Nothing on this path is range-checked by the firmware: ISO 25600 and shutter
# 1/65535 are both accepted and read back verbatim. These are the limits past
# which measured frames stop changing, so we clamp in software instead.
ISO_MIN, ISO_MAX = 100, 6400
SHUTTER_MIN, SHUTTER_MAX = 25, 8000

# Minimum spacing between extension-unit writes, and how many times to retry a
# write the firmware silently ignored; see Camera.xu_write and _write_settled.
XU_WRITE_GAP = 0.1
XU_WRITE_TRIES = 4

# Writing either of these takes the camera off the USB bus, so neither is ever
# written as a side effect of anything. Keyed by unit GUID, not unit number --
# the numbers are firmware-assigned and not contractual.
#
# Main sel 17: writing 2 or 3 drops the camera out of UVC mode; it stops being
# a webcam and reappears as mass storage or a vendor-class device.
#
# Tracking sel 17: measured on this hardware. Writing it made the device vanish
# from the bus entirely -- gone from lsusb and /dev/video*, not merely remoded.
# The SET ioctl returned ETIMEDOUT and the camera re-enumerated by itself about
# five seconds later, so it is a firmware reboot rather than a mode change.
DANGEROUS = {
    (XU_MAIN_GUID, SEL_USB_MODE_SWITCH):
        "switches the camera out of webcam mode; it will vanish from "
        "/dev/video* until it is re-enumerated",
    (XU_TRACK_GUID, 17):
        "reboots the camera; it drops off the USB bus entirely and comes "
        "back a few seconds later",
}

# sel 2 is a 56-byte live view-state record. Byte 54 is the current video mode.
# It is read-only: writing the record back with byte 54 changed is ignored, and
# so are six other encodings of the same request, with and without a live
# preview stream. The real setter takes (mode, hostpitch, ptz_check_result) and
# has not been located yet -- see PROTOCOL.md.
VIDEO_MODE_OFFSET = 54
VIDEO_MODES = {
    0: "Normal", 1: "Auto composition", 2: "Tracking", 4: "Whiteboard",
    5: "Overhead", 6: "DeskView", 7: "Auto framing", 8: "Smart whiteboard",
    9: "Regional track",
}

# Of the five bits sel 5 defines, this firmware only honours bits 1-3; it
# silently clears the rest (0x1f reads back as 0x0e).
GESTURE_MASK = 0x0E
GESTURE_BITS = (1, 2, 3)


class XUError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Device
# --------------------------------------------------------------------------- #

class Camera:
    """A Link 2 reachable through one /dev/videoN node."""

    def __init__(self, path: str, usb_dir: str | None = None):
        self.path = path
        self.usb_dir = usb_dir
        self.fd = os.open(path, os.O_RDWR)
        self.controls: dict[str, Control] = {}
        self.units: dict[uuid.UUID, int] = {}
        self.unit_controls: dict[int, int] = {}
        self._len_cache: dict[tuple[int, int], int] = {}
        self._last_write = 0.0
        self._load_controls()
        if usb_dir:
            self._load_units(usb_dir)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- discovery ---------------------------------------------------------- #

    @staticmethod
    def find_all() -> list[tuple[str, str]]:
        """Return (video node, usb sysfs dir) for every Link 2 present."""
        found = []
        for usb_dir in sorted(glob.glob("/sys/bus/usb/devices/*")):
            try:
                vid = int(open(os.path.join(usb_dir, "idVendor")).read(), 16)
                pid = int(open(os.path.join(usb_dir, "idProduct")).read(), 16)
            except (OSError, ValueError):
                continue
            if (vid, pid) != (VENDOR_ID, PRODUCT_ID):
                continue
            for node in sorted(glob.glob(os.path.join(usb_dir, "*", "video4linux", "video*"))):
                found.append(("/dev/" + os.path.basename(node), usb_dir))
        return found

    @classmethod
    def open_first(cls, path: str | None = None) -> "Camera":
        if path:
            usb = next((u for p, u in cls.find_all() if p == path), None)
            return cls(path, usb)
        devices = cls.find_all()
        if not devices:
            raise SystemExit(
                "No Insta360 Link 2 found (looked for USB %04x:%04x).\n"
                "Is it plugged in? `lsusb | grep -i insta` to check."
                % (VENDOR_ID, PRODUCT_ID))
        # The first node of a UVC device is the one carrying the controls; the
        # second is the metadata node.
        return cls(*devices[0])

    def _load_units(self, usb_dir: str):
        """Locate the extension units by GUID from the raw USB descriptors."""
        try:
            raw = open(os.path.join(usb_dir, "descriptors"), "rb").read()
        except OSError:
            return
        i = 0
        while i + 2 < len(raw):
            length, dtype = raw[i], raw[i + 1]
            if length == 0:
                break
            # CS_INTERFACE / VC_EXTENSION_UNIT
            if dtype == 0x24 and raw[i + 2] == 0x06 and length >= 24:
                unit = raw[i + 3]
                guid = uuid.UUID(bytes_le=bytes(raw[i + 4:i + 20]))
                npins = raw[i + 21]
                csize = raw[i + 22 + npins]
                bitmap = raw[i + 23 + npins:i + 23 + npins + csize]
                highest = max((b * 8 + k + 1
                               for b, byte in enumerate(bitmap)
                               for k in range(8) if byte >> k & 1), default=0)
                self.units[guid] = unit
                self.unit_controls[unit] = highest
            i += length

    @property
    def main_unit(self) -> int | None:
        return self.units.get(XU_MAIN_GUID)

    # -- V4L2 --------------------------------------------------------------- #

    def _load_controls(self):
        cid = 0
        while True:
            payload = struct.pack("<II32siiiiI8x", cid | V4L2_CTRL_FLAG_NEXT_CTRL,
                                  0, b"", 0, 0, 0, 0, 0)
            try:
                out = _ioctl(self.fd, VIDIOC_QUERYCTRL, payload)
            except OSError as exc:
                if exc.errno in (errno.EINVAL, errno.ENOTTY):
                    break
                raise
            got, ctype, name, lo, hi, step, default, flags = struct.unpack(
                "<II32siiiiI8x", out)
            if got <= cid:              # no forward progress: stop
                break
            cid = got
            if ctype == CTRL_CTRL_CLASS or flags & V4L2_CTRL_FLAG_DISABLED:
                continue
            ctrl = Control(got, ctype, name.split(b"\0")[0].decode("utf-8", "replace"),
                           lo, hi, step, default, flags)
            if ctype == CTRL_MENU:
                ctrl.menu = self._menu(got, lo, hi)
            self.controls[ctrl.slug] = ctrl

    def _menu(self, cid: int, lo: int, hi: int) -> dict[int, str]:
        items = {}
        for index in range(lo, hi + 1):
            try:
                out = _ioctl(self.fd, VIDIOC_QUERYMENU,
                             struct.pack("<II32sI", cid, index, b"", 0))
            except OSError:
                continue
            name = struct.unpack("<II32sI", out)[2].split(b"\0")[0]
            items[index] = name.decode("utf-8", "replace")
        return items

    def get(self, slug: str) -> int:
        ctrl = self.control(slug)
        out = _ioctl(self.fd, VIDIOC_G_CTRL, struct.pack("<Ii", ctrl.id, 0))
        return struct.unpack("<Ii", out)[1]

    def set(self, slug: str, value: int) -> int:
        ctrl = self.control(slug)
        if not ctrl.writable:
            raise SystemExit(f"{slug} is read-only")
        value = ctrl.clamp(value)
        _ioctl(self.fd, VIDIOC_S_CTRL, struct.pack("<Ii", ctrl.id, value))
        return value

    def refresh_control(self, slug: str):
        """Re-query one control's flags.

        The INACTIVE flag is dynamic -- white_balance_temperature becomes
        inactive the moment auto white balance is switched on -- and it is only
        visible by asking the driver again.
        """
        ctrl = self.controls.get(slug)
        if ctrl is None:
            return
        try:
            out = _ioctl(self.fd, VIDIOC_QUERYCTRL,
                         struct.pack("<II32siiiiI8x", ctrl.id, 0, b"", 0, 0, 0, 0, 0))
        except OSError:
            return
        _, _, _, lo, hi, step, default, flags = struct.unpack("<II32siiiiI8x", out)
        ctrl.min, ctrl.max, ctrl.default, ctrl.flags = lo, hi, default, flags
        ctrl.step = step or 1

    def control(self, slug: str) -> Control:
        try:
            return self.controls[slug]
        except KeyError:
            raise SystemExit(
                f"unknown control {slug!r}; `link2ctl list` shows what this "
                f"camera exposes") from None

    def try_get(self, slug: str):
        """Read a control, or None if it is absent or currently inactive."""
        if slug not in self.controls:
            return None
        try:
            return self.get(slug)
        except OSError:
            return None

    # -- extension units ---------------------------------------------------- #

    def xu(self, unit: int, selector: int, request: int, size: int,
           data: bytes | None = None) -> bytes:
        buf = ctypes.create_string_buffer(data if data is not None else max(size, 1))
        query = struct.pack("@BBBHP", unit, selector, request, size,
                            ctypes.addressof(buf))
        try:
            fcntl.ioctl(self.fd, UVCIOC_CTRL_QUERY, query)
        except OSError as exc:
            raise XUError(f"XU{unit} sel{selector} "
                          f"{'SET' if request == XU_SET_CUR else 'GET'}: "
                          f"{exc.strerror}") from None
        return buf.raw[:size]

    def xu_len(self, unit: int, selector: int) -> int:
        key = (unit, selector)
        if key not in self._len_cache:
            self._len_cache[key] = struct.unpack(
                "<H", self.xu(unit, selector, XU_GET_LEN, 2))[0]
        return self._len_cache[key]

    def xu_info(self, unit: int, selector: int) -> int:
        return self.xu(unit, selector, XU_GET_INFO, 1)[0]

    def xu_read(self, unit: int, selector: int) -> bytes:
        return self.xu(unit, selector, XU_GET_CUR, self.xu_len(unit, selector))

    def unit_guid(self, unit: int) -> uuid.UUID | None:
        for guid, number in self.units.items():
            if number == unit:
                return guid
        return None

    def xu_write(self, unit: int, selector: int, data: bytes, force: bool = False):
        harm = DANGEROUS.get((self.unit_guid(unit), selector))
        if harm and not force:
            name = (XU_MAIN_SELECTORS.get(selector, "?")
                    if unit == self.main_unit else "?")
            raise SystemExit(
                f"refusing to write XU{unit} sel{selector} ({name}): this "
                f"{harm}. Pass --force if you really mean it.")
        size = self.xu_len(unit, selector)
        if len(data) != size:
            raise SystemExit(f"XU{unit} sel{selector} expects {size} bytes, "
                             f"got {len(data)}")
        # A write issued within ~50 ms of the previous one is silently dropped:
        # the ioctl succeeds, the firmware ignores it, and the readback still
        # shows the old value. Measured on this hardware -- 50 ms fails, 50 ms
        # plus margin always lands. Space writes out rather than lie about them.
        wait = XU_WRITE_GAP - (time.monotonic() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self.xu(unit, selector, XU_SET_CUR, size, data)
        self._last_write = time.monotonic()

    def _main(self, selector: int) -> tuple[int, int]:
        unit = self.main_unit
        if unit is None:
            raise SystemExit("main extension unit not found on this device")
        return unit, selector

    # -- the features the Windows app exposes ------------------------------- #

    @property
    def serial(self) -> str:
        try:
            raw = self.xu_read(*self._main(SEL_DEVICE_SN))
        except XUError:
            return "unknown"
        return raw.split(b"\0")[0].decode("ascii", "replace") or "unknown"

    @property
    def video_mode(self) -> tuple[int, str]:
        """The AI framing mode the camera reports it is currently in."""
        try:
            raw = self.xu_read(*self._main(SEL_VIDEO_MODE))
        except XUError:
            return -1, "unknown"
        if len(raw) <= VIDEO_MODE_OFFSET:
            return -1, "unknown"
        mode = raw[VIDEO_MODE_OFFSET]
        return mode, VIDEO_MODES.get(mode, f"mode {mode}")

    @property
    def iso(self) -> int | None:
        try:
            return struct.unpack("<H", self.xu_read(*self._main(SEL_ISO)))[0]
        except (XUError, struct.error):
            return None

    @property
    def shutter(self) -> int | None:
        try:
            return struct.unpack("<H", self.xu_read(*self._main(SEL_EXPOSURE_TIME)))[0]
        except (XUError, struct.error):
            return None

    @property
    def ae_mode(self) -> int | None:
        try:
            return self.xu_read(*self._main(SEL_AE_MODE))[0]
        except (XUError, IndexError):
            return None

    def _write_settled(self, selector: int, data: bytes, read) -> int:
        """Write an exposure selector and confirm the firmware took it.

        Spacing writes out is necessary but not sufficient: while the camera is
        streaming it still drops the occasional write, with no error on the
        ioctl. So write, read back, and retry. The firmware rounds some shutter
        values down by one, hence `abs(... ) <= 1` rather than equality.
        """
        want = struct.unpack("<H", data)[0]
        got = None
        for _ in range(XU_WRITE_TRIES):
            self.xu_write(*self._main(selector), data)
            time.sleep(XU_WRITE_GAP)        # the readback lags the write too
            got = read()
            if got is not None and abs(got - want) <= 1:
                break
        return got

    def set_ae_mode(self, mode: int) -> int | None:
        for _ in range(XU_WRITE_TRIES):
            if self.ae_mode == mode:
                return mode
            self.xu_write(*self._main(SEL_AE_MODE), bytes([mode]))
            time.sleep(XU_WRITE_GAP)
        return self.ae_mode

    def set_iso(self, iso: int) -> int:
        """Pin the sensor gain. Implies manual exposure; returns the readback."""
        self.set_ae_mode(AE_MANUAL)
        return self._write_settled(
            SEL_ISO, struct.pack("<H", max(ISO_MIN, min(ISO_MAX, iso))),
            lambda: self.iso)

    def set_shutter(self, denominator: int) -> int:
        """Pin the exposure time to 1/denominator s. Implies manual exposure.

        The firmware rounds some values down by one (1/30 lands on 1/29), so
        the readback is what it actually took, not what we asked for.
        """
        self.set_ae_mode(AE_MANUAL)
        return self._write_settled(
            SEL_EXPOSURE_TIME,
            struct.pack("<H", max(SHUTTER_MIN, min(SHUTTER_MAX, denominator))),
            lambda: self.shutter)

    @property
    def device_status(self) -> bytes | None:
        try:
            return self.xu_read(*self._main(SEL_DEVICE_STATUS))
        except XUError:
            return None

    def get_gestures(self) -> int:
        return self.xu_read(*self._main(SEL_GESTURE_STATUS))[0]

    def set_gestures(self, mask: int) -> int:
        """Set the gesture-group bitmask; returns what the firmware kept."""
        unit, sel = self._main(SEL_GESTURE_STATUS)
        self.xu_write(unit, sel, bytes([mask & GESTURE_MASK]))
        return self.get_gestures()

    def get_denoise(self) -> bool:
        return bool(self.xu_read(*self._main(SEL_NOISE_CANCEL))[0])

    def set_denoise(self, enabled: bool) -> bool:
        unit, sel = self._main(SEL_NOISE_CANCEL)
        self.xu_write(unit, sel, bytes([1 if enabled else 0]))
        return self.get_denoise()

    # -- convenience -------------------------------------------------------- #

    def ptz(self) -> dict[str, int | None]:
        return {"pan": self.try_get("pan_absolute"),
                "tilt": self.try_get("tilt_absolute"),
                "zoom": self.try_get("zoom_absolute")}

    def home(self):
        for slug in ("pan_absolute", "tilt_absolute"):
            if slug in self.controls:
                self.set(slug, self.controls[slug].default)
        if "zoom_absolute" in self.controls:
            self.set("zoom_absolute", self.controls["zoom_absolute"].min)


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

PRESET_CONTROLS = ("pan_absolute", "tilt_absolute", "zoom_absolute",
                   "focus_automatic_continuous", "focus_absolute",
                   "brightness", "contrast", "saturation", "sharpness",
                   "white_balance_automatic", "white_balance_temperature")


def load_presets() -> dict:
    try:
        with open(PRESET_FILE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_presets(presets: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = PRESET_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(presets, handle, indent=2, sort_keys=True)
    os.replace(tmp, PRESET_FILE)


def capture_preset(cam: Camera) -> dict:
    snapshot = {}
    for slug in PRESET_CONTROLS:
        value = cam.try_get(slug)
        if value is not None:
            snapshot[slug] = value
    return snapshot


def apply_preset(cam: Camera, snapshot: dict):
    # Auto-mode switches first: a manual focus or white-balance value is
    # rejected (flagged inactive) while its auto counterpart is still on.
    order = sorted(snapshot, key=lambda s: 0 if "automatic" in s else 1)
    for slug in order:
        if slug in cam.controls and cam.controls[slug].writable:
            try:
                cam.set(slug, snapshot[slug])
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _deg(value: int | None) -> str:
    return "—" if value is None else f"{value / ARCSEC_PER_DEGREE:+.1f}deg"


def _zoom(value: int | None) -> str:
    return "—" if value is None else f"{value / 100:.2f}x"


def cmd_info(cam: Camera, args) -> int:
    mode_id, mode_name = cam.video_mode
    print(f"device         {cam.path}")
    print(f"serial         {cam.serial}")
    print(f"video mode     {mode_name}" + (f" ({mode_id})" if mode_id >= 0 else ""))
    ae = cam.ae_mode
    if ae is not None:
        print(f"exposure       {AE_MODES.get(ae, f'mode {ae}')}")
    iso, shutter = cam.iso, cam.shutter
    if iso is not None:
        print(f"iso            {iso}")
    if shutter is not None:
        print(f"shutter        1/{shutter}s" if shutter else "shutter        -")
    gestures = None
    try:
        gestures = cam.get_gestures()
    except XUError:
        pass
    if gestures is not None:
        on = [str(b) for b in GESTURE_BITS if gestures >> b & 1]
        print(f"gestures       0x{gestures:02x} (groups on: {', '.join(on) or 'none'})")
    try:
        print(f"audio denoise  {'on' if cam.get_denoise() else 'off'}")
    except XUError:
        pass
    ptz = cam.ptz()
    print(f"pan / tilt     {_deg(ptz['pan'])} / {_deg(ptz['tilt'])}")
    print(f"zoom           {_zoom(ptz['zoom'])}")

    print("\nextension units")
    names = {XU_MAIN_GUID: "main", XU_TRACK_GUID: "tracking", XU_FRAME_GUID: "framing"}
    for guid, unit in sorted(cam.units.items(), key=lambda kv: kv[1]):
        print(f"  unit {unit:<3} {names.get(guid, 'unknown'):8} {guid}  "
              f"{cam.unit_controls.get(unit, 0)} selectors")
    return 0


def cmd_list(cam: Camera, args) -> int:
    for slug, ctrl in cam.controls.items():
        kind = {CTRL_INTEGER: "int", CTRL_BOOLEAN: "bool", CTRL_MENU: "menu",
                CTRL_BUTTON: "button"}.get(ctrl.type, str(ctrl.type))
        flags = []
        if not ctrl.writable:
            flags.append("read-only")
        if ctrl.flags & V4L2_CTRL_FLAG_INACTIVE:
            flags.append("inactive")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        if ctrl.type == CTRL_BOOLEAN:
            span = ""
        elif ctrl.is_angle:
            span = (f"  {ctrl.min / ARCSEC_PER_DEGREE:+.0f}.."
                    f"{ctrl.max / ARCSEC_PER_DEGREE:+.0f}deg")
        else:
            span = f"  {ctrl.min}..{ctrl.max} step {ctrl.step}"
        print(f"{slug:34s} {kind:7s}{span}{suffix}")
        for value, label in ctrl.menu.items():
            print(f"{'':36s}  {value}: {label}")
    return 0


def cmd_get(cam: Camera, args) -> int:
    slugs = args.names or list(cam.controls)
    for slug in slugs:
        ctrl = cam.control(slug)
        try:
            print(f"{slug:34s} {ctrl.format(cam.get(slug))}")
        except OSError as exc:
            print(f"{slug:34s} <{exc.strerror}>")
    return 0


def cmd_set(cam: Camera, args) -> int:
    for assignment in args.assignments:
        if "=" not in assignment:
            raise SystemExit(f"expected NAME=VALUE, got {assignment!r}")
        slug, _, text = assignment.partition("=")
        ctrl = cam.control(slug.strip())
        text = text.strip()
        if ctrl.type == CTRL_BOOLEAN and text.lower() in ("on", "off", "true", "false", "yes", "no"):
            value = 1 if text.lower() in ("on", "true", "yes") else 0
        elif ctrl.is_angle:
            # `list` and `get` both speak degrees for these, so `set` has to as
            # well -- taking a bare number as arcseconds would make
            # `set pan_absolute=10` a 0.003 degree move.
            value = round(float(text.lower().removesuffix("deg")) * ARCSEC_PER_DEGREE)
        else:
            value = round(float(text))
        actual = cam.set(ctrl.slug, value)
        # Report the clamp in the same units the value was given in.
        note = "" if actual == value else f"  (clamped from {ctrl.format(value)})"
        print(f"{ctrl.slug:34s} {ctrl.format(actual)}{note}")
    return 0


def cmd_ptz(cam: Camera, args) -> int:
    if args.home:
        cam.home()
    nudge = {"left": ("pan_absolute", -1), "right": ("pan_absolute", 1),
             "up": ("tilt_absolute", 1), "down": ("tilt_absolute", -1)}
    if args.nudge:
        slug, sign = nudge[args.nudge]
        ctrl = cam.control(slug)
        step = round(args.step * ARCSEC_PER_DEGREE)
        cam.set(slug, cam.get(slug) + sign * step)
    if args.pan is not None:
        cam.set("pan_absolute", round(args.pan * ARCSEC_PER_DEGREE))
    if args.tilt is not None:
        cam.set("tilt_absolute", round(args.tilt * ARCSEC_PER_DEGREE))
    if args.zoom is not None:
        cam.set("zoom_absolute", round(args.zoom * 100))
    ptz = cam.ptz()
    print(f"pan {_deg(ptz['pan'])}  tilt {_deg(ptz['tilt'])}  "
          f"zoom {_zoom(ptz['zoom'])}")
    return 0


def cmd_preset(cam: Camera, args) -> int:
    presets = load_presets()
    if args.action == "list":
        if not presets:
            print("no presets saved")
        for name, snapshot in sorted(presets.items()):
            pan = snapshot.get("pan_absolute", 0) / ARCSEC_PER_DEGREE
            tilt = snapshot.get("tilt_absolute", 0) / ARCSEC_PER_DEGREE
            zoom = snapshot.get("zoom_absolute", 100) / 100
            print(f"{name:24s} pan {pan:+.1f}deg  tilt {tilt:+.1f}deg  zoom {zoom:.2f}x")
        return 0
    if not args.name:
        raise SystemExit(f"`preset {args.action}` needs a name")
    if args.action == "save":
        presets[args.name] = capture_preset(cam)
        save_presets(presets)
        print(f"saved preset {args.name!r}")
    elif args.action == "recall":
        if args.name not in presets:
            raise SystemExit(f"no preset named {args.name!r}")
        apply_preset(cam, presets[args.name])
        print(f"recalled preset {args.name!r}")
    elif args.action == "rm":
        if presets.pop(args.name, None) is None:
            raise SystemExit(f"no preset named {args.name!r}")
        save_presets(presets)
        print(f"deleted preset {args.name!r}")
    return 0


def cmd_gestures(cam: Camera, args) -> int:
    if args.value is not None:
        text = args.value.lower()
        if text in ("on", "all", "true", "yes"):
            mask = GESTURE_MASK
        elif text in ("off", "none", "false", "no"):
            mask = 0
        else:
            mask = int(text, 0)
        actual = cam.set_gestures(mask)
        if actual != (mask & GESTURE_MASK):
            print(f"firmware kept 0x{actual:02x} of requested 0x{mask:02x}")
    mask = cam.get_gestures()
    on = [str(b) for b in GESTURE_BITS if mask >> b & 1]
    print(f"gestures 0x{mask:02x} (groups on: {', '.join(on) or 'none'})")
    return 0


def cmd_denoise(cam: Camera, args) -> int:
    if args.value is not None:
        cam.set_denoise(args.value.lower() in ("on", "true", "yes", "1"))
    print(f"audio denoise {'on' if cam.get_denoise() else 'off'}")
    return 0


def cmd_exposure(cam: Camera, args) -> int:
    wrote = args.auto or args.iso is not None or args.shutter is not None
    if args.auto:
        cam.set_ae_mode(AE_AUTO)
    if args.iso is not None:
        if not ISO_MIN <= args.iso <= ISO_MAX:
            print(f"iso clamped to {max(ISO_MIN, min(ISO_MAX, args.iso))} "
                  f"(usable range {ISO_MIN}-{ISO_MAX})")
        cam.set_iso(args.iso)
    if args.shutter is not None:
        if not SHUTTER_MIN <= args.shutter <= SHUTTER_MAX:
            print(f"shutter clamped to 1/{max(SHUTTER_MIN, min(SHUTTER_MAX, args.shutter))}s "
                  f"(usable range 1/{SHUTTER_MIN}-1/{SHUTTER_MAX})")
        got = cam.set_shutter(args.shutter)
        if got != args.shutter and SHUTTER_MIN <= args.shutter <= SHUTTER_MAX:
            print(f"firmware rounded 1/{args.shutter}s to 1/{got}s")
    if wrote:
        # ISO and shutter are live telemetry, not stored settings: give the
        # firmware a beat to republish them before reporting what it did.
        time.sleep(XU_WRITE_GAP)
    ae, iso, shutter = cam.ae_mode, cam.iso, cam.shutter
    print(f"exposure {AE_MODES.get(ae, f'mode {ae}')}  "
          f"iso {iso}  shutter 1/{shutter}s")
    line = cam.get("power_line_frequency")
    if ae == AE_MANUAL and line in (1, 2) and shutter:
        mains = 50 if line == 1 else 60
        if shutter % mains:
            print(f"warning: 1/{shutter}s is not a multiple of {mains} Hz -- "
                  f"expect flicker banding. Try 1/{mains} or 1/{mains * 2}.")
    return 0


def cmd_monitor(cam: Camera, args) -> int:
    print("iso / shutter / mode / pan / tilt / zoom -- Ctrl+C to stop")
    try:
        while True:
            ptz = cam.ptz()
            mode = cam.video_mode[1]
            iso, shutter = cam.iso, cam.shutter
            sys.stdout.write(
                f"\riso {iso or 0:<6} 1/{shutter or 0:<5}s  {mode:<16} "
                f"pan {(ptz['pan'] or 0) / ARCSEC_PER_DEGREE:+7.1f}deg "
                f"tilt {(ptz['tilt'] or 0) / ARCSEC_PER_DEGREE:+7.1f}deg "
                f"zoom {(ptz['zoom'] or 100) / 100:.2f}x   ")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
    return 0


def cmd_xu(cam: Camera, args) -> int:
    unit = cam.main_unit if args.unit is None else args.unit
    if args.action == "map":
        guid = cam.unit_guid(unit)
        # Only the main unit has known selector names; for the others the
        # device's own control count is all we have to go on.
        count = cam.unit_controls.get(unit, max(XU_MAIN_SELECTORS))
        for sel in range(1, count + 1):
            try:
                size, info = cam.xu_len(unit, sel), cam.xu_info(unit, sel)
            except XUError:
                continue
            access = ("r" if info & XU_INFO_GET else "-") + \
                     ("w" if info & XU_INFO_SET else "-")
            harm = DANGEROUS.get((guid, sel))
            warn = f"  !! {harm}" if harm else ""
            name = XU_MAIN_SELECTORS.get(sel, "?") if guid == XU_MAIN_GUID else "?"
            unit_word = "byte " if size == 1 else "bytes"
            print(f"  sel {sel:<3} {access}  {size:>4} {unit_word}  "
                  f"{name}{warn}")
        return 0
    if args.action == "get":
        print(cam.xu_read(unit, args.selector).hex(" "))
        return 0
    data = bytes.fromhex(args.data.replace(" ", ""))
    cam.xu_write(unit, args.selector, data, force=args.force)
    # Several selectors on the tracking and framing units are write-only, so a
    # readback is a bonus rather than a result.
    try:
        print(cam.xu_read(unit, args.selector).hex(" "))
    except XUError as exc:
        print(f"written; no readback ({exc})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link2ctl", description="Control an Insta360 Link 2 on Linux.")
    parser.add_argument("-d", "--device", help="video node (default: autodetect)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("info", help="identity, extension units and live telemetry")
    sub.add_parser("list", help="every V4L2 control with its range")

    p = sub.add_parser("get", help="read controls")
    p.add_argument("names", nargs="*")

    p = sub.add_parser("set", help="write controls, as NAME=VALUE")
    p.add_argument("assignments", nargs="+")

    p = sub.add_parser("ptz", help="move the gimbal")
    p.add_argument("--pan", type=float, metavar="DEG")
    p.add_argument("--tilt", type=float, metavar="DEG")
    p.add_argument("--zoom", type=float, metavar="X")
    p.add_argument("--home", action="store_true")
    p.add_argument("--nudge", choices=("left", "right", "up", "down"))
    p.add_argument("--step", type=float, default=5.0, metavar="DEG",
                   help="nudge size in degrees (default 5)")

    p = sub.add_parser("preset", help="save and recall camera positions")
    p.add_argument("action", choices=("list", "save", "recall", "rm"))
    p.add_argument("name", nargs="?")

    p = sub.add_parser("gestures", help="gesture-control bitmask")
    p.add_argument("value", nargs="?", help="on, off, or a mask like 0x0e")

    p = sub.add_parser("denoise", help="microphone noise cancellation")
    p.add_argument("value", nargs="?", choices=("on", "off"))

    p = sub.add_parser("exposure", help="auto or manual ISO and shutter")
    p.add_argument("--auto", action="store_true", help="hand exposure back to the camera")
    p.add_argument("--iso", type=int, metavar="N",
                   help=f"pin sensor gain, {ISO_MIN}-{ISO_MAX} (implies manual)")
    p.add_argument("--shutter", type=int, metavar="N",
                   help=f"pin exposure to 1/N s, {SHUTTER_MIN}-{SHUTTER_MAX} "
                        "(implies manual)")

    p = sub.add_parser("monitor", help="live telemetry")
    p.add_argument("--interval", type=float, default=0.5)

    p = sub.add_parser("xu", help="raw extension-unit access")
    p.add_argument("action", choices=("map", "get", "set"))
    p.add_argument("selector", nargs="?", type=lambda s: int(s, 0))
    p.add_argument("data", nargs="?", help="hex bytes for `set`")
    p.add_argument("--unit", type=lambda s: int(s, 0))
    p.add_argument("--force", action="store_true",
                   help="allow writing selectors that can disconnect the camera")
    return parser


COMMANDS = {"info": cmd_info, "list": cmd_list, "get": cmd_get, "set": cmd_set,
            "ptz": cmd_ptz, "preset": cmd_preset, "gestures": cmd_gestures,
            "denoise": cmd_denoise, "exposure": cmd_exposure,
            "monitor": cmd_monitor, "xu": cmd_xu}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from link2ctl_gui import run_gui       # noqa: PLC0415 - optional GUI deps
        return run_gui(args.device)
    with Camera.open_first(args.device) as cam:
        return COMMANDS[args.command](cam, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except XUError as exc:
        sys.exit(f"link2ctl: {exc}")
