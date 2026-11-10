# SPDX-License-Identifier: GPL-3.0-or-later
"""Updates and rollback against a local stand-in for GitHub, and the Linux
menu entries next to an app manager's (Shelly's) entry."""
import datetime as dt
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from racecore import update  # noqa: E402
from raceoverlay import menuentry  # noqa: E402


@pytest.fixture()
def home(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("XDG_DATA_HOME", os.path.join(tmp, "share"))
        monkeypatch.setenv("XDG_CACHE_HOME", os.path.join(tmp, "cache"))
        monkeypatch.setenv("HOME", tmp)
        # Windows keeps these under %APPDATA%: point them here too
        from racecore import osutil
        monkeypatch.setattr(osutil, "data_dir", lambda: Path(tmp) / "share" / "raceoverlay")
        monkeypatch.setattr(osutil, "cache_dir", lambda: Path(tmp) / "cache" / "raceoverlay")
        yield Path(tmp)


@pytest.fixture()
def server():
    """Serves whatever is put in files {path: bytes}."""
    files = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, files
    srv.shutdown()


def _release(base, files, version, tag, body=b"", digest=True, published="2026-11-20"):
    assets = []
    for name in (f"RaceOverlay-{version}-x86_64.AppImage",
                 f"RaceOverlay-{version}-windows-setup.exe"):
        data = (name + "\n").encode() + body
        files[f"/dl/{name}"] = data
        a = {"name": name, "browser_download_url": f"{base}/dl/{name}", "size": len(data)}
        if digest:
            a["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
        assets.append(a)
    assets.append({"name": "notes.txt", "browser_download_url": "x", "size": 1})
    return {"tag_name": tag, "body": f"Notes for {version}", "published_at":
            published + "T00:00:00Z", "draft": False, "prerelease": False, "assets": assets}


def test_versions_and_release_list(server):
    base, files = server
    assert update.parse_version("V1.4") == (1, 4, 0)
    assert update.parse_version("1.10.0") > update.parse_version("1.9.9")
    assert update.parse_version("junk") == (0, 0, 0)
    data = [_release(base, files, "1.4.0", "V1.4"), _release(base, files, "1.10.0", "V1.10"),
            {"tag_name": "draft", "draft": True, "assets": []},
            {"tag_name": "V0.1", "assets": [{"name": "readme.md"}]}]
    files["/rel"] = json.dumps(data).encode()
    rels = update.releases(base + "/rel")
    assert [r.version for r in rels] == ["1.10.0", "1.4.0"]
    assert set(rels[0].assets) == {"appimage", "windows"} and rels[0].assets["windows"].sha256
    assert update.newer(rels, "1.4.0").version == "1.10.0"
    assert update.newer(rels, "1.10.0") is None
    no_win = rels[0]
    del no_win.assets["windows"]
    assert update.newer(rels, "1.4.0", "windows") is None      # no installer in that one
    with pytest.raises(update.UpdateError):
        update.releases(base + "/missing")


def test_daily_check_state(home):
    now = dt.datetime(2026, 11, 10, 9, 0)
    assert update.due(now)
    update.mark_checked(now)
    assert not update.due(now + dt.timedelta(hours=23))
    assert update.due(now + dt.timedelta(hours=24))
    update.save_state(auto=False)
    assert not update.due(now + dt.timedelta(days=3)) and not update.auto_enabled()
    update.save_state(skip="1.5.0")
    assert update.load_state()["skip"] == "1.5.0" and update.load_state()["auto"] is False


def test_download_checks_size_and_checksum(home, server):
    base, files = server
    rel = update.parse_releases([_release(base, files, "1.5.0", "V1.5", body=b"x" * 300000)])[0]
    a = rel.assets["appimage"]
    seen = []
    out = update.download(a, home / "dl" / a.name, lambda d, t: seen.append((d, t)))
    assert out.read_bytes() == files[f"/dl/{a.name}"] and seen[-1][0] == a.size
    bad = update.Asset(a.name, a.url, a.size, "0" * 64)
    with pytest.raises(update.UpdateError, match="checksum"):
        update.download(bad, home / "dl2" / a.name)
    assert not (home / "dl2" / a.name).exists()
    assert not list((home / "dl2").glob("*.part"))                  # nothing left behind
    with pytest.raises(update.UpdateError, match="Cancelled"):
        update.download(a, home / "dl3" / a.name, cancelled=lambda: True)


def test_appimage_update_and_offline_rollback(home):
    apps = home / "bin"
    apps.mkdir()
    target = apps / "RaceOverlay-1.4.0-x86_64.AppImage"
    target.write_bytes(b"version 1.4.0")
    new = apps / ".update"
    new.write_bytes(b"version 1.5.0")
    update.apply_appimage(new, target, "1.4.0")
    assert target.read_bytes() == b"version 1.5.0"              # same name, new insides
    assert os.access(target, os.X_OK)
    prev = update.previous()
    assert prev["version"] == "1.4.0" and prev["kind"] == "appimage"
    # roll back (no network involved), then forward again
    assert update.rollback_appimage(target, "1.5.0") == "1.4.0"
    assert target.read_bytes() == b"version 1.4.0"
    assert update.previous()["version"] == "1.5.0"
    assert update.rollback_appimage(target, "1.4.0") == "1.5.0"
    assert target.read_bytes() == b"version 1.5.0"
    kept = [f.name for f in update.previous_dir().iterdir()]
    assert sorted(kept) == ["RaceOverlay-1.4.0-x86_64.AppImage", "previous.json"]   # one copy


def test_windows_keeps_and_rolls_back_installers(home):
    dl = home / "dl"
    dl.mkdir()
    old = dl / "RaceOverlay-1.4.0-windows-setup.exe"
    old.write_bytes(b"setup 1.4.0")
    update.keep_windows_installer(old, "1.4.0")
    assert update.previous()["version"] == "1.4.0"
    # now on 1.5.0, its installer still in the download folder
    (dl / "RaceOverlay-1.5.0-windows-setup.exe").write_bytes(b"setup 1.5.0")
    old.unlink()
    run = update.rollback_windows(dl, "1.5.0")
    assert run.read_bytes() == b"setup 1.4.0"
    assert update.previous()["version"] == "1.5.0"               # can go forward again
    with pytest.raises(update.UpdateError):
        update.rollback_appimage(home / "x", "1.5.0")            # wrong kind kept


linux_only = pytest.mark.skipif(sys.platform == "win32", reason="Linux app menu")


@linux_only
def test_exec_lines():
    assert menuentry.exec_program('"/home/c/a b/RO.AppImage" %f') == "/home/c/a b/RO.AppImage"
    assert menuentry.exec_program("/opt/shelly/ro.AppImage %f") == "/opt/shelly/ro.AppImage"
    assert menuentry.exec_program("env QT_QPA_PLATFORM=xcb /x/RO.AppImage") == "/x/RO.AppImage"
    assert menuentry.exec_program("") is None


@linux_only
def test_analysis_entry_next_to_a_shelly_entry(home):
    app = home / "shelly" / "raceoverlay.AppImage"
    app.parent.mkdir()
    app.write_bytes(b"x")
    apps = menuentry.apps_dir()
    apps.mkdir(parents=True)
    shelly = ("[Desktop Entry]\nType=Application\nName=Race Overlay\n"
              f"Exec={app} %f\nTryExec={app}\nIcon=raceoverlay\nActions=analysis;\n\n"
              f"[Desktop Action analysis]\nName=Race Analysis\nExec={app} --analysis %F\n")
    (apps / "raceoverlay.desktop").write_text(shelly)
    # the action's Exec doesn't count as a separate Race Analysis entry
    assert not menuentry.has_entry_for(str(app), analysis=True)
    assert menuentry.ensure_analysis_entry(str(app))
    assert (apps / "raceoverlay.desktop").read_text() == shelly    # Shelly's left alone
    ra = (apps / "raceanalysis.desktop").read_text()
    assert f'Exec="{app}" --analysis %F' in ra and f"TryExec={app}" in ra
    assert not menuentry.ensure_analysis_entry(str(app))           # once is enough
    # not in the menu at all: nothing written without asking
    other = home / "other.AppImage"
    other.write_bytes(b"y")
    assert not menuentry.ensure_analysis_entry(str(other))


@linux_only
def test_our_own_entries(home):
    app = home / "RaceOverlay-1.4.0-x86_64.AppImage"
    app.write_bytes(b"x")
    menuentry.add_menu_entry(str(app))
    assert menuentry.menu_entry_target() == str(app)
    assert menuentry.has_entry_for(str(app), True) and menuentry.has_entry_for(str(app), False)
    menuentry.remove_menu_entry()
    assert menuentry.menu_entry_target() is None
