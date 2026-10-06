# link2ctl

A native Linux replacement for the Windows-only Insta360 Link Controller.

GTK4/libadwaita window plus a CLI, driving the camera over plain V4L2 and the
vendor's UVC extension units. No kernel module, no daemon, no root — just
read/write on `/dev/videoN`, which you already have.

Written against the camera on this machine: Insta360 Link 2, USB `2e1a:4c04`,
serial `IBNLB2409MG6W8`.

```
link2ctl                      open the window
link2ctl info                 identity, extension units, live telemetry
link2ctl list                 every control with its range
link2ctl get [NAME ...]
link2ctl set NAME=VALUE ...
link2ctl ptz [--pan DEG] [--tilt DEG] [--zoom X] [--home] [--nudge DIR]
link2ctl preset list|save NAME|recall NAME|rm NAME
link2ctl gestures [on|off|MASK]
link2ctl denoise [on|off]
link2ctl exposure [--auto] [--iso N] [--shutter N]
link2ctl monitor              live ISO/shutter/mode readout
link2ctl xu map|get|set       raw extension-unit access
```

## What it controls

**Position** — pan ±145°, tilt −90°…+100°, zoom 1×–4×, with an arrow pad, a
configurable nudge step, and named presets that also remember zoom, focus and
image settings. Angles are in degrees everywhere; V4L2's arcseconds stay
internal.

**Image** — brightness, contrast, saturation, hue, sharpness, white balance
(auto or 2000–10000 K), power line frequency, autofocus and manual focus.
Controls that the driver reports as inactive — manual white balance while auto
is on, for instance — are greyed out and come back automatically.

**Exposure** — auto, or manual ISO (100–6400) and shutter (1/25–1/8000). Auto
is the right default: it keeps the shutter on your mains frequency, so it will
not band under artificial light. Lock it manually when you want the look to
stay put between takes rather than drift as the room changes. If you pick a
shutter that is not a multiple of the power line frequency, link2ctl says so.

**Camera** — gesture control, microphone noise cancellation, and a live readout
of the AI framing mode, ISO, shutter and serial. The AI framing mode is the one
thing here that is read-only; see `PROTOCOL.md`.

Settings are applied on the camera, so they persist across applications and
survive unplugging the cable.

## Install

Needs `python3`, `python-gobject`, `gtk4`, `libadwaita`, and for the preview
`gstreamer` with `gst-plugins-good`. On Arch:

```sh
sudo pacman -S --needed python-gobject gtk4 libadwaita gstreamer gst-plugins-good
```

Then:

```sh
git clone https://github.com/janszafranski/link2ctl.git
cd link2ctl
./install.sh            # ~/.local/bin/link2ctl + a desktop entry
```

That puts **Insta360 Link 2** in your app launcher and `link2ctl` on your PATH.
`./uninstall.sh` takes it all back out and keeps your presets.

Or just run `./link2ctl.py` where it sits — nothing is hard-coded to an install
path.

## What it does not do

**Changing the AI framing mode** (Tracking / DeskView / Whiteboard / Overhead)
is read-only here. The current mode is reported, but setting it needs a retried
handshake with the camera that is not yet reverse-engineered — see
[PROTOCOL.md](PROTOCOL.md). Rather than ship a button that silently does
nothing, the mode is shown and not offered as a control. Use the gesture
controls or the button on the camera to switch modes meanwhile.

**Background blur, background replacement, green screen and beauty filters** are
not camera features. The Windows app runs those on the PC with its own CV models
and publishes a virtual camera; the camera knows nothing about them. Doing the
same on Linux means a `v4l2loopback` pipeline, which is a different project.

## A warning about other tools

Most of these extension-unit selectors are **unvalidated byte stores**. The
camera will accept and echo back nonsense — selectors 18, 19 and 30 all happily
store `255`. Readback matching what you wrote proves only that the firmware kept
the byte, not that anything happened. `link2ctl` only exposes writes that were
verified to have an effect, and `PROTOCOL.md` is explicit about which those are.

Selector 17 switches the camera out of webcam mode entirely, at which point it
vanishes from `/dev/video*`. `link2ctl xu set` refuses it without `--force`.

## Layout

| | |
|---|---|
| `link2ctl.py` | device access, V4L2 + extension units, CLI |
| `link2ctl_gui.py` | the GTK4/libadwaita window |
| `PROTOCOL.md` | the reverse-engineering notes and the selector map |
| `install.sh` / `uninstall.sh` | copies into `~/.local`, and takes it out again |

Presets live in `~/.config/link2ctl/presets.json`.
