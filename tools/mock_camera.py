"""Fake CCAPI camera for development: `python tools/mock_camera.py [--port 8080]`.

Serves generated JPEG/CR3/MP4 files, pages listings by 100 like the real
camera and answers 503 to concurrent requests.
"""
from __future__ import annotations

import argparse
import json
import colorsys
import io
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw

PAGE = 100
PROTECTED = {"IMG_0013.JPG"}  # protect=enable, refuses DELETE


def make_files() -> dict[str, list[str]]:
    a = []
    for n in range(1, 131):
        a.append(f"IMG_{n:04d}.JPG")
        if n % 5 == 0:
            a.append(f"IMG_{n:04d}.CR3")
    a.append("MVI_0131.MP4")
    b = [f"IMG_{n:04d}.JPG" for n in range(9990, 10000)]
    return {"100CANON": a, "101CANON": b}


def jpeg(name: str, size: tuple[int, int]) -> bytes:
    h = (hash(name) % 360) / 360
    r, g, bl = (int(c * 255) for c in colorsys.hls_to_rgb(h, 0.55, 0.5))
    img = Image.new("RGB", size, (r, g, bl))
    d = ImageDraw.Draw(img)
    w, hgt = size
    d.ellipse((w * .3, hgt * .2, w * .7, hgt * .8), fill=(255 - r, 255 - g, 255 - bl))
    d.text((8, 8), name, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def create_app(files: dict[str, list[str]], delay: float = 0.0) -> FastAPI:
    app = FastAPI()
    busy = threading.Lock()
    base = "/ccapi/ver100/contents/sd"
    main_cache: dict[str, bytes] = {}

    @app.middleware("http")
    async def single_client(request: Request, call_next):
        if not busy.acquire(blocking=False):
            return JSONResponse({"message": "Device busy"}, status_code=503)
        try:
            if delay:
                time.sleep(delay)
            return await call_next(request)
        finally:
            busy.release()

    def url(req: Request, path: str) -> str:
        return f"http://{req.url.netloc}{path}"

    def main_data(folder: str, name: str) -> bytes:
        key = f"{folder}/{name}"
        if key not in main_cache:
            if name.endswith(".JPG"):
                main_cache[key] = jpeg(name, (1200, 800))
            else:
                main_cache[key] = (name.encode() * 300000)[:2_000_000 if name.endswith("CR3") else 3_000_000]
        return main_cache[key]

    @app.get("/ccapi")
    def index():
        return {"ver100": [{"method": "GET", "path": "/ccapi/ver100/contents"},
                           {"method": "GET", "path": "/ccapi/ver100/deviceinformation"}]}

    @app.get("/ccapi/ver100/deviceinformation")
    def devinfo():
        return {"manufacturer": "Canon Inc.", "productname": "Canon EOS M50m2 (mock)",
                "serialnumber": "MOCK0001", "firmwareversion": "1.0.2"}

    @app.get("/ccapi/ver100/devicestatus/battery")
    def battery():
        return {"name": "LP-E12", "kind": "battery", "level": "full", "quality": "good"}

    @app.get("/ccapi/ver100/contents")
    def storages(req: Request):
        return {"url": [url(req, base)]}

    @app.get(base)
    def folders(req: Request):
        return {"url": [url(req, f"{base}/{f}") for f in files]}

    @app.get(base + "/{folder}")
    def listing(req: Request, folder: str, kind: str = "list", page: int = 1, type: str = "all"):
        if folder not in files:
            raise HTTPException(404)
        names = files[folder]
        if kind == "number":
            return {"contentsnumber": len(names), "pagenumber": (len(names) + PAGE - 1) // PAGE}
        chunk = names[(page - 1) * PAGE: page * PAGE]
        return {"url": [url(req, f"{base}/{folder}/{n}") for n in chunk]}

    events: list[str] = []

    @app.get("/ccapi/ver100/event/polling")
    def polling(req: Request):
        added = [url(req, f"{base}/{p}") for p in events]
        events.clear()
        return {"addedcontents": added} if added else {}

    @app.post("/mock/shoot")
    def shoot():
        """Simulate pressing the shutter: adds IMG_nnnn.CR3 + .JPG to the first folder."""
        folder = list(files)[0]
        n = 1 + max(int(x[4:8]) for x in files[folder] if x[4:8].isdigit())
        names = [f"IMG_{n:04d}.CR3", f"IMG_{n:04d}.JPG"]
        files[folder].extend(names)
        events.extend(f"{folder}/{x}" for x in names)
        return {"added": names}

    # -- shooting / live view --
    lv = {"size": "off", "n": 0}
    settings = {"shootingmodedial": "av", "av": "f5.6", "tv": "1/125", "iso": "auto",
                "exposure": "+0.0", "wb": "auto", "afoperation": "oneshot", "drive": "single",
                "stillimagequality": {"raw": "raw", "jpeg": "large_fine"}}

    @app.get("/ccapi/ver100/shooting/settings")
    def shooting_settings():
        return {k: {"value": v} for k, v in settings.items()}

    @app.post("/ccapi/ver100/shooting/liveview")
    async def liveview(req: Request):
        lv["size"] = (await req.json()).get("liveviewsize", "off")
        return {}

    @app.get("/ccapi/ver100/shooting/liveview/flipdetail")
    def flipdetail(kind: str = "both"):
        if lv["size"] == "off":
            raise HTTPException(503, "Live view not started")
        lv["n"] += 1
        info = json.dumps({"liveviewdata": {"histogram": [[(i * lv["n"]) % 97 for i in range(256)]] * 4},
                           "angleinformation": {"cameraposture": 0, "rolling": 0, "pitching": 0}}).encode()
        img = jpeg(f"LV {lv['n']}", (480, 320) if lv["size"] == "small" else (960, 640))
        block = lambda t, d: b"\xff\x00" + bytes([t]) + len(d).to_bytes(4, "big") + d + b"\xff\xff"
        return Response(block(1, info) + block(0, img), media_type="application/octet-stream")

    @app.post("/ccapi/ver100/shooting/control/shutterbutton")
    def shutterbutton():
        shoot()
        return {}

    @app.delete(base + "/{folder}/{name}")
    def remove(folder: str, name: str):
        if name not in files.get(folder, []):
            raise HTTPException(404)
        if name in PROTECTED:
            raise HTTPException(400, "Protected")
        files[folder].remove(name)
        return {}

    @app.get(base + "/{folder}/{name}")
    def content(folder: str, name: str, kind: str = "main"):
        if name not in files.get(folder, []):
            raise HTTPException(404)
        if kind == "info":
            n = int(name[4:8])
            return {"filesize": len(main_data(folder, name)),
                    "protect": "enable" if name in PROTECTED else "disable",
                    "archive": "disable", "rotate": 0, "rating": 0,
                    "lastmodifieddate": f"Tue, {1 + n % 28:02d} Oct 2026 10:{n % 60:02d}:00 GMT"}
        if kind in ("thumbnail", "display", "embedded"):
            if name.endswith(".MP4") and kind != "thumbnail":
                raise HTTPException(400, "Invalid parameter")
            return Response(jpeg(name, (160, 120) if kind == "thumbnail" else (1440, 960)),
                            media_type="image/jpeg")
        return Response(main_data(folder, name), media_type="application/octet-stream")

    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--delay", type=float, default=0.05, help="per-request latency")
    a = p.parse_args()
    uvicorn.run(create_app(make_files(), a.delay), host=a.host, port=a.port, log_level="warning")
