# Insta360 Link 2 — control protocol on Linux

Everything here was derived from two sources on 2026-10-03: the camera itself
(USB `2e1a:4c04`, serial `IBNLB2409MG6W8`, as `/dev/video0`), and the shipped
Windows build `Insta360LinkController_2.2.4(build14).exe`.

The Windows installer is Inno Setup 6.3, which the packaged `innoextract` 1.9
cannot read; current `innoextract` (1.10-dev) unpacks it. The payload is a Qt 6
application, `app/Insta360 Link Controller.exe` (208 MB), which embeds its
protobuf `FileDescriptorProto` blobs. Those descriptors carry the vendor's own
`ControlSelector`, `ParamType` and `VideoModeType` enums with their numeric
values, which is where the selector names below come from — they are not
guesses.

## Transport

The camera is a plain UVC 1.10 device. Nothing needs a kernel module, a daemon,
or root — the user just needs read/write on the video node, which is the normal
desktop case.

* **Standard controls** (pan, tilt, zoom, focus, white balance, image) are
  ordinary V4L2 controls.
* **Vendor controls** go through `UVCIOC_CTRL_QUERY` on the same file
  descriptor.

```c
struct uvc_xu_control_query {       /* 16 bytes on x86-64 */
    __u8  unit, selector, query;
    __u16 size;
    __u8 *data;
};
#define UVCIOC_CTRL_QUERY  _IOWR('u', 0x21, struct uvc_xu_control_query)  /* 0xC0107521 */
```

`query` is the UVC request code: `SET_CUR` 0x01, `GET_CUR` 0x81, `GET_LEN` 0x85,
`GET_INFO` 0x86. `GET_INFO` returns a bitmask — bit 0 means the selector is
readable, bit 1 means writable.

Note that some published write-ups describe a raw vendor-class transfer with
`wValue` holding the selector directly. That is not how you reach these controls
from Linux; the kernel's UVC driver owns the interface, and `UVCIOC_CTRL_QUERY`
is the supported path.

## Extension units

Three units, found on this device. Look them up **by GUID**, not by number —
the unit IDs are assigned by the firmware and are not contractual.

| GUID | Unit here | Selectors |
|---|---|---|
| `faf1672d-b71b-4793-8c91-7b1c9b7f95f8` | 9 | 30 |
| `e307e649-4618-a3ff-82fc-2d8b5f216773` | 10 | 26 |
| `a8bd5df2-1a98-474e-8dd0-d92672d194fa` | 11 | 5 |

The selector counts come from `bmControls` in the USB configuration descriptor
(readable at `/sys/bus/usb/devices/*/descriptors`).

> **This is where the public write-ups are wrong for the Link 2.** They are
> Link 1 documents. They give unit 10 six selectors — it has 26 here — and unit
> 9 selector 2 a 52-byte payload, where this device reports 56. Trust `GET_LEN`.

## Unit 9 — the main control surface

Selector numbers are the values of the vendor's `ControlSelector` protobuf enum,
which runs 1–30 and matches this unit's 30 selectors exactly. Four independent
anchors confirm the enum *is* the selector map: `DEVICE_SN` (12) returns the
serial printed on the camera, `USB_MODE_SWITCH` (17) reads 0 for UVC mode,
`NOISE_CANCEL` (7) tracks the microphone denoise setting, and
`PANTILT_ABSOLUTE` (26) holds the live gimbal angle.

Lengths are what this device returned from `GET_LEN`; `r/w` is from `GET_INFO`.

| Sel | Name | Len | r/w |
|----|------|-----|-----|
| 1 | `EXEC_SCRIPT` | 4 | rw |
| 2 | `VIDEO_MODE` | 56 | rw |
| 3 | `DEVICE_INFO` | 234 | rw |
| 4 | `PTZ_CMD` | 262 | rw |
| 5 | `GESTURE_STATUS` | 1 | rw |
| 6 | `GESTURE_BIND` | 5 | rw |
| 7 | `NOISE_CANCEL` | 1 | rw |
| 8 | `FIRMWARE_UPGRADE` / `BLEND_DRAW` | 102 | rw |
| 9 | `EXPOSURE_VALUE` | 2 | rw |
| 10 | `TAKE_PICTURE` | 129 | rw |
| 11 | `DEVICE_STATUS` | 5 | r- |
| 12 | `DEVICE_SN` | 32 | rw |
| 13 | `DEVICE_LICENSE` | 129 | rw |
| 14 | `DEVICE_PARAM` | 1 | -w |
| 15 | `AF_MODE` / `DOWNLOAD_FILE` | 12 | rw |
| 16 | `EXPOSURE_CURVE` / `UPLOAD_FILE` | 255 | rw |
| 17 | `USB_MODE_SWITCH` | 1 | rw | ⚠ |
| 18 | `TRACK_SPEED` | 1 | rw |
| 19 | `LAYOUT_STYLE` | 1 | rw |
| 20 | `HEAD_LIST` | 97 | r- |
| 21 | `TRACK_TARGET` | 16 | rw |
| 22 | `PANTILT_RELATIVE` | 4 | rw |
| 23 | `MOBVOI_PUBKEY` | 129 | rw |
| 24 | `BIAS` | 4 | rw |
| 25 | `ISO` | 2 | rw |
| 26 | `PANTILT_ABSOLUTE` | 8 | rw |
| 27 | `FUNC_ENABLE` | 2 | rw |
| 28 | `VIDEO_RES` | 10 | rw |
| 29 | `EXPOSURE_TIME_ABSOLUTE` | 2 | rw |
| 30 | `AE_MODE` | 1 | rw |

