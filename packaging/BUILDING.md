# Building the AppImage

Builds `dist/RaceOverlay-<version>-x86_64.AppImage`. The result runs on any
x86_64 distro with glibc at least as new as the build machine's, so build on
the oldest distro you want to support (this was set up on Ubuntu 24.04).

## What goes in it

| Part | Source | Notes |
|---|---|---|
| App + Python + Qt | PyInstaller (onedir) | Unused Qt plugins/libs pruned |
| ffmpeg, ffprobe | BtbN `ffmpeg-n8.1-latest-linux64-gpl-shared-8.1` | Loads the host's `libva.so.2` at runtime, so VAAPI uses the host's drivers |
| libmpv 0.41 | Built from source against those ffmpeg libs | OpenGL render API, X11/Wayland, VAAPI + EGL dmabuf interop, NVDEC + CUDA/GL interop, Pulse/ALSA |
| libplacebo 7.360.1 | Built static into libmpv | OpenGL only |

Host-provided (never bundled, per the AppImage excludelist): glibc, libstdc++,
GL/EGL/Mesa, libdrm, X11 core, fontconfig, freetype, harfbuzz, fribidi.
libmpv's other desktop libraries (PulseAudio, VA-API, Wayland/X11 helpers)
load from the host when present, else from bundled fallbacks.

## Steps (Ubuntu 24.04)

```bash
# tools
sudo apt install meson ninja-build pkg-config nasm patchelf libass-dev \
  libx11-dev libxext-dev libxrandr-dev libxss-dev libxpresent-dev \
  libxkbcommon-dev libwayland-dev wayland-protocols libegl-dev libgl-dev \
  libva-dev libdrm-dev libdisplay-info-dev libpulse-dev libasound2-dev \
  libvulkan-dev libfast-float-dev libffmpeg-nvenc-dev python3-jinja2 libmpv2
pip install pyinstaller pyinstaller-hooks-contrib glad2 \
  PySide6 pyqtgraph numpy pillow python-mpv
# (libmpv2 is only needed so PyInstaller can scan python-mpv; it is pruned.)

W=$PWD/build-deps; mkdir -p $W && cd $W
curl -LO https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-shared-8.1.tar.xz
curl -LO http://archive.ubuntu.com/ubuntu/pool/universe/m/mpv/mpv_0.41.0.orig.tar.gz
curl -LO http://archive.ubuntu.com/ubuntu/pool/universe/libp/libplacebo/libplacebo_7.360.1.orig.tar.gz
curl -LO https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
curl -LO https://raw.githubusercontent.com/AppImageCommunity/pkg2appimage/master/excludelist
chmod +x appimagetool-x86_64.AppImage
mkdir ffmpeg mpv placebo
tar -xf ffmpeg-n8.1-*.tar.xz -C ffmpeg --strip-components=1
tar -xf mpv_0.41.0.orig.tar.gz -C mpv --strip-components=1
tar -xf libplacebo_7.360.1.orig.tar.gz -C placebo --strip-components=1
sed -i "s#^prefix=.*#prefix=$W/ffmpeg#" ffmpeg/lib/pkgconfig/*.pc
P=$W/prefix

# libplacebo (static, OpenGL only)
meson setup placebo/build placebo --prefix=$P --libdir=lib --buildtype=release \
  -Ddefault_library=static -Dvulkan=disabled -Dglslang=disabled \
  -Dshaderc=disabled -Dlcms=disabled -Ddovi=disabled -Dlibdovi=disabled \
  -Ddemos=false -Dtests=false -Dunwind=disabled -Dxxhash=disabled -Dopengl=enabled
ninja -C placebo/build install

# libmpv
PKG_CONFIG_PATH=$W/ffmpeg/lib/pkgconfig:$P/lib/pkgconfig \
meson setup mpv/build mpv --prefix=$P --libdir=lib --buildtype=release \
  -Dauto_features=disabled -Dlibmpv=true -Dcplayer=false -Dgpl=true \
  -Dtests=false -Dmanpage-build=disabled -Dhtml-build=disabled -Dpdf-build=disabled \
  -Dgl=enabled -Dplain-gl=enabled -Degl=enabled -Degl-x11=enabled \
  -Degl-wayland=enabled -Dgl-x11=enabled -Dx11=enabled -Dwayland=enabled \
  -Dvaapi=enabled -Dvaapi-x11=enabled -Dvaapi-wayland=enabled \
  -Dvaapi-drm=enabled -Ddrm=enabled -Dcuda-hwaccel=enabled -Dcuda-interop=enabled \
  -Dpulse=enabled -Dalsa=enabled \
  -Dzlib=enabled -Diconv=enabled -Dlibavdevice=disabled
ninja -C mpv/build install

# AppImage
cd /path/to/raceoverlay
python3 packaging/build_appimage.py --ffmpeg $W/ffmpeg \
  --libmpv $W/prefix/lib/libmpv.so.2 \
  --appimagetool $W/appimagetool-x86_64.AppImage --excludelist $W/excludelist
```

## Testing switches

- `RACEOVERLAY_BUNDLED_MPV=1`: ignore the system mpv, use the built-in one.
  (Bundle setup runs in packaging/raceoverlay_boot.py, a PyInstaller runtime
  hook, so it happens before Qt loads any bundled library.)

- `RACEOVERLAY_FORCE_FALLBACK=1`: ignore the host's copies of libmpv's
  desktop libraries and use the bundled fallbacks.
- `RACEOVERLAY_MPV_LOG=v`: mpv's log on stderr (look for
  "Using EGL dmabuf interop" to confirm hardware decode can work).
- `RACEOVERLAY_SYSTEM_FFMPEG=1`: use the system ffmpeg instead of the bundled one.
- The AppImage also runs `--cli ...` (command line) and `--engine job.json`.
