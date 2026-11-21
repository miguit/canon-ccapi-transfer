import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mock_camera  # noqa: E402

from canont import library  # noqa: E402
from canont.ccapi import CCAPIClient  # noqa: E402
from canont.web import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_camera():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        mock_camera.create_app(mock_camera.make_files(), delay=0.01),
        host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return port, server


@pytest.fixture(scope="module")
def camera_port():
    port, server = _start_camera()
    yield port
    server.should_exit = True


@pytest.fixture
def own_camera_port():
    """A fresh camera for tests that change the card (delete)."""
    port, server = _start_camera()
    yield port
    server.should_exit = True


@pytest.fixture
def lib(tmp_path, monkeypatch, camera_port):
    monkeypatch.setattr(library, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(library, "CACHE_DIR", tmp_path / "cache")
    lib = library.Library(dest=tmp_path / "Kuvat", port=camera_port)
    lib.connect("127.0.0.1")
    return lib


def test_listing_pages_and_kinds(lib):
    files = lib.refresh()
    names = {f["name"] for f in files}
    assert len(files) == 130 + 26 + 1 + 10  # pages beyond 100 are fetched
    assert "IMG_0130.JPG" in names and "IMG_9999.JPG" in names
    kinds = {f["kind"] for f in files}
    assert kinds == {"jpeg", "raw", "video"}


def test_busy_camera_is_retried(lib, camera_port):
    # A second, independent client hammering the camera forces 503s.
    other = CCAPIClient("127.0.0.1", camera_port)
    lib.refresh()
    keys = list(lib.files)[:20]
    errors = []

    def spam():
        for k in keys:
            try:
                other.info(lib.files[k])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    t = threading.Thread(target=spam)
    t.start()
    for k in keys:
        assert lib.thumbnail(k)[:2] == b"\xff\xd8"
    t.join()
    assert not errors


def test_download_and_skip(lib, tmp_path):
    lib.refresh()
    keys = ["sd/100CANON/IMG_0005.JPG", "sd/100CANON/IMG_0005.CR3", "sd/100CANON/MVI_0131.MP4"]
    job = lib.start_download(keys)
    while job.finished is None:
        time.sleep(0.05)
    assert job.errors == [] and job.done == 3
    out = tmp_path / "Kuvat" / "2026-10-06"
    assert (out / "IMG_0005.JPG").read_bytes()[:2] == b"\xff\xd8"
    assert (out / "IMG_0005.CR3").stat().st_size == 2_000_000
    assert not list(tmp_path.rglob("*.part"))
    listing = {f["key"]: f for f in lib.listing()}
    assert all(listing[k]["transferred"] for k in keys)

    # Forget the bookkeeping: existing identical files are skipped, not duplicated.
    lib.transferred.data.clear()
    job = lib.start_download(keys)
    while job.finished is None:
        time.sleep(0.05)
    assert job.skipped == 3
    assert not (out / "IMG_0005_1.JPG").exists()


def test_web_api(lib):
    c = TestClient(create_app(lib))
    s = c.get("/api/status").json()
    assert s["connected"] and "mock" in s["model"]
    files = c.get("/api/files?refresh=1").json()
    key = files[0]["key"]
    r = c.get(f"/api/thumb/{key}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/preview/{key}").status_code == 200
    assert c.get("/api/thumb/sd/100CANON/NOPE.JPG").status_code == 502
    assert c.get("/").status_code == 200
    assert c.get("/static/fonts/material-symbols.woff2").status_code == 200


def _wait(job):
    while job.finished is None:
        time.sleep(0.05)
    return job


def test_local_and_nas_targets(lib, tmp_path):
    c = TestClient(create_app(lib))
    lib.refresh()
    key = "sd/100CANON/IMG_0007.JPG"

    # No NAS folder set: switching is refused, local stays selected.
    assert c.put("/api/settings", json={"target": "nas"}).status_code == 400
    assert lib.target == "local"

    nas = tmp_path / "nas" / "Photos" / "Canon"
    s = c.put("/api/settings", json={"dest_nas": str(nas), "target": "nas"}).json()
    assert s["target"] == "nas" and s["dest"] == str(nas) and s["nas_available"] is False
    with pytest.raises(library.CameraError):  # NAS not mounted
        lib.start_download([key])

    nas.parent.mkdir(parents=True)  # "mount" appears; Canon/ is created on demand
    assert lib.status()["nas_available"]
    assert _wait(lib.start_download([key])).done == 1
    assert len(list(nas.glob("*/IMG_0007.JPG"))) == 1
    assert not list((tmp_path / "Kuvat").glob("*/IMG_0007.JPG"))

    # Transferred marks are kept per target.
    marks = lambda: {f["key"]: f["transferred"] for f in c.get("/api/files").json()}
    assert marks()[key]
    c.put("/api/settings", json={"target": "local"})
    assert not marks()[key]
    assert _wait(lib.start_download([key])).done == 1
    assert len(list((tmp_path / "Kuvat").glob("*/IMG_0007.JPG"))) == 1


def test_old_single_dest_setting_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    nas = "/run/user/1/gvfs/smb-share:server=x,share=homes/u/Photos/Canon"
    (tmp_path / "data" / "settings.json").write_text(f'{{"dest": "{nas}", "layout": "%Y-%m-%d"}}')
    lib = library.Library()
    assert lib.target == "nas" and lib.settings.data["dest_nas"] == nas
    assert "dest" not in lib.settings.data and lib.settings.data["dest_local"]


def test_delete_from_camera(tmp_path, monkeypatch, own_camera_port):
    monkeypatch.setattr(library, "DATA_DIR", tmp_path / "data")
    lib = library.Library(dest=tmp_path / "Kuvat", port=own_camera_port)
    lib.connect("127.0.0.1")
    c = TestClient(create_app(lib))
    before = c.get("/api/files?refresh=1").json()
    keep, gone = "sd/100CANON/IMG_0013.JPG", "sd/100CANON/IMG_0014.JPG"  # 0013 is protected

    _wait(lib.start_download([gone]))
    assert next(f for f in c.get("/api/files").json() if f["key"] == gone)["copies"] == ["local"]

    r = c.post("/api/delete", json={"keys": [gone, keep]})
    assert r.status_code == 200 and r.json()["action"] == "delete"
    job = _wait(lib.job)
    assert job.done == 1 and job.skipped == 1 and "suojattu" in job.errors[0]

    after = {f["key"] for f in c.get("/api/files?refresh=1").json()}  # re-read from the card
    assert gone not in after and keep in after and len(after) == len(before) - 1
    assert len(list((tmp_path / "Kuvat").glob("*/IMG_0014.JPG"))) == 1  # local copy untouched


def test_live_mode_sees_new_shot(tmp_path, monkeypatch, own_camera_port):
    import requests
    monkeypatch.setattr(library, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(library.Library, "LIVE_INTERVAL", 0.05)
    lib = library.Library(dest=tmp_path / "Kuvat", port=own_camera_port)
    lib.connect("127.0.0.1")
    c = TestClient(create_app(lib))
    c.get("/api/files?refresh=1")

    r = c.get("/api/live").json()  # starts polling
    seq = r["seq"]
    while (resp := requests.post(f"http://127.0.0.1:{own_camera_port}/mock/shoot")).status_code == 503:
        time.sleep(0.01)  # the camera serves one request at a time, like the real one
    added = resp.json()["added"]
    for _ in range(100):
        r = c.get(f"/api/live?since={seq}").json()
        if r["shots"]:
            break
        time.sleep(0.05)
    assert [k.rsplit("/", 1)[1] for k in r["shots"][-1]] == added
    jpg = next(k for k in r["shots"][-1] if k.endswith(".JPG"))
    assert jpg in {f["key"] for f in c.get("/api/files").json()}  # listed without re-reading the card
    assert c.get(f"/api/preview/{jpg}").status_code == 200
    assert c.get(f"/api/live?since={r['seq']}").json()["shots"] == []


def test_exif_endpoint_uses_cached_preview(lib):
    c = TestClient(create_app(lib))
    key = c.get("/api/files?refresh=1").json()[0]["key"]
    assert c.get(f"/api/preview/{key}").status_code == 200
    assert key in lib._previews
    r = c.get(f"/api/exif/{key}")
    assert r.status_code == 200 and r.json() == {}  # mock previews carry no EXIF


def test_viewfinder_frames_settings_and_shutter(tmp_path, monkeypatch, own_camera_port):
    monkeypatch.setattr(library, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(library.Library, "LV_IDLE", 0.5)
    lib = library.Library(dest=tmp_path / "Kuvat", port=own_camera_port)
    lib.connect("127.0.0.1")
    c = TestClient(create_app(lib))
    c.get("/api/files?refresh=1")

    r = c.get("/api/lv/frame?after=0&size=small")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    seq = int(r.headers["X-Seq"])
    r2 = c.get(f"/api/lv/frame?after={seq}")
    assert r2.status_code == 200 and int(r2.headers["X-Seq"]) > seq  # a newer frame

    info = c.get("/api/lv/info").json()
    assert info["error"] is None and info["size"] == "small"
    assert len(info["histogram"]) == 4 and len(info["histogram"][0]) == 64
    assert info["settings"]["shootingmodedial"] == "av" and info["settings"]["av"] == "f5.6"

    assert c.post("/api/lv/shutter").json() == {"ok": True}
    for _ in range(100):  # the new shot arrives via camera events
        info = c.get("/api/lv/info").json()
        if info["last_shot"]:
            break
        time.sleep(0.05)
    assert info["last_shot"].endswith(".JPG") and info["last_shot"] in lib.files

    c.post("/api/lv/stop")
    lib._lv_thread.join(timeout=3)
    assert not lib._lv_thread.is_alive()
    import requests
    # live view was switched off on the camera
    while (resp := requests.get(f"http://127.0.0.1:{own_camera_port}/ccapi/ver100/shooting/liveview/flipdetail")).status_code == 503 \
            and "not started" not in resp.text:
        time.sleep(0.01)
    assert resp.status_code == 503 and "not started" in resp.text


def test_parse_flipdetail():
    from canont.ccapi import parse_flipdetail
    blk = lambda t, d: b"\xff\x00" + bytes([t]) + len(d).to_bytes(4, "big") + d + b"\xff\xff"
    jpeg, info = parse_flipdetail(blk(1, b'{"liveviewdata": {"histogram": []}}') + blk(0, b"\xff\xd8xx"))
    assert jpeg == b"\xff\xd8xx" and info == {"liveviewdata": {"histogram": []}}
    assert parse_flipdetail(b"garbage") == (None, {})
