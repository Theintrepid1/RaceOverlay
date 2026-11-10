# Race Overlay and Race Analysis

Two desktop apps for track days, for Linux and Windows:

- **Race Overlay** burns telemetry gauges onto your onboard video.
- **Race Analysis** is the one to keep open at the track. It compares laps and sessions, shows where you gained or lost time, and plays the matching video.

They work with APEX Pro, AiM, TrackAddict, Harry's LapTimer, RaceBox, VBO files (Racelogic VBOX, RaceChrono) and most other CSV exports, with GoPro video (And more if you send me your data and videos.)

## Install

- **Linux:** download `RaceOverlay-<version>-x86_64.AppImage` from Releases, mark it executable, and double-click it. The first launch offers to add both apps to your menu.
- **Windows:** run `RaceOverlay-<version>-windows-setup.exe` from Releases. It's a per-user install with no admin rights needed. It's unsigned for now, so click "More info → Run anyway".

## Race Overlay

- **Sync:**
  - Auto-sync matches the GoPro's own accelerometer to the data file.
  - Or line it up by hand at a start/finish crossing.
  - Fine-tune with Earlier/Later, down to one frame.
  - GoPro chapters (GH01…, GH02…) open as one video.
- **Gauges:**
  - Speed, g-ball, track map, lap timer with live delta, and corner entry/min speed.
  - Number, bar or dial gauges for any channel in the file, like RPM, oil temp or brake pressure.
  - An iRacing-style delta bar, a race-dash cluster (shift lights, gear, four channels of your choice, pedal bars), and logo and text gauges.
  - Gauges can be dragged and resized, and there are four styles.
  - A gauge whose channel isn't in the file just hides, so one layout works for every logger.
- **Export:**
  - The whole video or chosen laps, as one file or one per lap.
  - Trim the ends, so the paddock drive and false grid aren't in the video.
  - Picture-in-picture: a second GoPro (driver-facing, rear) in a box, synced from its own sensors, with crop, rounded corners and a border.
  - Optional intro and end cards: title, driver and car at the start, best lap and top speed at the end.
  - Files are named like `2026-09-27 VIR S1 lap5 2'19.806.mp4`.
- **Fast rendering:**
  - Renders in parallel chunks, on the GPU where possible (NVIDIA, Intel, AMD).
  - Pause, stop and resume, even after closing the app.
  - Saving to a NAS is quick because only the finished file crosses the network.

## Race Analysis

- **GoPro import:**
  - Copies the card into a folder per day and matches each video to its data file.
  - Fixes a camera clock that reset to 2016, using the GoPro's GPS time.
- **Laps:**
  - Lap times interpolated to the line, sector times, best sectors and the theoretical best.
  - Delta, speed, g and engine charts against distance; you pick which ones.
- **Corners & grip:**
  - Time lost or gained in each turn, minimum speed and braking point against your reference lap.
  - Grip used, measured against the most your car has shown it can do.
  - A g-g plot.
  - Loop the video on any turn.
- **Day:**
  - Compare any number of sessions, step by step or against a baseline.
  - It tells you whether a change showed up in every turn (car, tires, track) or only a few (driving).
  - Notes for each session, tire pressures cold and hot, and weather.
- **Video:** the synced video follows the cursor. A second video can be stacked to compare against the reference lap.
- **Tracks:**
  - VIR Full Course is built in with official turn names and sector lines. CMP Full Course is built in too, with its start line and sector lines.
  - Name any track's turns from its map image in a few clicks, and add your own sector lines.

## Running from source

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./install-launcher.sh          # adds both apps to the menu
```

You'll also need `ffmpeg` and `mpv` from your distro. On Windows, run `windows\setup.bat` once, then use `windows\RaceOverlay.bat` and `windows\RaceAnalysis.bat`. Packaging is described in [packaging/BUILDING.md](packaging/BUILDING.md), and the version history is in [CHANGELOG.md](CHANGELOG.md).

## License

Race Overlay is free software under the GNU General Public License v3 or later, and comes WITHOUT ANY WARRANTY; see [LICENSE](LICENSE).

The AppImage and the Windows installer bundle FFmpeg (GPL), mpv (GPL), Qt (LGPL) and other components; see [packaging/NOTICE.txt](packaging/NOTICE.txt). Anyone you give them to is entitled to the source code, including this repository's.
