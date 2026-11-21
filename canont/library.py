"""Camera session, transfer bookkeeping, thumbnail cache and download queue."""
from __future__ import annotations

import json
import os
from collections import OrderedDict
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .ccapi import CameraError, CCAPIClient, FileInfo, RemoteFile, parse_file_url
from .discovery import discover
from . import exif

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "canont"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "canont"
TARGETS = ("local", "nas")


def default_dest() -> Path:
    try:
        pics = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pics = ""
    base = Path(pics) if pics and Path(pics) != Path.home() else Path.home() / "Pictures"
    return base / "Canon"


class JsonStore:
    def __init__(self, path: Path, default):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = default

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
            tmp.replace(self.path)


@dataclass
class Job:
    keys: list[str]
    done: int = 0
    skipped: int = 0
    bytes_done: int = 0
    current: str | None = None
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: bool = False
    target: str = "local"
    dest: str = ""
    action: str = "download"  # or "delete"

    def as_dict(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started
        return {
            "action": self.action, "target": self.target, "dest": self.dest,
            "total": len(self.keys), "done": self.done, "skipped": self.skipped,
            "bytes_done": self.bytes_done, "current": self.current,
            "errors": self.errors[-20:], "running": self.finished is None,
            "cancelled": self.cancel,
            "speed": self.bytes_done / elapsed if elapsed > 0 else 0,
        }


class Library:
    def __init__(self, dest: Path | None = None, layout: str = "%Y-%m-%d",
                 port: int = 8080, nas: Path | None = None, target: str | None = None):
        self.settings = JsonStore(DATA_DIR / "settings.json", {})
        self.transferred = JsonStore(DATA_DIR / "transferred.json", {})
        data = self.settings.data
        if "dest_local" not in data:  # migrate the old single "dest" setting
            old = data.pop("dest", None)
            if old and "/gvfs/" in old:
                data["dest_nas"], data["target"] = old, "nas"
            elif old:
                data["dest_local"] = old
        if dest:
            data["dest_local"] = str(dest)
        if nas:
            data["dest_nas"] = str(nas)
        data.setdefault("dest_local", str(default_dest()))
        data.setdefault("dest_nas", "")
        data.setdefault("layout", layout)
        if target:
            self.set_target(target)
        data.setdefault("target", "local")
        self.settings.save()
        self.port = port
        self.client: CCAPIClient | None = None
        self.device: dict = {}
        self.files: dict[str, RemoteFile] = {}
        self.infos: dict[str, FileInfo] = {}
        self.job: Job | None = None
        self.error: str | None = None
        self._connect_lock = threading.Lock()
        # live mode: camera events are polled while a browser keeps asking
        self.live_events: list[tuple[int, list[str]]] = []  # (seq, keys of one shot)
        self.live_seq = 0
        self._live_until = 0.0
        self._live_thread: threading.Thread | None = None
        self._live_lock = threading.Lock()
        self._previews: OrderedDict[str, bytes] = OrderedDict()  # recent previews, for EXIF
        # viewfinder (camera live view) and the camera's current shooting settings
        self.shooting: dict = {}
        self._lv_cond = threading.Condition()
        self._lv_until = 0.0
        self._lv_size = "medium"
        self._lv_seq = 0
        self._lv_jpeg: bytes | None = None
        self._lv_data: dict = {}
        self._lv_error: str | None = None
        self._lv_times: list[float] = []
        self._lv_thread: threading.Thread | None = None

    # -- connection --------------------------------------------------------

    @property
    def target(self) -> str:
        return self.settings.data.get("target", "local")

    def set_target(self, target: str) -> None:
        if target not in TARGETS:
            raise CameraError(f"Tuntematon kohde: {target}")
        if target == "nas" and not self.settings.data.get("dest_nas"):
            raise CameraError("NAS-kansiota ei ole asetettu (Asetukset → NAS-kansio)")
        self.settings.data["target"] = target

    def dest_for(self, target: str) -> Path:
        return Path(self.settings.data[f"dest_{target}"]).expanduser()

    @property
    def dest(self) -> Path:
        return self.dest_for(self.target)

    def nas_available(self) -> bool:
        """The NAS folder (or its parent, if not created yet) is reachable."""
        if not self.settings.data.get("dest_nas"):
            return False
        d = self.dest_for("nas")
        try:
            return d.is_dir() or d.parent.is_dir()
        except OSError:
            return False

    def history(self, target: str | None = None) -> dict:
        target = target or self.target
        key = self.serial if target == "local" else f"{self.serial}@{target}"
        return self.transferred.data.setdefault(key, {})

    @property
    def serial(self) -> str:
        return self.device.get("serialnumber") or self.device.get("productname") or "camera"

    def connect(self, host: str | None = None) -> None:
        with self._connect_lock:
            host = host or self.settings.data.get("last_host")
            client = None
            if host:
                c = CCAPIClient(host, self.port, timeout=4)
                try:
                    c.probe()
                    client = c
                except CameraError:
                    client = None
            if client is None:
                found = discover()
                if not found:
                    self.client = None
                    self.error = "Kameraa ei löytynyt. Onko kameran Wi-Fi päällä ja kone kameran verkossa?"
                    raise CameraError(self.error)
                client = CCAPIClient(found, self.port)
                client.probe()
            client.timeout = 10
            self.client = client
            self.device = client.device_info()
            self.settings.data["last_host"] = client.host
            self.settings.save()
            self.files, self.infos, self.error = {}, {}, None

    def require(self) -> CCAPIClient:
        if not self.client:
            raise CameraError("Ei yhteyttä kameraan")
        return self.client

    def status(self) -> dict:
        battery = self.client.battery() if self.client and not self.busy else None
        return {
            "connected": self.client is not None,
            "host": self.client.host if self.client else self.settings.data.get("last_host"),
            "model": self.device.get("productname"),
            "firmware": self.device.get("firmwareversion"),
            "battery": battery,
            "dest": str(self.dest),
            "target": self.target,
            "dest_local": self.settings.data["dest_local"],
            "dest_nas": self.settings.data["dest_nas"],
            "nas_available": self.nas_available(),
            "layout": self.settings.data["layout"],
            "error": self.error,
        }

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.finished is None

    # -- listing -----------------------------------------------------------

    def refresh(self) -> list[dict]:
        files = self.require().list_all()
        self.files = {f.key: f for f in files}
        return self.listing()

    def listing(self) -> list[dict]:
        done = self.history()
        hist = {t: self.history(t) for t in TARGETS}
        out = []
        for f in self.files.values():
            info = self.infos.get(f.key)
            rec = done.get(f.key)

            def ok(r):
                return bool(r and (not info or r.get("size") == info.size))
            out.append({
                "key": f.key, "name": f.name, "folder": f.folder, "kind": f.kind,
                "ext": f.ext,
                "size": info.size if info else None,
                "date": info.modified.isoformat() if info and info.modified else None,
                "transferred": ok(rec),
                "copies": [t for t in TARGETS if ok(hist[t].get(f.key))],
            })
        out.sort(key=lambda d: (d["folder"], d["name"]), reverse=True)
        return out

    def file(self, key: str) -> RemoteFile:
        try:
            return self.files[key]
        except KeyError:
            raise CameraError(f"Tuntematon tiedosto {key}") from None

    def info(self, key: str) -> FileInfo:
        if key not in self.infos:
            self.infos[key] = self.require().info(self.file(key))
        return self.infos[key]

    def thumbnail(self, key: str) -> bytes:
        f = self.file(key)
        info = self.info(key)
        path = CACHE_DIR / "thumbs" / self.serial / f.folder / f"{f.name}-{info.size}.jpg"
        if path.exists():
            return path.read_bytes()
        data = self.require().thumbnail(f)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    PREVIEW_CACHE = 16

    def preview(self, key: str) -> bytes:
        if key in self._previews:
            self._previews.move_to_end(key)
            return self._previews[key]
        data = self.require().thumbnail(self.file(key), kind="display")
        self._previews[key] = data
        while len(self._previews) > self.PREVIEW_CACHE:
            self._previews.popitem(last=False)
        return data

    def exif(self, key: str) -> dict:
        """Exposure info from the preview JPEG (cached, so normally no extra camera request)."""
        f = self.file(key)
        if f.kind == "video":
            return {}
        return exif.summary(self.preview(key))

    # -- downloading -------------------------------------------------------

    def target_path(self, f: RemoteFile, info: FileInfo, base: Path | None = None) -> Path:
        when = info.modified or datetime.now()
        if when.tzinfo:
            when = when.astimezone()
        return (base or self.dest) / when.strftime(self.settings.data["layout"]) / f.name

    def start_download(self, keys: list[str]) -> Job:
        if self.busy:
            raise CameraError("Siirto on jo käynnissä")
        self.require()
        target = self.target
        if target == "nas" and not self.nas_available():
            raise CameraError(f"NAS ei ole yhdistetty: {self.dest}. "
                              "Avaa yhteys NAS:iin tiedostonhallinnassa.")
        self.job = Job(keys=[k for k in keys if k in self.files],
                       target=target, dest=str(self.dest))
        threading.Thread(target=self._run, args=(self.job,), daemon=True).start()
        return self.job

    def start_delete(self, keys: list[str]) -> Job:
        """Delete files from the camera's card in the background."""
        if self.busy:
            raise CameraError("Toinen toiminto on jo käynnissä")
        self.require()
        self.job = Job(keys=[k for k in keys if k in self.files], action="delete")
        threading.Thread(target=self._run_delete, args=(self.job,), daemon=True).start()
        return self.job

    def _run_delete(self, job: Job) -> None:
        client = self.require()
        for key in job.keys:
            if job.cancel:
                break
            f = self.files[key]
            job.current = f.name
            try:
                if self.info(key).protected:
                    job.skipped += 1
                    job.errors.append(f"{f.name}: suojattu kamerassa, ei poistettu")
                    continue
                client.delete(f)
                self.files.pop(key, None)
                self.infos.pop(key, None)
                self._previews.pop(key, None)
                job.done += 1
            except CameraError as e:
                job.errors.append(f"{f.name}: {e}")
        job.current = None
        job.finished = time.time()

    # -- live mode ---------------------------------------------------------

    LIVE_IDLE = 10.0     # stop polling this many seconds after the last browser request
    LIVE_INTERVAL = 1.0

    def live(self, since: int) -> dict:
        """Called by the browser about once a second while live mode is on."""
        self.require()
        with self._live_lock:
            self._live_until = time.time() + self.LIVE_IDLE
            if not (self._live_thread and self._live_thread.is_alive()):
                self._live_thread = threading.Thread(target=self._live_loop, daemon=True)
                self._live_thread.start()
            shots = [keys for seq, keys in self.live_events if seq > since]
        return {"seq": self.live_seq, "shots": shots, "busy": self.busy}

    def _live_loop(self) -> None:
        while time.time() < self._live_until and self.client:
            if not self.busy:  # events queue up in the camera meanwhile
                try:
                    self._apply_events(self.client.events())
                except CameraError:
                    pass
            time.sleep(self.LIVE_INTERVAL)

    def _apply_events(self, ev: dict) -> None:
        for k, v in ev.items():
            if isinstance(v, dict) and "value" in v:
                self.shooting[k] = v["value"]
        if isinstance(ev.get("recordable"), dict):
            self.shooting["recordableshots"] = ev["recordable"].get("recordableshots")
        added = []
        for url in ev.get("addedcontents") or []:
            try:
                f = parse_file_url(url)
            except (ValueError, IndexError):
                continue
            self.files[f.key] = f
            added.append(f.key)
        for url in ev.get("deletedcontents") or []:
            try:
                key = parse_file_url(url).key
            except (ValueError, IndexError):
                continue
            self.files.pop(key, None)
            self.infos.pop(key, None)
        if added:
            with self._live_lock:
                self.live_seq += 1
                self.live_events.append((self.live_seq, added))
                del self.live_events[:-50]

    # -- viewfinder (live view) ---------------------------------------------

    LV_IDLE = 5.0  # live view is switched off this long after the browser stops asking

    def lv_touch(self, size: str | None = None) -> None:
        self.require()
        if size in ("small", "medium"):
            self._lv_size = size
        with self._lv_cond:
            self._lv_until = time.time() + self.LV_IDLE
            if not (self._lv_thread and self._lv_thread.is_alive()):
                self._lv_error = None
                self._lv_thread = threading.Thread(target=self._lv_loop, daemon=True)
                self._lv_thread.start()

    def lv_stop(self) -> None:
        self._lv_until = 0.0

    def lv_frame(self, after: int, size: str | None = None, wait: float = 2.0) -> tuple[int, bytes | None]:
        """Wait for a frame newer than `after`; returns (seq, jpeg or None)."""
        self.lv_touch(size)
        with self._lv_cond:
            self._lv_cond.wait_for(lambda: self._lv_seq > after or self._lv_error, timeout=wait)
            return self._lv_seq, (self._lv_jpeg if self._lv_seq > after else None)

    def lv_info(self) -> dict:
        self.lv_touch()
        data = self._lv_data.get("liveviewdata") or {}
        hist = []
        for ch in data.get("histogram") or []:  # 256 levels -> 64 bins
            hist.append([sum(ch[i:i + 4]) for i in range(0, len(ch), 4)])
        last = None
        if self.live_events:
            keys = self.live_events[-1][1]
            last = next((k for k in keys if k.upper().endswith((".JPG", ".JPEG"))), keys[0])
        t = self._lv_times
        fps = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0
        return {"seq": self._lv_seq, "size": self._lv_size, "fps": round(fps, 1),
                "error": self._lv_error, "busy": self.busy, "histogram": hist,
                "angle": self._lv_data.get("angleinformation"),
                "settings": self.shooting, "last_shot": last, "live_seq": self.live_seq}

    def lv_shutter(self) -> None:
        if self.busy:
            raise CameraError("Siirto tai poisto on käynnissä")
        self.require().shutter(af=True)

    def _lv_loop(self) -> None:
        client = self.client
        if client is None:
            return
        size = None
        events_at = 0.0
        try:
            try:
                self.shooting.update({k: v.get("value") for k, v in client.shooting_settings().items()
                                      if isinstance(v, dict) and "value" in v})
            except CameraError:
                pass
            while time.time() < self._lv_until and self.client is client:
                if self.busy:  # don't slow down a transfer
                    time.sleep(0.3)
                    continue
                try:
                    if size != self._lv_size:
                        client.liveview(self._lv_size)
                        size = self._lv_size
                        time.sleep(0.3)
                    jpeg, info = client.liveview_frame()
                except CameraError as e:
                    with self._lv_cond:
                        self._lv_error = str(e)
                        self._lv_cond.notify_all()
                    size = None  # try switching live view on again
                    time.sleep(1.0)
                    continue
                now = time.time()
                if jpeg:
                    with self._lv_cond:
                        self._lv_jpeg, self._lv_data, self._lv_error = jpeg, info, None
                        self._lv_seq += 1
                        self._lv_times = (self._lv_times + [now])[-20:]
                        self._lv_cond.notify_all()
                if now - events_at > 1.0:  # settings changes and new shots
                    events_at = now
                    try:
                        self._apply_events(client.events())
                    except CameraError:
                        pass
        finally:
            try:
                client.liveview("off")
            except CameraError:
                pass
            with self._lv_cond:
                self._lv_jpeg, self._lv_times = None, []
                self._lv_cond.notify_all()

    def _run(self, job: Job) -> None:
        client = self.require()
        done = self.history(job.target)
        base = Path(job.dest)
        for key in job.keys:
            if job.cancel:
                break
            f = self.files[key]
            job.current = f.name
            part: Path | None = None
            try:
                info = self.info(key)
                target = self.target_path(f, info, base)
                if target.exists() and info.size and target.stat().st_size == info.size:
                    job.skipped += 1
                else:
                    target = _free_name(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part = target.with_name(target.name + ".part")

                    def progress(n: int) -> None:
                        job.bytes_done += n

                    with open(part, "wb") as fh:
                        client.download(f, fh, progress)
                    if info.modified:
                        ts = info.modified.timestamp()
                        os.utime(part, (ts, ts))
                    part.replace(target)
                done[key] = {"size": info.size, "dest": str(target),
                             "at": datetime.now().isoformat(timespec="seconds")}
                self.transferred.save()
                job.done += 1
            except (CameraError, OSError) as e:
                job.errors.append(f"{f.name}: {e}")
                if part and part.exists():
                    part.unlink()
        job.current = None
        job.finished = time.time()


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while (cand := path.with_name(f"{stem}_{i}{suffix}")).exists():
        i += 1
    return cand
