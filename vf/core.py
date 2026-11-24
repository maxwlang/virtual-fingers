"""Shared plumbing for virtual-fingers drivers.

A driver claims BLE advertisements for one vendor (`classify`) and declares the
actions it can perform. Everything vendor-neutral lives here: MAC handling, the
device cache, a single scan loop shared by all drivers, and device lookup.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

ROOT = Path(__file__).resolve().parent.parent
DEVICES_FILE = ROOT / "devices.json"

Record = dict[str, Any]
Target = BLEDevice | str  # what BleakClient connects to: a live device or a cached CBUUID


def norm_mac(s: str) -> str:
    """Normalize any 12-hex-digit MAC spelling to AA:BB:CC:DD:EE:FF."""
    h = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(h) != 12:
        raise ValueError(f"not a valid MAC address: {s!r}")
    return ":".join(h[i : i + 2] for i in range(0, 12, 2)).upper()


@dataclass(frozen=True)
class Param:
    """An integer action parameter, clamped to [min, max]."""

    name: str
    description: str
    default: int
    min: int
    max: int

    def clamp(self, value: Optional[int]) -> int:
        return max(self.min, min(int(self.default if value is None else value), self.max))


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    run: Callable[..., Awaitable[str | dict[str, Any]]]  # run(target, mac, **params)
    actuates: bool = True  # physically changes something; gated by the plugin
    params: tuple[Param, ...] = ()


@dataclass(frozen=True)
class ListTool:
    name: str
    description: str
    heading: str
    default_duration: int
    # Exposes "include records hidden by default" under this tool parameter name.
    hidden_param: Optional[tuple[str, str]] = None


@dataclass(frozen=True)
class ActTool:
    name: str
    description: str
    default_action: str


@dataclass
class Driver:
    vendor: str
    label: str
    types: tuple[str, ...]
    list_tool: ListTool
    act_tool: ActTool
    actions: dict[str, Action] = field(default_factory=dict)

    def classify(self, device: BLEDevice, adv: AdvertisementData) -> Optional[Record]:
        """Return a record if this advertisement belongs to the vendor, else None."""
        raise NotImplementedError

    def finalize(self, rec: Record) -> None:
        """Recompute derived fields after ads from one device are merged."""

    def hidden_by_default(self, rec: Record) -> bool:
        return rec.get("mac") is None

    def schema(self) -> dict[str, Any]:
        lt, at = self.list_tool, self.act_tool
        return {
            "vendor": self.vendor,
            "label": self.label,
            "types": list(self.types),
            "list_tool": {
                "name": lt.name,
                "description": lt.description,
                "heading": lt.heading,
                "default_duration": lt.default_duration,
                "hidden_param": (
                    {"name": lt.hidden_param[0], "description": lt.hidden_param[1]}
                    if lt.hidden_param else None
                ),
            },
            "act_tool": {
                "name": at.name,
                "description": at.description,
                "default_action": at.default_action,
            },
            "actions": [
                {
                    "name": a.name,
                    "description": a.description,
                    "actuates": a.actuates,
                    "params": [vars(p) for p in a.params],
                }
                for a in self.actions.values()
            ],
        }


def _classify(drivers: Sequence[Driver], device: BLEDevice,
              adv: AdvertisementData) -> Optional[tuple[int, Record]]:
    """First driver (in priority order) to claim the ad wins."""
    for i, drv in enumerate(drivers):
        rec = drv.classify(device, adv)
        if rec is not None:
            rec["vendor"] = drv.vendor
            return i, rec
    return None


async def scan(drivers: Sequence[Driver], duration: float) -> list[Record]:
    """One BLE scan shared by all drivers; records are merged per device.

    Records are keyed by CoreBluetooth UUID so a device whose ads alternate
    between formats ends up with a single record, owned by the highest-priority
    driver that recognized any of its ads.
    """
    found: dict[str, tuple[int, Record]] = {}

    def callback(device: BLEDevice, adv: AdvertisementData) -> None:
        hit = _classify(drivers, device, adv)
        if hit is None:
            return
        prio, rec = hit
        cur = found.get(rec["cb_uuid"])
        if cur is None or prio < cur[0]:
            found[rec["cb_uuid"]] = hit
            return
        if prio > cur[0]:
            return
        old = cur[1]
        for k, v in rec.items():
            if old.get(k) is None and v is not None:
                old[k] = v
        if (rec.get("rssi") or -999) > (old.get("rssi") or -999):
            old["rssi"] = rec["rssi"]

    async with BleakScanner(detection_callback=callback):
        await asyncio.sleep(duration)

    records = []
    for prio, rec in found.values():
        drivers[prio].finalize(rec)
        records.append(rec)
    return records


def load_cache() -> dict[str, Record]:
    try:
        return json.loads(DEVICES_FILE.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(records: Sequence[Record]) -> None:
    store = load_cache()
    for rec in records:
        if rec.get("mac"):
            entry = {k: v for k, v in rec.items() if k != "rssi"}
            entry["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            store[rec["mac"]] = entry
    DEVICES_FILE.write_text(json.dumps(store, indent=2, sort_keys=True))


async def locate(mac: str, drivers: Sequence[Driver], timeout: float = 15.0) -> tuple[Driver, Target]:
    """Find a device by real BT MAC (live scan, then the cache) and its driver."""
    target = norm_mac(mac)
    owner: dict[str, Driver] = {}

    def match(device: BLEDevice, adv: AdvertisementData) -> bool:
        hit = _classify(drivers, device, adv)
        if hit is None or hit[1].get("mac") != target:
            return False
        owner["driver"] = drivers[hit[0]]
        return True

    device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if device is not None:
        return owner["driver"], device

    cached = load_cache().get(target)
    if cached and cached.get("cb_uuid"):
        for drv in drivers:
            if drv.vendor == cached.get("vendor"):
                return drv, cached["cb_uuid"]
    raise RuntimeError(
        f"no device advertising MAC {target} found (and none cached). "
        "Run `vf.py list` with the device awake/nearby first."
    )
