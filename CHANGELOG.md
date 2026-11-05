# Changelog

v1.4.0
- **Delta bar:** an iRacing-style bar with the gap in the middle, blending red to green. Range, direction and gaining/losing colouring are adjustable. The lap timer drops its own delta while a bar is shown.
- **Dash cluster:** shift lights, an RPM bar graph, gear (worked out from RPM and speed), four channels of your choice, brake/throttle bars and a footer line. With no RPM it shows speed instead.
- **Snapping:** gauges snap to the middle, the edges and the corners while you drag them (hold Shift to place freely).
- **Picture-in-picture:** a second GoPro in a box on the video, synced from its own sensors, with crop, rounded corners, a border and opacity. It's off until you open a second camera.

v1.3.0
- **Trim ends:** cut the start and end of a whole-video export.
- **Readable file names:** date, track, session, lap and lap time.
- **A faster, clearer finish:** work files stay on this computer and only the finished video goes to the destination (a big win for NAS saves); every finishing step is named.
- **Lap times interpolated** to where the car crossed the line, not the first sample after it.
- **Day tab compares any number of sessions,** step by step or against a baseline.
- **CMP Full Course** in the track library; APEX exports without `.csv` recognized.

v1.2.0 — Race Analysis and Race Overlay polish:
- **Race Analysis:** pick your own charts, track-defined sectors, labels on the charts, and a better Day tab.
- **Import:** fixes a GoPro clock that reset to 2016.
- **Race Overlay:** logo and text gauges, optional intro and end cards, smoother round gauges.
- **Both apps:** separate icons.

v1.1.0 — Windows and a shared track library. Windows installer (per-user,
unsigned for now) built by GitHub Actions and attached to each release;
Race Analysis ships with built-in tracks (VIR Full Course to start), with
local edits, Reset to built-in and Export for sharing. Same version on Linux
and Windows.

v1.0.0 — first release. Same as v0.9.9, tested on NVIDIA (KDE Wayland) and
Intel.

v0.9.9 — 1.0 candidate. Side panel fits on any screen and is resizable;
advanced preview options tucked away; clearer frame-rate choices.

v0.9.8 — fixes garbage in the empty preview area before a video is opened.

v0.9.7 — clean GPU preview on NVIDIA/KDE Wayland: native player window drawn
through X11, built-in mpv by default.

v0.9.6 — fixes the native preview opening as a separate window on KDE
Wayland.

v0.9.5 — preview player now draws in its own native window by default (same
renderer as standalone mpv); runs through XWayland on Wayland desktops.
Setup > Preview > Player window switches back to the embedded mode.

v0.9.4 — fixes v0.9.3's system-mpv preference silently falling back to the
built-in player on newer distros (CachyOS/Arch).

v0.9.3 — preview uses the system's mpv when it's installed (clean NVIDIA GPU
decode); built-in player is the fallback. Setup tab shows which player is in
use and can force the built-in one.

v0.9.1 — preview decoding setting (default: GPU decode with safe copy), NVIDIA
NVDEC support, version in the window title. Fixes striped/blocky preview on
NVIDIA with nvidia-vaapi-driver.

v0.9 — AppImage: one file, no terminal, bundled ffmpeg 8.1 + lean libmpv
0.41, app-menu integration on first run, VAAPI preview decode via EGL.

v0.8.2 — combos under one slowdown stay one corner (Roller Coaster fix).

v0.8.1 — corner peak g shows left/right; combos split by direction;
gauge background opacity slider.

v0.8 — AiM importer, channel roles with fallbacks and GPS sign checks,
custom Number/Bar/Dial gauges on any channel, speed/lap/pedals converted.

v0.7.1 — purple best laps; corner gauge reworked (per-lap detection,
always visible, smaller, no turn numbers).
v0.7 — lap timer + delta gauge, corner gauge.

v0.6 — parallel chunked renderer (pause/resume/stop, ~2-3x faster on 2 cores
before GPU encoding), lap exports, render format options, GoPro chapter
merging, frame-step shortcuts.

v0.5 — overlay styles, drag/resize gauges, g-ball flips, auto g-sensor
alignment, file pickers in the app with remembered folders and network
places, track map keeps true shape, fonts bundled.

v0.4 — single-window GUI (mpv renders inside Qt, works on Wayland),
lap-crossing sync, earlier/later fine-tune buttons, player controls.

v0.3 — sync GUI (Qt + mpv): trace-based manual alignment, Ctrl+click snap,
frame-accurate nudges, live WYSIWYG overlay, lap jump/lap times, preview and
full render with progress, sidecar sync files read by the CLI.
Fixes: NaN-aware interpolation (mixed-rate CSVs), cached fonts, one CSV column
per channel, `--g-rotate` now overrides auto-detected named g columns,
"Engine Speed" no longer mistaken for vehicle speed.
