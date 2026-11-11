# Roadmap

## Next — planned
- CMP official turn names (placed from the track map's distance table)
- Day tab across several days (F2): decide after using multi-session compare
- Check against real data: the dash with an APEX OBD dongle log, picture-in-picture
  with two real GoPro recordings, the import clock fix on a reset-clock card, the
  event folder on the VIR 2021 folder over the NAS

## Next — collecting ideas, not started
- GoPro GPS positions for laps or sync (needs an outdoor clip with a solid 3D lock)
- Picture-in-picture sync for non-GoPro cameras (audio match)
- Track database stage 2: public tracks repo + "Check for track updates"
- Code signing (Azure Artifact Signing) once teammates have tried the build

## Done
- **v1.5.0** (2026-11-11):
  - **Updates:** daily check, download and install, one-click roll back; the AppImage adds both apps to the menu.
  - **Open event folder:** hand-organised folders matched up from the GoPro sensors, a second camera found or added, and remembered for next time.
  - **Sync by hand** window, from the event review and after import.
  - **VBO files**, with the file's own start line.
  - **Day tab:** Sessions / Compare / Grip sub-tabs; sessions In, Left out or Hidden.
- **v1.4.0** (2026-11-05):
  - **Delta bar** (iRacing-style), **dash cluster** gauge, and **snapping** while
    moving gauges.
  - **Picture-in-picture:** a second GoPro in a box, synced from its own sensors.
- **v1.3.0** (2026-11-02):
  - **Whole-video export:** trim the ends (under "Trim ends").
  - **File names** like `2026-09-27 VIR S1 lap5 2'19.806.mp4`.
  - **Export finish:**
    - Work files go to this computer's disk; only the finished video is copied to the destination (one copy to a NAS).
    - Audio is pulled out during the render.
    - Every finishing step is named, with a running time.
    - Faststart is optional.
  - **GoPro GPS clock:** checked against a real HERO6 clip.
  - **Lap times interpolated** to the line crossing; **Day tab** compares any number
    of sessions; **CMP Full Course** in the track library.
- **v1.2.0** (2026-10-31):
  - **Race Analysis:**
    - APEX exports recognized in all three naming styles, with short session names.
    - Charts on the Laps tab you pick, add and remove (1–6, any channel).
    - Track-defined sectors (VIR Full ships with two lines).
    - Lap-time and g labels on the charts; turn-name toggles.
    - Day tab: split start time, tire pressure grids with hot gain, envelope numbers and hover readout.
    - Tabs no longer held wide by other tabs; readout strip digits no longer clipped; g-g labels placed by the data.
  - **Import:** fixes a GoPro clock that reset to 2016, one shift for the whole card, filled in from GPS when there's a fix.
  - **Race Overlay:** smooth g-ball and track map; logo and text gauges; optional intro and end cards.
  - **Both apps:** separate icons and taskbar identities.
- **v1.1.0** (2026-10-30): Windows (same code as Linux, per-user installer
  built by GitHub Actions on each release, unsigned); built-in track library
  shipped with the app (VIR Full Course), local overrides, Reset to built-in,
  Export for sharing.
- **v1.0.0** (2026-10-28): first release. v0.9.9 confirmed on Cam's NVIDIA
  desktop (CachyOS, KDE Wayland) and Intel laptop.
- v0.9.9: side panel no longer clips (long filenames wrap, dropdowns don't size
  to their longest item, panel resizable, window fits the screen); Test
  decoders and Player window behind "Show advanced options"; clearer frame
  rate labels (60 recommended, Source marked slower)
- v0.9.8: native preview area is plain black before a video loads (was garbage)
- v0.9.7: native preview forced to an X11 GPU context (Wayland desktops opened a
  separate mpv window); built-in mpv is the default again (system mpv crashed
  at startup on CachyOS when mixed with the bundle's X11 libs; now opt-in via
  RACEOVERLAY_SYSTEM_MPV=1); normal quit clears the crash guard
- v0.9.6: native preview stays inside the main window on Wayland desktops
  that set QT_QPA_PLATFORM (it opened as a separate window in v0.9.5)
- v0.9.5: native-window preview (mpv draws in its own X11 window, gpu-next
  renderer, like standalone mpv) — default; old embedded mode kept in Setup.
  Aimed at striped GPU preview on NVIDIA, where standalone mpv is clean
- v0.9.4: system mpv actually loads on newer distros (its libraries were being
  resolved to the bundle's older Ubuntu copies); crash guard; reason shown
  when it falls back
- v0.9.3: uses the system's mpv for the preview when installed (fixes NVIDIA
  GPU-decode striping seen with the bundled player); "Player:" readout and a
  Setup toggle to force the built-in player
- v0.9.2: Vulkan removed from Auto/Direct decode; per-decoder test modes
- v0.9.1: version shown in the window title; About section on the Setup tab
- v0.9.1: preview decoding setting (safe copy default) + NVDEC; fixes striped
  preview on NVIDIA with the VA-API wrapper

## Open items carried past 1.0
- License decision (GPL-3.0 recommended; bundles GPL ffmpeg/mpv)
- Older-distro AppImage build if a teammate needs it (Ubuntu 22.04 / Debian 12)
- System-mpv startup crash on CachyOS (only matters if the opt-in is kept)

## Planned, not scheduled
- Start/finish line from GPS (laps for files without a lap channel; fix bad markers)
- Layout templates (save/share an overlay look, rebind by channel name)
- More gauge types: shift lights, indicator lights, rolling graph
- Corner gauge option: combined g instead of lateral-only
- Contrast: outlines/backing for colored lap-timer text in the Clean style

## Waiting on sample files
- VBOX .vbo, RaceChrono CSV v3, Harry's LapTimer, RaceBox
- Native AiM .xrk (libxrk), MoTeC .ld (check parser license)

## Later
- Flatpak / Flathub if it goes public

## Shelved
- Math channels
- GoPro GPMF auto-sync (LosslessCut strips the data)
