"""FastAPI app: JSON API for the browser UI plus the static files."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ccapi import CameraError
from .library import Library

STATIC = Path(__file__).parent / "static"


class ConnectBody(BaseModel):
    host: str | None = None


class DownloadBody(BaseModel):
    keys: list[str]


class SettingsBody(BaseModel):
    dest_local: str | None = None
    dest_nas: str | None = None
    target: str | None = None
    layout: str | None = None


def create_app(lib: Library) -> FastAPI:
    app = FastAPI(title="canont")

    def guard(fn, *args):
        try:
            return fn(*args)
        except CameraError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/status")
    def status():
        return lib.status()

    @app.post("/api/connect")
    def connect(body: ConnectBody):
        guard(lib.connect, body.host)
        return lib.status()

    @app.get("/api/files")
    def files(refresh: bool = False):
        if refresh or not lib.files:
            return guard(lib.refresh)
        return lib.listing()

    @app.get("/api/info/{key:path}")
    def info(key: str):
        i = guard(lib.info, key)
        return {"size": i.size, "date": i.modified.isoformat() if i.modified else None,
                "rating": i.rating, "protected": i.protected}

    @app.get("/api/thumb/{key:path}")
    def thumb(key: str):
        return Response(guard(lib.thumbnail, key), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/exif/{key:path}")
    def exif(key: str):
        return guard(lib.exif, key)

    @app.get("/api/preview/{key:path}")
    def preview(key: str):
        return Response(guard(lib.preview, key), media_type="image/jpeg")

    @app.post("/api/download")
    def download(body: DownloadBody):
        return guard(lib.start_download, body.keys).as_dict()

    @app.post("/api/delete")
    def delete(body: DownloadBody):
        return guard(lib.start_delete, body.keys).as_dict()

    @app.get("/api/live")
    def live(since: int = 0):
        return guard(lib.live, since)

    @app.get("/api/lv/frame")
    def lv_frame(after: int = 0, size: str | None = None):
        seq, jpeg = guard(lib.lv_frame, after, size)
        if jpeg is None:
            return Response(status_code=204, headers={"X-Seq": str(seq)})
        return Response(jpeg, media_type="image/jpeg",
                        headers={"X-Seq": str(seq), "Cache-Control": "no-store"})

    @app.get("/api/lv/info")
    def lv_info():
        return guard(lib.lv_info)

    @app.post("/api/lv/shutter")
    def lv_shutter():
        guard(lib.lv_shutter)
        return {"ok": True}

    @app.post("/api/lv/stop")
    def lv_stop():
        lib.lv_stop()
        return {"ok": True}

    @app.get("/api/job")
    def job():
        return lib.job.as_dict() if lib.job else None

    @app.post("/api/job/cancel")
    def cancel():
        if lib.job:
            lib.job.cancel = True
        return lib.job.as_dict() if lib.job else None

    @app.put("/api/settings")
    def settings(body: SettingsBody):
        if body.dest_local:
            lib.settings.data["dest_local"] = body.dest_local
        if body.dest_nas is not None:
            lib.settings.data["dest_nas"] = body.dest_nas
            if not body.dest_nas and lib.target == "nas":
                lib.settings.data["target"] = "local"
        if body.target:
            try:
                lib.set_target(body.target)
            except CameraError as e:
                raise HTTPException(400, str(e)) from e
        if body.layout:
            lib.settings.data["layout"] = body.layout
        lib.settings.save()
        return lib.status()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
