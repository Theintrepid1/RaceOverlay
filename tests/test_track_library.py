# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in track library merged with local tracks."""
import json

import pytest

from racecore import osutil, tracks


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    builtin, data = tmp_path / "builtin", tmp_path / "data"
    builtin.mkdir()
    monkeypatch.setenv("RACEOVERLAY_BUILTIN_TRACKS", str(builtin))
    monkeypatch.setattr(osutil, "data_dir", lambda: data)
    return builtin, data / "tracks"


def _write(folder, stem, name, turns=("T1",), image=None):
    folder.mkdir(parents=True, exist_ok=True)
    d = {"name": name, "start": [36.5, -79.2],
         "turns": [{"name": t, "lat": 36.5 + i * 1e-3, "lon": -79.2} for i, t in enumerate(turns)],
         "image": image, "transform": [1, 0, 0, 0] if image else None}
    (folder / f"{stem}.json").write_text(json.dumps(d), encoding="utf-8")


def test_builtin_listed_and_found(dirs):
    builtin, _ = dirs
    _write(builtin, "vir_full", "VIR Full", ("T1", "T2"))
    (p,) = tracks.load_all()
    assert p.builtin and not p.edited and p.key == "vir_full"
    assert tracks.find(36.5, -79.2).name == "VIR Full"


def test_editing_builtin_saves_locally_and_overrides(dirs):
    builtin, local = dirs
    _write(builtin, "vir_full", "VIR Full", ("T1", "T2"))
    p = tracks.load_all()[0]
    p.turns.append(tracks.Turn("T3", 36.6, -79.2))
    p.save()
    assert (local / "vir_full.json").exists()
    assert len(json.loads((builtin / "vir_full.json").read_text())["turns"]) == 2  # untouched
    (q,) = tracks.load_all()
    assert q.edited and not q.builtin and len(q.turns) == 3


def test_reset_to_builtin(dirs):
    builtin, local = dirs
    _write(builtin, "vir_full", "VIR Full", ("T1", "T2"))
    _write(local, "vir_full", "VIR Full", ("T1", "T2", "T3"))
    (p,) = tracks.load_all()
    base = tracks.reset_to_builtin(p)
    assert base.builtin and len(base.turns) == 2
    assert not (local / "vir_full.json").exists()
    assert tracks.load_all()[0].builtin


def test_local_only_tracks_and_reset_noop(dirs):
    _, local = dirs
    _write(local, "cmp", "CMP")
    (p,) = tracks.load_all()
    assert not p.builtin and not p.edited
    assert tracks.reset_to_builtin(p) is None and (local / "cmp.json").exists()


def test_export_drops_image(dirs, tmp_path):
    _, local = dirs
    _write(local, "cmp", "CMP", image="cmp.png")
    (p,) = tracks.load_all()
    assert p.image
    d = json.loads(p.export(tmp_path / "out.json").read_text())
    assert d["image"] is None and d["transform"] is None and len(d["turns"]) == 1


def test_repo_library_files_are_valid():
    """Every committed built-in track must load and carry no image."""
    folder = tracks.builtin_dir()
    for f in sorted(folder.glob("*.json")) if folder else []:
        d = json.loads(f.read_text(encoding="utf-8"))
        tracks.TrackProfile.from_dict(d, str(f))
        assert not d.get("image"), f"{f.name}: map images stay out of the repo"
