"""Shelly Gen2+ driver (plugs, relays) over BLE RPC.

Advertisements: manufacturer data, company id 0x0BA9 (Allterco), a sequence of
blocks: 0x01 flags (2 bytes), 0x0A MAC (6 bytes, little-endian), 0x0B model id
(2 bytes). The advertised name also ends in the MAC, e.g.
ShellyPlugUSG4-AABBCCDDEEFF.

RPC over GATT (service "_mOS_RPC_SVC_ID_"): write the 4-byte big-endian frame
length to tx_ctl, write the JSON-RPC frame to data, poll rx_ctl for the 4-byte
response length, then read data until that many bytes arrive.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import struct
import time
from typing import Any, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from vf.core import Action, ActTool, Driver, ListTool, Param, Record, Target, norm_mac

MFR_ALLTERCO = 0x0BA9

SVC_RPC = "5f6d4f53-5f52-5043-5f53-56435f49445f"
CHAR_DATA = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
CHAR_TX_CTL = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
CHAR_RX_CTL = "5f6d4f53-5f52-5043-5f72-785f63746c5f"

# Ordered (first match wins) against the advertised model, e.g. "ShellyPlugUSG4".
TYPE_PATTERNS: list[tuple[str, str]] = [
    ("plug", r"Plug"),
    ("dimmer", r"Dimmer"),
    ("light", r"RGBW|Bulb|Duo|Vintage|Light"),
    ("sensor", r"HT|Smoke|Flood|Presence|Sense"),
    ("meter", r"EM|^Shelly(Plus)?PMMini"),
    ("input", r"I4|i4"),
    ("relay", r"^Shelly(Plus|Pro)?\d"),
]
TYPES = tuple(t for t, _ in TYPE_PATTERNS) + ("other",)

CHANNEL = Param("channel", "switch channel id (default 0)", default=0, min=0, max=15)

_ids = itertools.count(int(time.time()) % 100000)


def device_type(model: Optional[str]) -> str:
    for kind, pattern in TYPE_PATTERNS:
        if model and re.search(pattern, model):
            return kind
    return "other"


def _parse_mfr(data: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sizes = {0x01: 2, 0x0A: 6, 0x0B: 2}
    i = 0
    while i < len(data):
        kind = data[i]
        size = sizes.get(kind)
        if size is None or i + 1 + size > len(data):
            break
        block = data[i + 1 : i + 1 + size]
        if kind == 0x0A:
            out["mac"] = ":".join(f"{b:02X}" for b in reversed(block))
        elif kind == 0x0B:
            out["model_id"] = int.from_bytes(block, "little")
        i += 1 + size
    return out


async def _call(client: BleakClient, method: str, params: Optional[dict[str, Any]] = None,
                timeout: float = 10.0) -> Any:
    frame = json.dumps({"id": next(_ids), "src": "virtual-fingers", "method": method,
                        "params": params or {}}).encode()
    await client.write_gatt_char(CHAR_TX_CTL, struct.pack(">I", len(frame)), response=True)
    chunk = max(20, client.mtu_size - 3)
    for off in range(0, len(frame), chunk):
        await client.write_gatt_char(CHAR_DATA, frame[off : off + chunk], response=True)

    deadline = time.monotonic() + timeout
    length = 0
    while not length:
        length = struct.unpack(">I", bytes(await client.read_gatt_char(CHAR_RX_CTL)))[0]
        if not length:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{method}: no response in {timeout:.0f}s")
            await asyncio.sleep(0.1)

    buf = b""
    while len(buf) < length:
        buf += bytes(await client.read_gatt_char(CHAR_DATA))
    resp = json.loads(buf)
    if "error" in resp:
        err = resp["error"]
        hint = " (device has auth enabled; not supported)" if err.get("code") == 401 else ""
        raise RuntimeError(f"{method} failed: {err.get('code')} {err.get('message')}{hint}")
    return resp.get("result")


async def rpc(target: Target, calls: list[tuple[str, Optional[dict[str, Any]]]]) -> list[Any]:
    """Connect once and run a sequence of RPC calls; return their results."""
    # Restricting discovery to the RPC service avoids a CoreBluetooth descriptor
    # discovery error (CBErrorDomain 8) on the device's other characteristics.
    async with BleakClient(target, services=[SVC_RPC]) as client:
        return [await _call(client, m, p) for m, p in calls]


async def _status(target: Target, mac: str, channel: int) -> dict[str, Any]:
    info, st = await rpc(target, [("Shelly.GetDeviceInfo", None),
                                  ("Switch.GetStatus", {"id": channel})])
    return {"device": info, "switch": st}


def _switch_action(name: str, description: str) -> Action:
    async def run(target: Target, mac: str, channel: int) -> dict[str, Any]:
        if name == "toggle":
            call = ("Switch.Toggle", {"id": channel})
        else:
            call = ("Switch.Set", {"id": channel, "on": name == "on"})
        res, st = await rpc(target, [call, ("Switch.GetStatus", {"id": channel})])
        return {
            "mac": mac,
            "channel": channel,
            "action": name,
            "was_on": res.get("was_on") if isinstance(res, dict) else None,
            "output": st.get("output"),
            "apower_w": st.get("apower"),
            "voltage_v": st.get("voltage"),
        }

    return Action(name, description, run, params=(CHANNEL,))


class Shelly(Driver):
    def classify(self, device: BLEDevice, adv: AdvertisementData) -> Optional[Record]:
        name = adv.local_name or device.name or ""
        mfr = (adv.manufacturer_data or {}).get(MFR_ALLTERCO)
        if not name.lower().startswith("shelly") and mfr is None:
            return None

        parsed = _parse_mfr(bytes(mfr)) if mfr else {}
        mac = parsed.get("mac")
        m = re.fullmatch(r"(Shelly[^-]*)-([0-9A-Fa-f]{12})", name)
        if mac is None and m:
            mac = norm_mac(m.group(2))
        return {
            "mac": mac,
            "cb_uuid": device.address,
            "name": name or None,
            "model": m.group(1) if m else None,
            "type": None,
            "rssi": adv.rssi,
            "manufacturer_data": bytes(mfr).hex() if mfr else None,
        }

    def finalize(self, rec: Record) -> None:
        rec["type"] = device_type(rec.get("model"))


DRIVER = Shelly(
    vendor="shelly",
    label="Shelly",
    types=TYPES,
    list_tool=ListTool(
        name="list_shellies",
        description=(
            "Scan Bluetooth for Shelly Gen2+ devices (plugs, relays) near this Mac and "
            "enumerate them by their real Bluetooth MAC address. Returns MAC, type, model "
            "(from the advertised name, e.g. ShellyPlugUSG4), RSSI, and the macOS "
            "CoreBluetooth UUID. Pass `types` to only list some device types. The "
            "switch_shelly tool needs a MAC from this list."
        ),
        heading="Shelly devices near this Mac (by BT MAC)",
        default_duration=5,
    ),
    act_tool=ActTool(
        name="switch_shelly",
        description=(
            "Control a Shelly plug/relay over Bluetooth by its BT MAC address (get MACs "
            "from list_shellies). Actions: `toggle` (default), `on`, `off`, or `status` "
            "(read-only: device info, output state, power/voltage). `channel` selects the "
            "switch id on multi-channel devices (default 0). Locating and connecting over "
            "BLE can take up to ~30 seconds."
        ),
        default_action="toggle",
    ),
    actions={
        a.name: a
        for a in (
            _switch_action("toggle", "flip the switch"),
            _switch_action("on", "switch on"),
            _switch_action("off", "switch off"),
            Action("status", "device info + switch state/power", _status, actuates=False,
                   params=(CHANNEL,)),
        )
    },
)
