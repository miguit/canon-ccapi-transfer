"""Command line entry point: `canont serve`, `canont list`, `canont pull`."""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .ccapi import CameraError
from .library import Library


def _connect(lib: Library, host: str | None) -> None:
    print("Etsitään kameraa…", file=sys.stderr)
    lib.connect(host)
    s = lib.status()
    print(f"Yhdistetty: {s['model']} ({s['host']})", file=sys.stderr)


def cmd_serve(lib: Library, args) -> None:
    import uvicorn
    from .web import create_app

    if not args.no_connect:
        try:
            _connect(lib, args.camera)
        except CameraError as e:
            print(f"{e}\nVoit yhdistää myöhemmin selaimesta.", file=sys.stderr)
    url = f"http://{'localhost' if args.bind in ('127.0.0.1', '0.0.0.0') else args.bind}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    print(f"Käyttöliittymä: {url}", file=sys.stderr)
    uvicorn.run(create_app(lib), host=args.bind, port=args.port, log_level="warning")


def cmd_list(lib: Library, args) -> None:
    _connect(lib, args.camera)
    for f in lib.refresh():
        mark = "✓" if f["transferred"] else " "
        print(f"{mark} {f['folder']}/{f['name']}")


def cmd_pull(lib: Library, args) -> None:
    _connect(lib, args.camera)
    files = lib.refresh()
    keys = [f["key"] for f in files
            if (args.all or not f["transferred"]) and (not args.kind or f["kind"] in args.kind)]
    if not keys:
        print("Ei siirrettävää.")
        return
    job = lib.start_download(keys)
    print(f"Siirretään {len(keys)} tiedostoa → {lib.dest}")
    while job.finished is None:
        d = job.as_dict()
        print(f"\r{d['done'] + d['skipped']}/{d['total']}  {d['bytes_done'] / 1e6:.1f} MB"
              f"  {d['speed'] / 1e6:.1f} MB/s  {d['current'] or ''}\033[K", end="", flush=True)
        time.sleep(0.5)
    d = job.as_dict()
    print(f"\rValmis: {d['done']} siirretty ({d['skipped']} oli jo koneella), "
          f"{d['bytes_done'] / 1e6:.1f} MB\033[K")
    for err in job.errors:
        print("  virhe:", err, file=sys.stderr)


def cmd_diag(lib: Library, args) -> None:
    """Print raw camera responses, for checking CCAPI details on a real camera."""
    import json

    _connect(lib, args.camera)
    c = lib.client

    def show(title, fn):
        print(f"\n== {title}")
        try:
            r = fn()
            print(json.dumps(r, indent=1, ensure_ascii=False)[:1500] if not isinstance(r, str) else r)
            return r
        except CameraError as e:
            print("VIRHE:", e)

    show("/ccapi (versiot)", lambda: {k: len(v) for k, v in c._json("/ccapi").items()})
    print("käytetään:", c.version)
    show("deviceinformation", c.device_info)
    show("battery", c.battery)
    storages = show("contents", lambda: c._json(f"/ccapi/{c.version}/contents")) or {}
    for st in storages.get("url", [])[:1]:
        folders = show(f"kansiot {st}", lambda: c._json(st)) or {}
        for fo in folders.get("url", [])[-1:]:
            show(f"määrä {fo}", lambda: c._json(fo, {"type": "all", "kind": "number"}))
            files = list(c.files_in(fo))
            print(f"tiedostoja {len(files)}, viimeiset:", [f.name for f in files[-3:]])
            by_kind = {}
            for f in files:
                by_kind.setdefault(f.kind, f)
            for f in by_kind.values():
                show(f"info {f.name}", lambda: c.info(f).raw)
                for kind in ("thumbnail", "display"):
                    show(f"{kind} {f.name}", lambda: f"{len(c.thumbnail(f, kind))} tavua")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="canont", description="Kuvat Canon-kamerasta Wi-Fin (CCAPI) yli")
    p.add_argument("--camera", help="kameran IP (oletus: etsi automaattisesti)")
    p.add_argument("--camera-port", type=int, default=8080)
    p.add_argument("--dest", type=Path, help="paikallinen kohdekansio (tallentuu asetuksiin)")
    p.add_argument("--nas", type=Path, help="NAS-kohdekansio, esim. gvfs-liitos (tallentuu)")
    p.add_argument("--target", choices=["local", "nas"], help="siirtokohde (tallentuu)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="käynnistä selainkäyttöliittymä (oletus)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-connect", action="store_true")

    sub.add_parser("list", help="listaa kameran tiedostot")
    sub.add_parser("diag", help="tulosta kameran raakavastaukset vianetsintään")

    pl = sub.add_parser("pull", help="siirrä uudet tiedostot")
    pl.add_argument("--all", action="store_true", help="myös jo siirretyt")
    pl.add_argument("--kind", action="append", choices=["jpeg", "raw", "video"])

    args = p.parse_args(argv)
    try:
        lib = Library(dest=args.dest, nas=args.nas, target=args.target, port=args.camera_port)
    except CameraError as e:
        sys.exit(f"Virhe: {e}")
    cmd = args.cmd or "serve"
    if cmd == "serve" and args.cmd is None:
        args = p.parse_args([*(argv or sys.argv[1:]), "serve"])
    try:
        {"serve": cmd_serve, "list": cmd_list, "pull": cmd_pull, "diag": cmd_diag}[cmd](lib, args)
    except CameraError as e:
        sys.exit(f"Virhe: {e}")
    except KeyboardInterrupt:
        if lib.job:
            lib.job.cancel = True
        print()


if __name__ == "__main__":
    main()
