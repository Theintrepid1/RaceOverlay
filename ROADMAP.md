# Roadmap

## Next (v0.10) — collecting ideas, not started
- (add ideas here)

## Done
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
- Windows build: Linux-only bits ported, runs from source (windows/setup.bat); still to do: GitHub Actions build, installer, signing decision
- Flatpak / Flathub if it goes public

## Shelved
- Math channels
- GoPro GPMF auto-sync (LosslessCut strips the data)
