# Insta360 Link 2 — control protocol on Linux

Everything here was derived from two sources, starting 2026-10-03 and extended
2026-10-06: the camera itself (USB `2e1a:4c04`, serial `IBNLB2409MG6W8`, as
`/dev/video0`), and the shipped Windows build
`Insta360LinkController_2.2.4(build14).exe`.

The enum tables were re-derived on 2026-10-06 by decoding the embedded
descriptors as actual `FileDescriptorProto` messages rather than by grepping
for name/number pairs, so the numbers below are now confirmed twice over by
independent means.

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
The descriptor spells them `XU_FIRMWARE_UPGRADE_CONTROL` / `XU_BLEND_DRAW_CONTROL`,
`XU_DOWNLOAD_FILE_CONTROL` / `XU_AF_MODE_CONTROL` and
`XU_UPLOAD_FILE_CONTROL` / `XU_EXPOSURE_CURVE_CONTROL`.

### How the app addresses a selector

Worth knowing because it shows a selector write is not always self-contained.
The app's phone/browser remote drives the camera over a websocket whose
`WebTransport.proto` carries:

```proto
message UVCExtendRequest {
  required string          curDeviceSerialNum = 1;
  required ParamType       paramType          = 2;   // which setting
  required ControlSelector selector           = 3;   // which XU selector
  repeated int32           data               = 4;
  optional int32           presetPosIndex     = 5;   // which preset slot
}
```

So every extension-unit access is tagged with a *semantic* `ParamType` as well
as the raw selector, and may carry a preset slot index alongside. `ParamType`
has 80-odd values; the ones that matter here are `PARAM_VIDEO_MODE = 5`,
`PARAM_PRESET_POSITION = 103`, `PARAM_PITCH = 61`, `PARAM_HOST_PTZ_INFO = 45`,
`PARAM_ISO_VALUE = 14`, `PARAM_SHUTTER_VALUE = 15`, `PARAM_AUTO_EXPOSURE = 17`
and `PARAM_RESET_PTZ = 3`. The `ParamType` is how the *host* decides what to do
— it is not a byte on the wire to the camera — which is why one selector (2)
backs several different operations.

> ⚠ **Selector 17 is destructive.** Writing 1/2/3 switches the device out of
> webcam mode (`0` = uvc, `1` = photo, `2` = mass storage, `3` = vendor). The
> camera disappears from `/dev/video*` until it is re-enumerated. `link2ctl`
> refuses to write it without `--force`.

> ⚠ **Selector 17 on the *tracking* unit reboots the camera.** Found the hard
> way while sweeping for the video-mode setter: writing it made the device
> vanish from the USB bus altogether — gone from `lsusb`, not merely remoded —
> with the `SET` ioctl returning `ETIMEDOUT`. It re-enumerated by itself about
> five seconds later with settings intact but the gimbal readback garbage
> (`pan +10100.9deg`), which `ptz --home` cleared. `link2ctl` now refuses this
> one without `--force` too. Both guards are keyed by unit **GUID**, not unit
> number, since the numbers are firmware-assigned.

## Units 10 and 11

Neither has selector names in any enum — `ControlSelector` covers the main unit
only — so these are raw observations from `link2ctl xu map --unit 10/11` plus
`GET_CUR` on whatever is readable. Values are from an idle camera in Tracking
mode.

**Unit 11, the framing unit** (`a8bd5df2…`), five one-byte selectors:

| Sel | r/w | Value |
|----|-----|-------|
| 1 | r- | `01` |
| 2 | rw | `00` |
| 3 | r- | `06` |
| 4 | -w | — |
| 5 | -w | — |

The shape is suggestive — two write-only command bytes next to readable status
bytes is what an acknowledged handshake looks like — but it is not the video
mode register: sel 3 reads `6` while the camera is demonstrably in mode `2`,
and writing the target mode to sel 2, 4 and 5 changed nothing.

**Unit 10, the tracking unit** (`e307e649…`), 26 selectors. Readable values
seen: sel 1 `00×8`, sel 4 `28ff0100…`, sel 6 `ff`, sel 8 `00×8`, sel 9 `00`,
sel 10 `00`, sel 11 `00ff`, sel 13 `0000`, sel 14 `00×255`, sel 15 `0300`,
sel 16 `00`, sel 17 `03`, sel 18 `01`, sel 19 `0300000000000000`, sel 20 `01`,
sel 21 `00×90`, sel 22 `03`, sel 23 `00`, sel 24 `…5c4400005c850000…`,
sel 25 `0300`, sel 26 `01`. Selectors 3, 7 and 12 are write-only.

