# Roadmap

## Next (v0.10) — collecting ideas, not started
- (add ideas here)

## Done
- v0.9.3: uses the system's mpv for the preview when installed (fixes NVIDIA
  GPU-decode striping seen with the bundled player); "Player:" readout and a
  Setup toggle to force the built-in player
- v0.9.2: Vulkan removed from Auto/Direct decode; per-decoder test modes
- v0.9.1: version shown in the window title; About section on the Setup tab
- v0.9.1: preview decoding setting (safe copy default) + NVDEC; fixes striped
  preview on NVIDIA with the VA-API wrapper

## Before 1.0
- Fix GPU decode in the bundled player on NVIDIA (bundled ffmpeg 8.1 vs system)
- Real-session testing on Cam's machine (AppImage, GPU decode/encode)
- License decision (GPL-3.0 recommended; bundles GPL ffmpeg/mpv)
- Older-distro AppImage build if a teammate needs it (Ubuntu 22.04 / Debian 12)

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
- Windows build (port Linux-only bits, GitHub Actions build, installer; signing decision)
- Flatpak / Flathub if it goes public

## Shelved
- Math channels
- GoPro GPMF auto-sync (LosslessCut strips the data)
