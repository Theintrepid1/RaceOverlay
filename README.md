# raceoverlay

Burn racing telemetry gauges (speed, throttle/brake, g-force, track map, lap)
onto video, with a GUI for lining the data up by eye.
Python + ffmpeg. Works with phone-app exports (TrackAddict, Harry's LapTimer)
and dedicated loggers (AiM, VBOX, RaceBox) via flexible column matching.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

System packages (CachyOS/Arch): `sudo pacman -S ffmpeg mpv` (mpv provides
libmpv for the sync GUI).

## Install (AppImage, no terminal)

Download `RaceOverlay-<version>-x86_64.AppImage`, allow it to run
(right-click > Properties > Permissions > "Is executable" / "Allow
executing"), and double-click. First launch offers to add it to your app
menu. Everything it needs is inside; see `packaging/HOW-TO-OPEN.txt` (the
note to send along with it) and `packaging/BUILDING.md` (how it's built).

## Running from source

## App launcher (no terminal after setup)

One-time setup, then it's double-click from the app menu:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
./install-launcher.sh            # add --desktop for a desktop icon too
```

"Race Overlay" then appears in the KDE menu and under right-click > Open With
on video files. It asks for the video and CSV with file pickers, reopens the
saved sync if one exists, and shows problems as dialogs. Logs go to
`~/.local/state/raceoverlay/launch.log`. Moved the folder? Re-run the installer.

## Windows (from source, for testing)

There's no Windows installer yet. To try it on Windows from a copy of this
repo: install Python 3.11–3.13 (`winget install Python.Python.3.13`), then
double-click `windows\setup.bat` once. It makes `.venv`, installs the Python
packages and downloads ffmpeg + libmpv into `windows-bin\` (about 250 MB).
After that, `windows\RaceOverlay.bat` and `windows\RaceAnalysis.bat` start
the apps. Logs go to `%LOCALAPPDATA%\raceoverlay\launch.log`; settings live
in the registry (`HKCU\Software\raceoverlay`), saved tracks in
`%APPDATA%\raceoverlay`.

What's different on Windows: GPU encoding uses Intel QSV, NVIDIA NVENC or AMD
AMF (whichever works; the app tests each); preview decoding uses Direct3D 11;
SD cards are found on any drive letter with a `DCIM\1xxGOPRO` folder.

## The app (start here)

Video with a live overlay preview on the left; four tabs on the right.

**Sync tab**
- **Files:** open the video and the data CSV (remembers folders; desktop file
  dialog with Network places). Open any GoPro chapter (GH01xxxx, GH02xxxx…)
  and it offers to load the whole recording as one video, without writing
  anything to disk. **Merge files…** joins any videos in an order you choose;
  **Save merged file** writes them as one MP4 losslessly.
- **Videos imported in Race Analysis** open already synced to their data
  file (the sync is saved next to the video).
- **1 · Line it up:** **Auto-sync from GoPro sensors** matches the camera's
  accelerometer to the data file's g-forces (no camera GPS needed; needs the
  original GoPro files, since merged or re-encoded videos lose the sensor
  track). Weak matches ask first. Or by hand: pause where the car crosses
  start/finish, pick which lap is starting, click **Sync here**, or Ctrl+click
  a speed drop in the trace.
- **2 · Fine-tune:** gauges react before the car → **Later**; lag → **Earlier**.

**Export tab**
- **What:** whole video, or selected laps as one file each or joined, with
  extra seconds before/after (default 3s).
- **Format:** resolution (source or smaller), frame rate (120fps sources
  default to 60), quality, encoder (GPU encoders listed only if they actually
  work on your machine), parallel jobs.
- **Render** shows live fps, speed vs realtime and time left. **Pause** freezes
  it instantly; **Stop** keeps finished chunks, and pressing Render again
  resumes where it stopped, even after closing the app.

**Look tab:** style, background opacity (0–100%, or the style's own),
gauges, Move & resize, g-ball flips, units.
- **Data gauges:** Number, Bar or Dial, fed by any channel in the file
  (speed, RPM, oil temp, brake pressure, steering angle, AFR...). Set label,
  unit conversion (°C→°F, bar→psi, km/h→mph...), decimals, range and
  redline; ranges fill in automatically from the data, and the editor shows
  a trace of the channel. Speed, lap number and throttle/brake are data
  gauges too, so you can edit or delete them.
- **Thin files are fine:** a gauge whose channel isn't in the current file is
  greyed out and skipped (no dead zeros), and comes back when you open a file
  that has it. One layout works for a phone app and a full AiM car log.
- **Built-in gauges:** g-ball, track map, plus:
- **Lap timer:** running lap time, last and best lap, and a live delta (green =
  ahead) matched by position around the track. Compare to the best lap *so far*
  (no spoilers) or the session's best; optional delta bar. When you cross the
  line, the lap you just finished shows for 4 s, purple if it's a new best.
  Out-laps and in-laps get a clock but no delta.
- **Corner** (off by default): entry speed, minimum speed and peak lateral g,
  with the header showing which way: ◀ LEFT G or RIGHT G ▶. One braking event =
  one corner, so a left-right combo taken in one slowdown (VIR's Roller Coaster)
  is a single corner; a direction change only starts a new corner if you got
  back on the gas in between. Direction is wherever the peak g happened.
  A corner is anywhere you slow by 5+ mph: entry = speed where slowing began,
  min = the low point. Detected on every lap from your own speed trace, so a
  different line or braking point doesn't throw it off; flat-out sweepers
  don't count. The last corner stays up until the next one. *Live* fills in
  (accent color) as you go through; *Summary* updates after each corner.
  Optional +/- vs the same corner on the delta lap.

**Setup tab:** which device the file came from and which column feeds each
channel (speed, throttle, brake, RPM, lap, g-force, GPS), with overrides.
G-force columns are checked against GPS and their sign fixed automatically
(e.g. AiM's CAN lateral-g channel is inverted). Plus g-sensor alignment for
raw-axis loggers (APEX Pro).

**Supported data:** AiM Race Studio CSV (full sessions or single laps),
APEX Pro, and generic CSVs (TrackAddict, Harry's LapTimer, RaceBox...).
VBOX, RaceChrono and native AiM .xrk are planned.

Keys: Space or click video = play/pause · ←/→ 1s (Shift 5s) ·
Ctrl+←/→ one frame · Ctrl+Shift+←/→ gauges earlier/later by one frame ·
`[` `]` gauges later/earlier 0.1s · Ctrl+O open · Ctrl+S save.

**How rendering got fast:** the export is split into short chunks rendered in
parallel (one per CPU core), only the gauge areas are sent to ffmpeg instead
of full transparent frames, encoding uses your GPU when available (Intel Quick
Sync, AMD, NVIDIA), and audio is added once at the end.

## Race Analysis (at the track)

Race Analysis is the app to keep open at the track; Race Overlay is for
making the video afterwards. Start it from the app menu ("Race Analysis"),
or with `raceoverlay --analysis [data.csv ...]` / `./launch.sh --analysis`.

- **Import from GoPro card…:** lists the recordings on the card (newest first,
  already-copied ones unticked), copies the ticked ones into a folder per day
  (`<library>/2026-09-27/`) with chapters kept as-is, then matches each to a
  data file in your data folder by syncing against every candidate from that
  day. Matches are saved next to the video (so Race Overlay opens them synced)
  and added to the analysis. Nothing on the card changes.
- **Open track day…** loads every synced session in a day folder.
  **Add data file…** adds one by hand; **Find video…** picks the GoPro file
  for the selected session and syncs it from the camera's sensors.
- **Lap list:** "Lap 5 – 2:19.806", gap to the reference (the fastest lap
  unless you pick another with **Use as reference**), and sector times (equal
  thirds of the lap). Best sectors are purple; the theoretical best adds them
  up. Several sessions from the same track can be compared.
- **Traces against distance:** delta to the reference, speed, and braking /
  acceleration g for the reference plus up to four laps, with a legend. Laps
  are lined up by GPS position on the reference lap.
- **Turns:** with no setup, corners are found from speed dips and numbered
  C1, C2… **Track turns…** names them like the official map: load the
  track's map image, line it up by clicking the same spot on the image and on
  your trace twice, then click each turn number. Split turns work: type 4A in
  "Next turn" before clicking it and the next click becomes 4B. Saved per track and reused on
  every visit (in `~/.local/share/raceoverlay/tracks/`).
- **Per turn, for the selected lap:** time lost or gained in that turn's
  stretch of track, minimum speed, where braking started vs the reference, and
  the best any lap did there. Sorted worst first; click one to jump the video
  to its braking zone.
- **Grip used:** an envelope of the most grip you've shown the car can do in
  every direction (cornering, braking, trail-braking), built from all loaded
  laps. Grip used = combined g ÷ that envelope, so 100% is as hard as you've
  ever pushed that way; straights and pure acceleration (engine-limited) are
  left out. Shown per lap in the lap list, per turn (this lap / reference) in
  the turns table, and along the lap as a trace.
- **g-g (grip) tab:** the envelope (dashed) with the selected lap and the
  reference plotted inside it; click a turn in the table to show just that
  turn's path, again to go back to the whole lap. It's relative to your own
  data, not the tyre's absolute limit, and grows as you push harder. g comes
  from the data logger (on every lap); on test data the GoPro's accelerometer
  agreed within ~0.05 g in corners but read ~0.1 g more under braking.
- **Track map:** coloured where the selected lap gained (green) or lost (red)
  time; click it to move the cursor.
- **Video:** the session's synced video follows the cursor, and the cursor
  follows the video while it plays.

## CLI usage

```bash
# See it work with synthetic data + a test clip:
PYTHONPATH=src .venv/bin/python -m raceoverlay.cli --demo -o out.mp4

# Inspect how your CSV columns map to gauges (no render):
PYTHONPATH=src .venv/bin/python -m raceoverlay.cli --csv run.csv --inspect

# Render your footage (quick 15s preview first):
PYTHONPATH=src .venv/bin/python -m raceoverlay.cli \
    --video run.mp4 --csv run.csv -o out.mp4 --limit 15 --offset 0

# Full render:
PYTHONPATH=src .venv/bin/python -m raceoverlay.cli \
    --video run.mp4 --csv run.csv -o out.mp4 --offset 0
```

### Key flags
- `--sync FILE` — use the offset/csv/g-rotate/units saved by the GUI.
  `--video X.MP4 --sync X.MP4.sync.json -o out.mp4` renders a synced session.
- `--offset SEC` — align data to video. `+` means the data clock is ahead of
  the video start. offset = (data time of an event) − (video time of that event).
- `--start SEC` — begin render at this video time (preview a mid-session lap
  without rendering from the start of a long file).
- `--fps F` — output framerate (default: source fps). A 120fps GoPro renders
  ~4x faster at `--fps 30`; use it for sync previews.
- `--g-rotate DEG` / `--g-flip` — raw ax/ay sensor alignment. Leave off to
  auto-detect from GPS (VIR sample: −45°, 96%/99% fit).
- `--style NAME` — Clean, Panel, Race or Minimal.
- `--invert-lat` / `--invert-lon` — flip the g-ball left/right or up/down.
- `--map chan=Column` — override auto-detection, e.g. `--map speed="GPS Speed (m/s)"`.
  Channels: time, speed, throttle, brake, rpm, lat_g, lon_g, ax, ay, az, lap,
  gps_lat, gps_lon.
- `--limit SEC` — only render the first N seconds (quick preview).
- `--kmh` — display km/h instead of mph.
- `--crf N` — output quality (lower = better/larger, default 18).

### APEX Pro + GoPro example (sync workflow)
```bash
# 1. Find the video time of a start/finish crossing, then compute offset:
#    offset = (data lap-crossing time) - (video crossing time)
# 2. Preview that lap to confirm the map dot tracks:
... --video gopro.MP4 --csv apex.csv --g-rotate -45 \
    --start <video_sec> --offset <offset> --fps 30 --limit 20 -o preview.mp4
# 3. Full render once synced (drop --start/--limit, raise fps or omit --fps):
```

## Status
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

Later, RPM/throttle gauges when OBD
present, gauge layout/theme config, multi-file (chaptered) sessions.

## License
Race Overlay is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE).

The AppImage bundles FFmpeg (GPL), mpv (GPL), Qt (LGPL) and other components;
see [packaging/NOTICE.txt](packaging/NOTICE.txt). Anyone you give the AppImage
to is entitled to the source code, including this repository's.
