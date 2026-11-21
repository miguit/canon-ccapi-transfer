"""Minimal EXIF reader for the camera's preview JPEGs (no Pillow needed).

Only the handful of tags shown in the UI are read: exposure time, aperture,
ISO, focal length, exposure compensation, shooting mode, date and model.
"""
from __future__ import annotations

import struct
from fractions import Fraction

TAGS = {
    0x0110: "model",
    0x829A: "exposure_time",
    0x829D: "fnumber",
    0x8822: "program",
    0x8827: "iso",
    0x8833: "iso_ext",        # ISOSpeed, used when ISO > 65535
    0x9003: "taken",
    0x9204: "bias",
    0x920A: "focal",
    0xA434: "lens",
}
EXIF_IFD = 0x8769
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
PROGRAMS = {1: "M", 2: "P", 3: "Av", 4: "Tv", 5: "Luova", 6: "Toiminta", 7: "Muotokuva", 8: "Maisema"}


def _app1(jpeg: bytes) -> bytes | None:
    if jpeg[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan
            return None
        size = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        seg = jpeg[i + 4:i + 2 + size]
        if marker == 0xE1 and seg[:6] == b"Exif\0\0":
            return seg[6:]
        i += 2 + size
    return None


def read_raw(jpeg: bytes) -> dict:
    """Return {name: value} for the known tags, or {} if there is no EXIF."""
    tiff = _app1(jpeg)
    if not tiff or tiff[:2] not in (b"II", b"MM"):
        return {}
    e = "<" if tiff[:2] == b"II" else ">"
    out: dict = {}

    def value(typ: int, count: int, raw: bytes):
        if typ == 2:
            return raw[:count].split(b"\0", 1)[0].decode("ascii", "replace").strip()
        if typ == 3:
            return struct.unpack(e + "H", raw[:2])[0]
        if typ in (4, 9):
            return struct.unpack(e + ("I" if typ == 4 else "i"), raw[:4])[0]
        if typ in (5, 10):
            n, d = struct.unpack(e + ("II" if typ == 5 else "ii"), raw[:8])
            return Fraction(n, d) if d else None
        return None

    def ifd(off: int, depth: int = 0) -> None:
        if depth > 2 or off + 2 > len(tiff):
            return
        n = struct.unpack(e + "H", tiff[off:off + 2])[0]
        for k in range(n):
            p = off + 2 + 12 * k
            if p + 12 > len(tiff):
                return
            tag, typ, count = struct.unpack(e + "HHI", tiff[p:p + 8])
            size = TYPE_SIZE.get(typ, 0) * count
            if size > 4:
                (ptr,) = struct.unpack(e + "I", tiff[p + 8:p + 12])
                raw = tiff[ptr:ptr + size]
            else:
                raw = tiff[p + 8:p + 12]
            if tag == EXIF_IFD:
                ifd(struct.unpack(e + "I", tiff[p + 8:p + 12])[0], depth + 1)
            elif tag in TAGS and len(raw) >= min(size, 8):
                v = value(typ, count, raw)
                if v not in (None, ""):
                    out[TAGS[tag]] = v

    try:
        ifd(struct.unpack(e + "I", tiff[4:8])[0])
    except struct.error:
        pass
    return out


def _shutter(t: Fraction) -> str:
    if t >= Fraction(3, 10):
        s = float(t)
        return (f"{s:g} s" if s >= 1 else f"{s:.1f} s").replace(".", ",")
    return f"1/{round(1 / t)} s"


def _ev(b: Fraction) -> str:
    if b == 0:
        return "±0 EV"
    sign = "+" if b > 0 else "−"
    thirds = round(abs(b) * 3)
    whole, rest = divmod(thirds, 3)
    txt = (str(whole) if whole else "") + (f"{' ' if whole else ''}{rest}/3" if rest else "")
    return f"{sign}{txt} EV"


def summary(jpeg: bytes) -> dict:
    """Display-ready strings for the UI; missing values are left out."""
    r = read_raw(jpeg)
    out: dict = {}
    if isinstance(r.get("exposure_time"), Fraction) and r["exposure_time"] > 0:
        out["shutter"] = _shutter(r["exposure_time"])
    if isinstance(r.get("fnumber"), Fraction) and r["fnumber"] > 0:
        out["aperture"] = f"f/{float(r['fnumber']):g}".replace(".", ",")
    iso = r.get("iso")
    if iso == 65535 and r.get("iso_ext"):
        iso = r["iso_ext"]
    if iso:
        out["iso"] = f"ISO {iso}"
    if isinstance(r.get("focal"), Fraction) and r["focal"] > 0:
        out["focal"] = f"{float(r['focal']):g} mm".replace(".", ",")
    if isinstance(r.get("bias"), Fraction):
        out["ev"] = _ev(r["bias"])
    if r.get("program") in PROGRAMS:
        out["mode"] = PROGRAMS[r["program"]]
    if r.get("taken"):
        out["taken"] = r["taken"]  # "YYYY:MM:DD HH:MM:SS", camera clock
    if r.get("lens"):
        out["lens"] = r["lens"]
    if r.get("model"):
        out["model"] = r["model"]
    return out
