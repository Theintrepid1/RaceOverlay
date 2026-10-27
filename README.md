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

## The app (start here)

Video with a live overlay preview on the left; four tabs on the right.

**Sync tab**
- **Files:** open the video and the data CSV (remembers folders; desktop file
  dialog with Network places). Open any GoPro chapter (GH01xxxx, GH02xxxx…)
  and it offers to load the whole recording as one video, without writing
  anything to disk. **Merge files…** joins any videos in an order you choose;
  **Save merged file** writes them as one MP4 losslessly.
- **1 · Line it up:** pause where the car crosses start/finish, pick which
  lap is starting, click **Sync here**. Or Ctrl+click a speed drop in the trace.
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