Several of these drift between reads (15, 17, 19 were seen changing within half
a second on an idle camera), so treat single readings as weak evidence. And see
the warning above: **selector 17 reboots the camera.**

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
* **Manual exposure**: AE mode (sel 30), ISO (sel 25) and shutter (sel 29) are
  all writable, not just readable. Write `1` to sel 30 and the sensor stops
  auto-exposing; `2` hands it back. Verified against captured frames, not just
  readbacks — at a fixed 1/50s, mean frame luminance tracked ISO monotonically
  (ISO 200 → 86, 500 → 154, 1600 → 207 on a 0–255 scale), and at a fixed ISO
  500 the shutter did the same in reverse (1/25 → 159, 1/50 → 135, 1/100 → 96,
  1/200 → 70).

  Nothing on this path is range-checked. ISO 25600 and shutter 1/65535 are
  accepted and read back verbatim; the frames stop changing long before that,
  so the usable ranges are ISO 100–6400 (below 100 the sensor floors — ISO 25,
  50 and 100 produce identical frames) and shutter 1/25–1/8000. Some shutter
  values come back one lower than written: 1/30 lands on 1/29, 1/60 on 1/59.
  `link2ctl` clamps to the measured ranges rather than trusting the firmware.

  AE modes 0, 4 and 8 are also stored without complaint, but they behave as
  undocumented auto variants that settle on 1/33s — not a multiple of either
  mains frequency, so they band under artificial light. Only 1 and 2 are worth
  using, and `link2ctl` only offers those two.

### Writes are dropped if you rush them

Extension-unit writes are not reliably synchronous. A write issued within
~50 ms of the previous one is silently discarded: the ioctl returns success,
the firmware ignores it, and the readback still shows the old value. Measured
directly — a 0 ms and a 20 ms gap both drop the write, 50 ms and above land.
The readback lags the write by a similar amount, so reading immediately after
writing can report the *old* value even when the write did land.

Under an active video stream even a correctly spaced write occasionally goes
missing. So `link2ctl` does three things on this path: it spaces every XU write
at least 100 ms apart, it waits before reading back, and it re-writes up to
four times until the readback confirms the value. Without the retry, changing
ISO while the GUI preview is running fails perhaps one time in six.
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

`VideoModeType`, decoded from the binary's own `device.proto` descriptor:
0 `NORMAL_MODE`, 1 `AUTO_COMPOSITION`, 2 `TRACKING_MODE`, 4 `WHITEBOARD_MODE`,
5 `OVERHEAD_MODE`, 6 `DESKVIEW_MODE`, 7 `AUTOFRAMEING_MODE` (sic),
8 `SMARTWHITEBOARD_MODE`, 9 `REGIONALTRACK_MODE`, 10 `SMARTWHITEBOARD_QUERY`,
11 `SMARTWHITEBOARD_CONFIG`. 3 is deliberately absent, which matches the gap in
independent firmware notes. The last two are query/config operations rather
than framing modes, so a tool should not offer them as modes.

#### Byte 54 is not the write path

**Sel 2 is a read-only mirror of the live view state, and writing it does
nothing.** This is now a measured negative result rather than an assumption.
Seven payload encodings were tried — the record exactly as read with byte 54
replaced; the same with byte 0 forced to `0xff`; with byte 55 set as a flag;
all-zero records with the mode at byte 54 and at byte 0 — each retried four
times with a 3-second poll for the mode to move, all with a live 1080p MJPEG
preview running to satisfy the app's "preview is closed! Waiting for
opening..." precondition. Byte 54 never budged off the firmware's own value.

Sel 2 *is* genuinely live, which is what makes the negative result meaningful:
with the camera at zoom 1.70× the zoom field read `0xaa` = 170, and byte 0
reads `0xff` ("no preset") rather than the `0x00` seen earlier.

#### What the binary says the real path is

The app's own call graph rules sel 2 out directly:

* `Webcam::CameraInsta::AddCurrentPTZInfo` builds a preset by reading the video
  mode, the pan/tilt and the roll as **three separate** calls (`faile to get
  video mode from uvc_extend`, `faile to get pan tilt absolute value from
  uvc_extend`, `failed to get roll absolute value from uvc`). If the mode lived
  in the pan/tilt record, that would be one read, not three.
