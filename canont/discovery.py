"""Find a CCAPI camera on the local network.

Order: SSDP (UPnP) search, the default gateway (in camera access point mode
the camera usually is the gateway), and finally a scan of the local /24 for
port 8080 answering /ccapi.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

SSDP_ADDR = ("239.255.255.250", 1900)
CCAPI_ST = "urn:schemas-canon-com:service:ICPO-CameraControlAPIService:1"


def is_ccapi(host: str, port: int = 8080, timeout: float = 1.5) -> bool:
    try:
        r = requests.get(f"http://{host}:{port}/ccapi", timeout=timeout)
        return r.ok and any(k.startswith("ver") for k in r.json())
    except (requests.RequestException, ValueError):
        return False


def ssdp_search(timeout: float = 2.0) -> list[str]:
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
        'MAN: "ssdp:discover"',
        "MX: 1",
        f"ST: {CCAPI_ST}",
        "", "",
    ]).encode()
    hosts: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, SSDP_ADDR)
        while True:
            data, addr = sock.recvfrom(4096)
            text = data.decode(errors="replace")
            if "canon" not in text.lower():
                continue
            host = addr[0]
            for line in text.split("\r\n"):
                if line.lower().startswith("location:"):
                    host = urlparse(line.split(":", 1)[1].strip()).hostname or host
            if host not in hosts:
                hosts.append(host)
    except (socket.timeout, OSError):
        pass
    finally:
        sock.close()
    return hosts


def _routes() -> tuple[list[str], list[ipaddress.IPv4Network]]:
    """Default gateways and directly connected /24 networks (wlan first)."""
    gateways: list[str] = []
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = subprocess.run(["ip", "-4", "route"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return gateways, nets
    lines = sorted(out.splitlines(), key=lambda l: 0 if " dev wl" in l else 1)
    for line in lines:
        parts = line.split()
        if parts[:2] == ["default", "via"]:
            gateways.append(parts[2])
        elif "/" in parts[0] and "scope" in parts and "link" in parts:
            net = ipaddress.ip_network(parts[0], strict=False)
            if net.prefixlen >= 24 and net.is_private:
                nets.append(net)
    return gateways, nets


def discover(scan: bool = True) -> str | None:
    for host in ssdp_search():
        if is_ccapi(host):
            return host
    gateways, nets = _routes()
    for gw in gateways:
        if is_ccapi(gw, timeout=1.0):
            return gw
    if not scan:
        return None
    for net in nets:
        hosts = [str(h) for h in net.hosts()]
        with ThreadPoolExecutor(max_workers=64) as pool:
            for host, ok in zip(hosts, pool.map(lambda h: is_ccapi(h, timeout=0.7), hosts)):
                if ok:
                    return host
    return None
