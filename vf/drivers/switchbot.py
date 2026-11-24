"""SwitchBot driver (Bot / WoHand actuation; other models are listed only).

New-generation SwitchBot ads (2023+ firmware):
  - manufacturer data, company id 0x0969 (Woan Technology): bytes 0-5 = real
    Bluetooth MAC address, remaining bytes = telemetry
  - service data on 0000fd3d: [0] = model char (bit7 clear), [2] = battery %
  - encrypted variant: service data on 0000fcd2 with byte0 = 0x40 (no MAC in ad)

Old-generation ads: service data on 00000d00, mfr 0x9009 (no MAC on macOS).

Commands follow OpenWonderLabs/SwitchBotAPI-BLE: REQ `0x57 <cmd> <payload>`
written to cba20002, RESP status byte notified on cba20003.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from vf.core import Action, ActTool, Driver, ListTool, Param, Record, Target

MFR_WOAN = 0x0969
MFR_WONDER = 0x9009
SVC_FD3D = "0000fd3d-0000-1000-8000-00805f9b34fb"
SVC_FCD2 = "0000fcd2-0000-1000-8000-00805f9b34fb"
SVC_0D00 = "00000d00-0000-1000-8000-00805f9b34fb"

CHAR_CMD = "cba20002-224d-11e6-9fb8-0002a5d5c51b"
CHAR_NOTIFY = "cba20003-224d-11e6-9fb8-0002a5d5c51b"

# model char -> (name, type)
MODELS: dict[str, tuple[str, str]] = {
    "H": ("Bot (WoHand)", "bot"),
    "T": ("Meter (WoSensorTH)", "sensor"),
    "S": ("Meter Plus", "sensor"),
    "W": ("Outdoor Meter (WoIOSensorTH)", "sensor"),
    "w": ("Outdoor Meter (WoIOSensorTH)", "sensor"),
    "c": ("Curtain (WoCurtain)", "curtain"),
    "v": ("Blind Tilt", "blind"),
    "d": ("Contact (WoContact)", "sensor"),
    "s": ("Motion (WoMotion)", "sensor"),
    "i": ("Humidifier", "humidifier"),
    "o": ("Hub", "hub"),
    "p": ("Plug", "plug"),
}
TYPES = ("bot", "sensor", "curtain", "blind", "humidifier", "hub", "plug", "other")

PRESS_COMMANDS = {
    "press": bytes([0x57, 0x01, 0x00]),  # push and pull back
    "on": bytes([0x57, 0x01, 0x01]),     # switch on (Bot must be in switch mode)
    "off": bytes([0x57, 0x01, 0x02]),    # switch off (Bot must be in switch mode)
    "back": bytes([0x57, 0x01, 0x04]),   # retract the arm
}
GET_INFO = bytes([0x57, 0x02])

STATUS = {0x01: "OK", 0x02: "ERROR", 0x03: "BUSY", 0x05: "unsupported (mode?)",
          0x06: "low battery", 0x07: "device is encrypted"}

SECONDS = Param("seconds", "hold duration in seconds when action=hold (default 10, max 60)",
                default=10, min=1, max=60)


def hold_payload(seconds: int) -> bytes:
    """Action list per official BLE API: push-stop, wait N s, back, end list."""
    return bytes([0x57, 0x01, 0x03, seconds, 0x04, 0x00])


async def _command(target: Target, payload: bytes, ack_timeout: float = 8.0) -> bytes:
    """Connect once, write a REQ, return the raw RESP notify bytes."""
    async with BleakClient(target) as client:
        box: asyncio.Queue[bytes] = asyncio.Queue()

        def handler(_sender: Any, data: bytearray) -> None:
            box.put_nowait(bytes(data))

        await client.start_notify(CHAR_NOTIFY, handler)
        await client.write_gatt_char(CHAR_CMD, payload, response=False)
        try:
            return await asyncio.wait_for(box.get(), ack_timeout)
        except TimeoutError:
            raise RuntimeError(f"sent {payload.hex()}; no notify response in {ack_timeout:.0f}s")


async def _send(target: Target, mac: str, payload: bytes, label: str, wait: float = 0) -> str:
    resp = await _command(target, payload, ack_timeout=wait + 10.0)
    status = resp[0] if resp else 0
    verdict = STATUS.get(status, f"status 0x{status:02x}")
    line = f"sent {payload.hex()} ({label}) to {mac} -> notify {resp.hex()} ({verdict})"
    if status != 0x01:
        raise RuntimeError(line)
    return line


def _press_action(name: str, description: str) -> Action:
    async def run(target: Target, mac: str) -> str:
        return await _send(target, mac, PRESS_COMMANDS[name], name)

    return Action(name, description, run)


async def _hold(target: Target, mac: str, seconds: int) -> str:
    label = f"hold {seconds}s (push-stop, wait {seconds}s, back)"
    return await _send(target, mac, hold_payload(seconds), label, wait=seconds)


async def _info(target: Target, mac: str) -> dict[str, Any]:
    """REQ 5702: battery, firmware, mode, timers."""
    d = await _command(target, GET_INFO)
    if not d or d[0] != 0x01:
        raise RuntimeError(f"info request rejected: {d.hex() if d else 'no response'}")
    # [status, bat, fw, strength, adc(2), motor(2), timers, act_mode, hold_times, svc0, svc1]
    act_mode = d[9]
    return {
        "mac": mac,
        "battery": d[1],
        "firmware": d[2] / 10,
        "push_strength": d[3],
        "timers": d[8],
        "act_mode": "switch (two-state)" if act_mode & 0xF0 else "press (one-state)",
        "inverse": bool(act_mode & 0x0F),
        "raw": d.hex(),
    }


class SwitchBot(Driver):
    def classify(self, device: BLEDevice, adv: AdvertisementData) -> Optional[Record]:
        mfr = adv.manufacturer_data or {}
        sd = adv.service_data or {}

        mac: Optional[str] = None
        raw_mfr: Optional[bytes] = None
        if MFR_WOAN in mfr and len(mfr[MFR_WOAN]) >= 6:
            raw_mfr = bytes(mfr[MFR_WOAN])
            mac = ":".join(f"{b:02X}" for b in raw_mfr[:6])
        elif MFR_WONDER in mfr:
            raw_mfr = bytes(mfr[MFR_WONDER])

        svc_uuid: Optional[str] = None
        svc: Optional[bytes] = None
        for uuid in (SVC_FD3D, SVC_FCD2, SVC_0D00):
            if uuid in sd:
                svc_uuid, svc = uuid, bytes(sd[uuid])
                break

        encrypted = svc_uuid == SVC_FCD2 and bool(svc) and (svc[0] & 0xC0) == 0x40
        if mac is None and svc is None and raw_mfr is None:
            return None

        rec: Record = {
            "mac": mac,  # None for old-gen / encrypted ads: MAC not advertised
            "cb_uuid": device.address,
            "name": adv.local_name or device.name,
            "rssi": adv.rssi,
            "model": None,
            "model_name": None,
            "type": None,
            "battery": None,
            "encrypted": encrypted,
            "service_data": svc.hex() if svc else None,
            "manufacturer_data": raw_mfr.hex() if raw_mfr else None,
        }
        if encrypted:
            rec["model_name"] = "encrypted (fcd2)"  # payload not parseable
        elif svc:
            ch = chr(svc[0] & 0x7F)
            rec["model"] = ch
            rec["model_name"] = MODELS.get(ch, (f"model {ch!r}", "other"))[0]
            if len(svc) > 2:
                rec["battery"] = svc[2] & 0x7F
        return rec

    def finalize(self, rec: Record) -> None:
        rec["type"] = MODELS.get(rec.get("model") or "", ("", "other"))[1]

    def hidden_by_default(self, rec: Record) -> bool:
        return bool(rec.get("encrypted"))


DRIVER = SwitchBot(
    vendor="switchbot",
    label="SwitchBot",
    types=TYPES,
    list_tool=ListTool(
        name="list_switchbots",
        description=(
            "Scan Bluetooth for SwitchBot devices (Bots, Meters, Curtains, ...) near this "
            "Mac and enumerate them by their real Bluetooth MAC address. Returns MAC, type, "
            "model, battery %, RSSI, advertised name, and the macOS CoreBluetooth UUID used "
            "for connecting. Encrypted devices that broadcast no MAC are hidden by default "
            "(pass include_encrypted to see them). The press_switchbot tool needs a MAC "
            "from this list."
        ),
        heading="SwitchBots near this Mac (by BT MAC)",
        default_duration=3,
        hidden_param=("include_encrypted",
                      "also list encrypted devices that broadcast no MAC (default false)"),
    ),
    act_tool=ActTool(
        name="press_switchbot",
        description=(
            "Actuate a SwitchBot Bot by its Bluetooth MAC address (get MACs from the "
            "list_switchbots tool). Actions: `press` (one press, always works), `hold` "
            "(extend the arm and keep it out for N seconds, default 10; pass `seconds`), "
            "`on`/`off` (Bot must be in switch mode), `back` (retract the arm), `info` "
            "(read-only: battery, firmware, press/switch mode). Locating and connecting "
            "over BLE can take up to ~45 seconds, and a hold takes its duration on top, "
            "so allow time before assuming failure."
        ),
        default_action="press",
    ),
    actions={
        a.name: a
        for a in (
            _press_action("press", "push and pull back"),
            _press_action("on", "switch on (Bot in switch mode)"),
            _press_action("off", "switch off (Bot in switch mode)"),
            _press_action("back", "retract the arm"),
            Action("hold", "extend the arm for N seconds, then retract", _hold,
                   params=(SECONDS,)),
            Action("info", "battery / firmware / mode", _info, actuates=False),
        )
    },
)