* `Webcam::CameraInsta::setPitch` logs `set host pitch to …` and then
  `failed to set video mode` — setting the host pitch is routed *through* the
  video-mode setter, so the setter takes a pitch argument.
* `Webcam::CameraInsta::setVideoModeToCamera` logs `Set DeskView mode with
  hostpitch: …`, confirming the same.

So the setter is `SetVideoMode(mode, aux)` where `aux` is the host-side struct
`VideoModeAuxiliaryData { mode, flag, hostpitch, ptz_check_result }` — a C++
struct, *not* a protobuf message (it appears only in Qt signal signatures and
log strings, never in a `FileDescriptorProto`). The host has to supply a
`ptz_check_result`, i.e. it performs a PTZ feasibility check and reports the
answer as part of entering the mode. That is consistent with
`can`t use deskview on vertical resolution!!!` and with the dedicated
`enterDeskViewFailed` / `enterWhiteBoardFailed` signals.

Around that sits a retried, acknowledged handshake:

```
exit from video mode: %d, then enter tracking mode
remaining times:
uvc transmission failed! Ready to exit track mode
preview is closed! Waiting for opening...
onSetVideoModeTimeOut / enterVideoModeFailed
video mode hasn`t changed from %d yet! should delay setting zoom.
```

You leave the current mode, poll until the camera confirms, then enter the
next, retrying a bounded number of times — and zoom changes must be deferred
until the mode has actually flipped.

#### Which selector carries it — still open

A sweep of the plausible writable selectors (main 1, 4, 9, 14, 15, 18, 19, 21,
27, 28; framing 2, 4, 5; tracking 6, 9, 10) writing the target mode as the
first byte, with a live preview, moved the mode in **none** of them. The sweep
did not complete: tracking sel 17 rebooted the camera partway through, and
every later probe in that run failed spuriously with `No such device`, so
tracking selectors 3, 4, 7, 8, 11, 12, 13, 15, 18, 19, 20, 22, 23, 25 and 26
remain genuinely untested. They are the obvious place to look next, but they
are also the unit that reboots the camera when poked, so the next attempt
wants the exact payload in hand first rather than another blind sweep.

Getting that payload means disassembling `SetVideoMode`. The caller
`setVideoModeToCamera` was located (`0x140bf8180`–`0x140bf920c` at image base
`0x140000000`) and its call into the camera wrapper identified, but following
it to the UVC transport is unfinished. It is left unimplemented rather than
shipped as a toggle that silently does nothing.

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

If no 1.10-dev `innoextract` is available (the distro package is 1.9, and
building it needs Boost *headers*, not just the `boost-libs` runtime), the
payload can be reached directly: the installer carries two `zlb\x1a` LZMA1
blocks, and decompressing the first one with `lzma.FORMAT_RAW` — reading the
one-byte `lc/lp/pb` properties and the 4-byte dictionary size that follow the
magic — yields ~1.25 GB containing every packaged file. The 208 MB app exe
starts at the first `MZ` whose `PE\0\0` signature checks out, and its length
matches the section table exactly, so it can be carved out and fed to
`objdump -d -M intel --start-address=…`.

Two cautions, both learned by getting them wrong:

* **Don't trust the enum numbers to a `grep`.** Decode the embedded
  `FileDescriptorProto` blobs properly instead; `WebTransport.proto`,
  `device.proto` and `settings.proto` are all present in full. A clean
  descriptor parse is itself an integrity check on those bytes, which a string
  grep is not. That is where the tables in this document come from —
  `ControlSelector` runs 1–30 and matches this unit's selector count exactly.
* **Don't measure extraction integrity by scanning for `0xe8` call bytes and
  checking whether the targets land on `.pdata` function starts.** A linear
  byte scan over 31 MB of code is overwhelmingly false positives, and the
  resulting hit rate looks like catastrophic corruption even on a perfectly
  good file. The honest check is to disassemble each `.pdata` function from its
  recorded `BeginAddress` to its `EndAddress` and confirm the sweep stays in
  sync and emits no `(bad)` instructions. On a correct extraction that passes
  for essentially every function.

`link2ctl xu map` prints the live selector map from whatever camera is attached,
with lengths and access bits read from the device rather than from this file.
It takes `--unit` now, so the tracking and framing units can be mapped too.
