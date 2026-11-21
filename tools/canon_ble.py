"""Kokeilu: Canon-kameran herätys Bluetooth LE:n yli (ilman CCAPI:a).

Protokolla on furble-projektin (github.com/gkoh/furble, CanonEOSSmart) selvittämä
"älypuhelin"-pariutus: kamera luulee meitä Camera Connect -sovellukseksi.

    python tools/canon_ble.py scan            # etsi kamerat
    python tools/canon_ble.py gatt [OSOITE]   # tulosta kaikki palvelut ja ominaisuudet
    python tools/canon_ble.py pair [OSOITE]   # pariuta (kamera: Bluetooth → Pariuta)
    python tools/canon_ble.py wake [OSOITE] [--mode wake|playback|shoot]
    python tools/canon_ble.py watch [OSOITE]  # lokita ilmoitukset ja arvojen muutokset

Osoite muistetaan tiedostossa ~/.local/share/canont/ble.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from bleak import BleakClient, BleakScanner

STATE = Path.home() / ".local/share/canont/ble.json"
CANON_SUFFIX = "-0000-1000-0000-d8492fffa821"
CANON_COMPANY_ID = 0x01A9


def cu(short: int) -> str:
    return f"{short:08x}{CANON_SUFFIX}"


SVC_SMART = cu(0x00010000)
CHR_NAME = cu(0x00010006)   # kahva 0xf108, ilmoittaa pariutuksen tuloksen
CHR_IDEN = cu(0x0001000A)   # kahva 0xf104
CHR_MODE = cu(0x00030010)   # kahva 0xf307
SVC_REMOTE = cu(0x00050000)  # BR-E1-kaukolaukaisimen palvelu

PAIR_ACCEPT, PAIR_REJECT = 0x02, 0x03
MODES = {"playback": 0x01, "shoot": 0x02, "wake": 0x03}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**kw) -> dict:
    s = load_state() | kw
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))
    return s


def our_id() -> tuple[str, bytes]:
    """Name and 16-byte id we present to the camera; must stay the same across runs."""
    s = load_state()
    if "uuid" not in s:
        s = save_state(uuid=str(uuid.uuid4()), name="canont")
    return s.get("name", "canont"), uuid.UUID(s["uuid"]).bytes


def is_canon(dev, adv) -> bool:
    return (any(u.endswith(CANON_SUFFIX) for u in adv.service_uuids)
            or CANON_COMPANY_ID in adv.manufacturer_data
            or "canon" in (adv.local_name or dev.name or "").lower()
            or "eos" in (adv.local_name or dev.name or "").lower())


async def scan(timeout: float = 8.0, verbose: bool = True) -> list:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    cams = [(d, a) for d, a in found.values() if is_canon(d, a)]
    if verbose:
        if not cams:
            print(f"Ei Canon-laitteita ({len(found)} muuta BLE-laitetta näkyvissä).")
        for d, a in sorted(cams, key=lambda x: -x[1].rssi):
            kind = ("älylaitepalvelu" if SVC_SMART in a.service_uuids else
                    "kaukolaukaisinpalvelu" if SVC_REMOTE in a.service_uuids else "?")
            print(f"{d.address}  {a.local_name or d.name or '-':20} RSSI {a.rssi:4}  {kind}")
            for u in a.service_uuids:
                print(f"    palvelu {u}")
            for cid, data in a.manufacturer_data.items():
                print(f"    valmistajadata 0x{cid:04x}: {data.hex(' ')}")
    return cams


async def resolve(address: str | None) -> str:
    if address:
        return address
    if a := load_state().get("address"):
        return a
    print("Etsitään kameraa…")
    cams = await scan(verbose=False)
    if not cams:
        sys.exit("Kameraa ei löytynyt. Onko kameran Bluetooth päällä?")
    return max(cams, key=lambda x: x[1].rssi)[0].address


async def gatt(address: str | None) -> None:
    address = await resolve(address)
    async with BleakClient(address, timeout=20) as c:
        print(f"Yhdistetty {address}, MTU {c.mtu_size}")
        for svc in c.services:
            print(f"\n[{svc.handle:#06x}] palvelu {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                val = ""
                if "read" in ch.properties:
                    try:
                        b = await c.read_gatt_char(ch)
                        val = f" = {b.hex(' ')}" + (f"  '{b.decode()}'" if b.isascii() and b else "")
                    except Exception as e:  # noqa: BLE001 - diagnostic output only
                        val = f" (luku epäonnistui: {e})"
                print(f"  [{ch.handle:#06x}] {ch.uuid}  {','.join(ch.properties)}{val}")


async def handshake(c: BleakClient, wait_confirm: bool) -> None:
    """Identify ourselves as a smartphone; on first pairing the camera asks the user."""
    name, ident = our_id()
    result = asyncio.get_running_loop().create_future()

    def on_name(_, data: bytearray):
        print(f"  kamera ilmoitti: {data.hex(' ')}")
        if data and not result.done():
            result.set_result(data[0])

    await c.start_notify(CHR_NAME, on_name)
    steps = [
        (CHR_NAME, b"\x01" + name.encode()),
        (CHR_IDEN, b"\x03" + ident),
        (CHR_IDEN, b"\x04" + name.encode()),
        (CHR_IDEN, b"\x05\x02"),
    ]
    for chr_uuid, data in steps:
        print(f"  → {chr_uuid[4:8]}: {data.hex(' ')}")
        await c.write_gatt_char(chr_uuid, data, response=True)

    if wait_confirm:
        print("Hyväksy pariutus kameran näytöltä (60 s)…")
        try:
            r = await asyncio.wait_for(result, 60)
        except asyncio.TimeoutError:
            sys.exit("Kamera ei vastannut.")
        if r != PAIR_ACCEPT:
            sys.exit(f"Kamera hylkäsi pariutuksen (0x{r:02x}).")
    else:
        try:
            await asyncio.wait_for(result, 3)
        except asyncio.TimeoutError:
            pass

    await c.write_gatt_char(CHR_IDEN, b"\x01", response=True)


async def pair(address: str | None) -> None:
    address = await resolve(address)
    print(f"Yhdistetään {address} ja pariutetaan (BLE-bondaus)…")
    async with BleakClient(address, timeout=30, pair=True) as c:
        print("Yhdistetty.")
        await handshake(c, wait_confirm=True)
        save_state(address=address)
        print("Pariutettu. Kokeile nyt: canon_ble.py wake")


async def wake(address: str | None, mode: str, hold: float) -> None:
    address = await resolve(address)
    async with BleakClient(address, timeout=30) as c:
        print(f"Yhdistetty {address}")
        await handshake(c, wait_confirm=False)
        print(f"  → tila {mode} (0x{MODES[mode]:02x})")
        await c.write_gatt_char(CHR_MODE, bytes([MODES[mode]]), response=True)
        print(f"Lähetetty. Pidetään yhteys auki {hold:.0f} s – katso kameran näyttöä.")
        await asyncio.sleep(hold)


def fmt(b: bytes) -> str:
    text = b.rstrip(b"\0")
    shown = f"  '{text.decode()}'" if text and text.isascii() and text.decode().isprintable() else ""
    return b.hex(" ") + shown


async def watch(address: str | None, duration: float, interval: float) -> None:
    """Log every notification and every change in readable values; for finding Wi-Fi info."""
    address = await resolve(address)
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    def log(msg: str) -> None:
        print(f"{loop.time() - t0:6.1f}s  {msg}", flush=True)

    async with BleakClient(address, timeout=30) as c:
        log(f"yhdistetty {address}")
        await handshake(c, wait_confirm=False)
        readable, last = [], {}
        for svc in c.services:
            for ch in svc.characteristics:
                short = ch.uuid[:8]
                if {"notify", "indicate"} & set(ch.properties) and ch.uuid != CHR_NAME:
                    await c.start_notify(ch, lambda _, d, s=short: log(f"ilmoitus {s}: {fmt(d)}"))
                if "read" in ch.properties and ch.uuid.endswith(CANON_SUFFIX):
                    readable.append(ch)
        log("kuunnellaan – käynnistä kamera, avaa Wi-Fi, anna sen nukahtaa… (Ctrl+C lopettaa)")
        while loop.time() - t0 < duration and c.is_connected:
            for ch in readable:
                try:
                    v = bytes(await c.read_gatt_char(ch))
                except Exception as e:  # noqa: BLE001 - keep watching
                    v = f"virhe {e}".encode()
                if last.get(ch.uuid) != v:
                    log(f"{'arvo' if ch.uuid not in last else 'MUUTOS'} {ch.uuid[:8]}: {fmt(v)}")
                    last[ch.uuid] = v
            await asyncio.sleep(interval)
        log("yhteys katkesi" if not c.is_connected else "valmis")


def main() -> None:
    p = argparse.ArgumentParser(description="Canon-kameran herätys Bluetoothilla (kokeilu)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").add_argument("--timeout", type=float, default=8.0)
    for name in ("gatt", "pair", "wake", "watch"):
        sp = sub.add_parser(name)
        sp.add_argument("address", nargs="?")
        if name == "wake":
            sp.add_argument("--mode", choices=MODES, default="wake")
            sp.add_argument("--hold", type=float, default=15.0)
        if name == "watch":
            sp.add_argument("--duration", type=float, default=600.0)
            sp.add_argument("--interval", type=float, default=2.0)
    a = p.parse_args()
    if a.cmd == "scan":
        coro = scan(a.timeout)
    elif a.cmd == "gatt":
        coro = gatt(a.address)
    elif a.cmd == "pair":
        coro = pair(a.address)
    elif a.cmd == "watch":
        coro = watch(a.address, a.duration, a.interval)
    else:
        coro = wake(a.address, a.mode, a.hold)
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