The duplicated names at 8, 15 and 16 are enum aliases: the same selector means
something different once the camera has been switched into firmware-update mode.

> ⚠ **Selector 17 is destructive.** Writing 1/2/3 switches the device out of
> webcam mode (`0` = uvc, `1` = photo, `2` = mass storage, `3` = vendor). The
> camera disappears from `/dev/video*` until it is re-enumerated. `link2ctl`
> refuses to write it without `--force`.

## What was actually verified against the hardware

The single most important finding is that **most of these selectors are
unvalidated byte stores.** Writing a value and reading the same value back does
*not* prove the camera did anything — selectors 18, 19 and 30 cheerfully accept
and echo `255`. Any tool that reports "set OK" purely on readback is lying to
you. The list below separates what was proven from what was not.

### Proven to work

* **All standard V4L2 controls.** Verified by capturing frames before and after:
  setting `pan_absolute` physically moves the gimbal, and the readback snaps to
  the device's 3600-arcsec step grid.
  Ranges: pan ±145°, tilt −90°…+100°, zoom 1.00×–4.00×, focus 0–100,
  white balance 2000–10000 K, brightness/contrast/saturation/sharpness 0–100,
  hue ±15.
* **Gesture enable** (sel 5). This one *is* firmware-validated, which is how we
  know it is really parsed: the byte is masked to `0x0e`. Writing `0x1f` reads
  back `0x0e`; writing `0x01` reads back `0x00`. So this firmware honours three
  gesture groups (bits 1–3), not the five the Windows UI lists.
* **Microphone noise cancellation** (sel 7). Plain 0/1.
* **Telemetry reads**: serial (sel 12), ISO (sel 25) and shutter (sel 29) track
  real sensor state — ISO was observed moving between 2929 and 6393 as room
  lighting changed, so these are live readouts rather than stored values.
* **Current AI framing mode**: byte 54 of sel 2, maintained by the firmware and
  read-only in practice.

### Not solved: changing the AI framing mode

Selector 2 is a 56-byte view-state record:

```
[0]      preset index (0xff = none)
[1..37]  preset name
[38..41] int32  pan,  0.1°
[42..45] int32  tilt, 0.1°
[46..49] int32  roll
[50..53] int32  zoom ×100
[54]     current video mode   <- read-only
[55]     reserved
```

`VideoModeType` from the Windows binary: 0 `NORMAL`, 1 `AUTO_COMPOSITION`,
2 `TRACKING`, 4 `WHITEBOARD`, 5 `OVERHEAD`, 6 `DESKVIEW`, 7 `AUTOFRAMING`,
8 `SMARTWHITEBOARD`, 9 `REGIONALTRACK`. (3 is deliberately absent, which matches
the gap in independent firmware notes.)

Writing the mode was attempted at byte 0 and byte 54, as a `[subcommand, value]`
pair using the documented sub-parameters `0x10` and `0x12`, through unit 11
selector 2, and with a live video stream running. Byte 54 never moved off the
firmware's own value. What the Windows binary's log strings show is why:

```
exit from video mode: %d, then enter tracking mode
remaining times:
uvc transmission failed! Ready to exit track mode
preview is closed! Waiting for opening...
onSetVideoModeTimeOut / enterVideoModeFailed
```

So it is a retried, acknowledged handshake — you must leave the current mode and
wait for the camera to confirm before entering the next one — and the payload
carries a `VideoModeAuxiliaryData { mode, flag, hostpitch, ptz_check_result }`
rather than a bare mode byte. Reproducing that needs the state machine, not one
more poke. It is left unimplemented rather than shipped as a toggle that
silently does nothing.

### Deliberately not attempted

Unit 9 selectors 3, 4, 8, 10, 13, 16, 20, 21, 23 are bulk blobs, firmware
transfer and file I/O paths. Independent firmware analysis reports that the
vendor-class mode exposes unauthenticated read/write to arbitrary paths on the
camera's internal volume. None of that belongs in a settings app.

## Features that are not camera features at all

The Windows app's background blur, background replacement, green screen and
beauty filters do **not** live on the camera. Its binary contains a full
host-side computer-vision stack (`bva::FmgHeadTracker`,
`bva::DetectPersonTrackerConfig`, `bva::camera::WhiteboardModelConfig`, OpenCV)
plus `VirtualCameraService.exe`. The PC does the work and publishes the result
as a virtual camera. A Linux equivalent would be a `v4l2loopback` pipeline with
its own models — a separate project, not a camera control.

The AI framing, gesture recognition, gimbal tracking and HDR all run on the
camera, and persist across applications once set.

## Reproducing the binary analysis

```sh
# current innoextract; the packaged 1.9 cannot read Inno Setup 6.3
innoextract -e -I "app/Insta360 Link Controller.exe" "Insta360LinkController_2.2.4(build14).exe"

# enum name/number pairs: EnumValueDescriptorProto is literally
#   0A <len> <NAME> 10 <varint number>
grep -aoP '\x0a.[A-Z_]{4,40}\x10' "app/Insta360 Link Controller.exe"
```

`link2ctl xu map` prints the live selector map from whatever camera is attached,
with lengths and access bits read from the device rather than from this file.
