"""Driver registry. To add a vendor, write a module exposing DRIVER and list it here.

Order is classification priority: when several drivers recognize ads from the
same device, the earliest one owns it. Shelly comes first because Shelly BLU
sensors also broadcast the fcd2 (BTHome) service data SwitchBot's encrypted
ads use.
"""
from __future__ import annotations

from vf.core import Driver
from vf.drivers import shelly, switchbot

DRIVERS: list[Driver] = [shelly.DRIVER, switchbot.DRIVER]


def by_vendor(vendors: list[str] | None = None) -> list[Driver]:
    if not vendors:
        return list(DRIVERS)
    return [d for d in DRIVERS if d.vendor in vendors]
