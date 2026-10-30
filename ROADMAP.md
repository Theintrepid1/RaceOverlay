# Roadmap

## Next — planned (see the project's change list, batches C–F)
- Laps tab charts: add/remove (1–6), any channel per chart
- Track-defined sectors (turns editor, built-in tracks, thirds fallback)
- Import: fix the GoPro's reset clock for a whole card at once (GPS-suggested)
- Overlay: logo/image and text gauges; intro and end cards; separate app icons
- Day tab: compare many sessions (consecutive / baseline) across several days

## Next — collecting ideas, not started
- Interpolate the start/finish crossing (laps start up to one sample late)
- Track database stage 2: public tracks repo + "Check for track updates"
- Code signing (Azure Artifact Signing) once teammates have tried the build

## Done
- **Next release (in PR, not released yet):** Race Analysis bugs and polish.
  APEX exports recognized in all three naming styles (short session names,
  ID-only files dated from the GoPro's GPS or typed start); tabs no longer
  held wide by other tabs; long table cells capped with a tooltip; readout
  strip digits no longer clipped; g-g direction labels placed by the data;
  smooth (antialiased) g-ball and track map on the overlay; lap-time and
  g labels on the Overview and Day charts; turn-name toggles on the Laps
  tab; Day tab start time and tire pressures as separate boxes with hot
  gain; envelope numbers table and hover readout on the Day tab.
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
