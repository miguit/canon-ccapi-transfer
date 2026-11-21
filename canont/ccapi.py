"""Minimal client for Canon's Camera Control API (CCAPI).

The camera handles one request at a time and answers 503 "Device busy" when
it is occupied, so every request goes through a lock and busy responses are
retried.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator
from urllib.parse import urlparse

import requests

RAW_EXT = {"CR3", "CR2"}
VIDEO_EXT = {"MP4", "MOV"}


class CameraError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    path: str          # e.g. /ccapi/ver100/contents/sd/100CANON/IMG_0001.JPG
    storage: str       # sd
    folder: str        # 100CANON
    name: str          # IMG_0001.JPG

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].upper() if "." in self.name else ""

    @property
    def kind(self) -> str:
        if self.ext in RAW_EXT:
            return "raw"
        if self.ext in VIDEO_EXT:
            return "video"
        return "jpeg"

    @property
    def key(self) -> str:
        return f"{self.storage}/{self.folder}/{self.name}"


@dataclass
class FileInfo:
    size: int | None = None
    modified: datetime | None = None
    rating: int | None = None
    protected: bool = False
    raw: dict = field(default_factory=dict)


def parse_file_url(url: str) -> RemoteFile:
    path = urlparse(url).path
    parts = path.rstrip("/").split("/")
    # ['', 'ccapi', 'ver100', 'contents', storage, folder, name]
    idx = parts.index("contents")
    storage, folder, name = parts[idx + 1], parts[idx + 2], parts[idx + 3]
    return RemoteFile(path=path, storage=storage, folder=folder, name=name)


def parse_flipdetail(data: bytes) -> tuple[bytes | None, dict]:
    """flipdetail is a sequence of blocks: FF 00, type (0 jpeg, 1 json), u32 length, data, FF FF."""
    jpeg, info, i = None, {}, 0
    while i + 7 <= len(data) and data[i:i + 2] == b"\xff\x00":
        kind = data[i + 2]
        n = int.from_bytes(data[i + 3:i + 7], "big")
        block = data[i + 7:i + 7 + n]
        if kind == 0:
            jpeg = block
        elif kind == 1:
            try:
                info = json.loads(block)
            except ValueError:
                pass
        i += 7 + n
        if data[i:i + 2] == b"\xff\xff":
            i += 2
    return jpeg, info


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


class CCAPIClient:
    def __init__(self, host: str, port: int = 8080, scheme: str = "http",
                 timeout: float = 10.0, busy_retries: int = 20):
        self.base = f"{scheme}://{host}:{port}"
        self.host = host
        self.timeout = timeout
        self.busy_retries = busy_retries
        self.session = requests.Session()
        self.lock = threading.RLock()
        self.version = "ver100"

    # -- low level ---------------------------------------------------------

    def _request(self, path: str, params: dict | None = None, stream: bool = False,
                 timeout: float | None = None, method: str = "GET",
                 json_body: dict | None = None) -> requests.Response:
        url = path if path.startswith("http") else self.base + path
        for attempt in range(self.busy_retries + 1):
            with self.lock:
                try:
                    resp = self.session.request(method, url, params=params, stream=stream,
                                                json=json_body, timeout=timeout or self.timeout)
                except requests.RequestException as e:
                    raise CameraError(f"Kameraan ei saada yhteyttä: {e}") from e
                if resp.status_code != 503:
                    if resp.status_code >= 400:
                        msg = resp.text[:200]
                        resp.close()
                        raise CameraError(f"HTTP {resp.status_code} {url}: {msg}")
                    if not stream:
                        resp.content  # read body while holding the lock
                    return resp
                resp.close()
            time.sleep(min(0.2 * (attempt + 1), 1.5))
        raise CameraError("Kamera on varattu (503), yritä hetken päästä uudelleen")

    def _json(self, path: str, params: dict | None = None) -> dict:
        return self._request(path, params).json()

    # -- device ------------------------------------------------------------

    def probe(self) -> dict:
        """Return the API index and pick the newest version that has contents."""
        apis = self._json("/ccapi")
        versions = sorted(k for k in apis if k.startswith("ver"))
        for ver in reversed(versions):
            if any("/contents" in (e.get("path") or e.get("url") or "") for e in apis[ver]):
                self.version = ver
                break
        return apis

    def device_info(self) -> dict:
        return self._json("/ccapi/ver100/deviceinformation")

    def battery(self) -> dict | None:
        try:
            return self._json("/ccapi/ver100/devicestatus/battery")
        except CameraError:
            return None

    # -- contents ----------------------------------------------------------

    def _urls(self, path: str, params: dict | None = None) -> list[str]:
        return self._json(path, params).get("url", [])

    def storages(self) -> list[str]:
        return self._urls(f"/ccapi/{self.version}/contents")

    def folders(self, storage_url: str) -> list[str]:
        return self._urls(storage_url)

    def files_in(self, folder_url: str) -> Iterator[RemoteFile]:
        count = self._json(folder_url, {"type": "all", "kind": "number"})
        pages = int(count.get("pagenumber", 1) or 1)
        for page in range(1, pages + 1):
            for url in self._urls(folder_url, {"type": "all", "kind": "list", "page": page}):
                yield parse_file_url(url)

    def list_all(self) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        for storage in self.storages():
            for folder in self.folders(storage):
                out.extend(self.files_in(folder))
        return out

    def info(self, f: RemoteFile) -> FileInfo:
        d = self._json(f.path, {"kind": "info"})
        return FileInfo(
            size=d.get("filesize"),
            modified=_parse_date(d.get("lastmodifieddate")),
            rating=d.get("rating") if isinstance(d.get("rating"), int) else None,
            protected=d.get("protect") == "enable",
            raw=d,
        )

    def thumbnail(self, f: RemoteFile, kind: str = "thumbnail") -> bytes:
        """kind: 'thumbnail' (~160px) or 'display' (screen-sized JPEG)."""
        if f.kind == "raw" and kind == "display":
            kind = "embedded"
        return self._request(f.path, {"kind": kind}, timeout=30).content

    def events(self) -> dict:
        """Changes since the previous call (ver100 answers immediately; {} = nothing new).
        New shots appear as "addedcontents": [file urls]."""
        return self._json(f"/ccapi/{self.version}/event/polling", None)

    # -- shooting / live view ----------------------------------------------

    def shooting_settings(self) -> dict:
        return self._json(f"/ccapi/{self.version}/shooting/settings")

    def liveview(self, size: str = "medium") -> None:
        """size: 'small' (480x320), 'medium' (960x640) or 'off'."""
        self._request(f"/ccapi/{self.version}/shooting/liveview", method="POST",
                      json_body={"liveviewsize": size, "cameradisplay": "on"})

    def liveview_frame(self) -> tuple[bytes | None, dict]:
        """One live view frame: (jpeg, liveviewdata incl. histogram)."""
        r = self._request(f"/ccapi/{self.version}/shooting/liveview/flipdetail",
                          {"kind": "both"}, timeout=10)
        return parse_flipdetail(r.content)

    def shutter(self, af: bool = True) -> None:
        """Press the shutter button fully (with autofocus)."""
        self._request(f"/ccapi/{self.version}/shooting/control/shutterbutton", method="POST",
                      json_body={"af": af}, timeout=15)

    def delete(self, f: RemoteFile) -> None:
        """Delete the file from the memory card. Cannot be undone."""
        self._request(f.path, method="DELETE", timeout=30)

    def download(self, f: RemoteFile, dest, progress: Callable[[int], None] | None = None) -> int:
        """Stream the original file into an open binary file object."""
        total = 0
        with self.lock:
            resp = self._request(f.path, {"kind": "main"}, stream=True, timeout=60)
            try:
                for chunk in resp.iter_content(256 * 1024):
                    dest.write(chunk)
                    total += len(chunk)
                    if progress:
                        progress(len(chunk))
            finally:
                resp.close()
        return total
