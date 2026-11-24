"""virtual-fingers CLI: control BLE devices near this Mac by real Bluetooth MAC.

  vf.py list [--vendor V]... [--type T]... [--all] [--json] [--duration 10]
  vf.py <action> <MAC> [--vendor V]... [--seconds N] [--channel N]
  vf.py schema                        driver/tool/action registry as JSON

Actions are declared by the drivers (vf/drivers/); `vf.py -h` lists them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from vf import core
from vf.drivers import DRIVERS, by_vendor


async def cmd_list(vendors: list[str] | None, types: list[str] | None, show_all: bool,
                   duration: float, as_json: bool) -> None:
    records = await core.scan(DRIVERS, duration)
    core.save_cache(records)
    drivers = {d.vendor: d for d in by_vendor(vendors)}
    shown = sorted(
        (
            r for r in records
            if r["vendor"] in drivers
            and (show_all or not drivers[r["vendor"]].hidden_by_default(r))
            and (not types or r["type"] in types)
        ),
        key=lambda r: (r["vendor"], r["mac"] is None, r["mac"] or r["cb_uuid"]),
    )
    if as_json:
        print(json.dumps(shown, indent=2))
        return
    print(f"{'MAC':18s}  {'vendor':9s} {'type':7s} {'model':24s} {'RSSI':>4s}  {'name':28s} cb_uuid (macOS)")
    for r in shown:
        model = r.get("model_name") or r.get("model") or "?"
        print(f"{r['mac'] or '-':18s}  {r['vendor']:9s} {r['type']:7s} {model:24s} "
              f"{r['rssi']!s:>4s}  {r['name'] or '-':28s} {r['cb_uuid']}")


async def cmd_act(action: str, mac: str, vendors: list[str] | None,
                  values: dict[str, int | None]) -> None:
    mac = core.norm_mac(mac)
    driver, target = await core.locate(mac, DRIVERS)
    if vendors and driver.vendor not in vendors:
        raise RuntimeError(f"{mac} is a {driver.label} device, not {'/'.join(vendors)}")
    spec = driver.actions.get(action)
    if spec is None:
        raise RuntimeError(f"{driver.label} devices don't support {action!r} "
                           f"(supported: {', '.join(driver.actions)})")
    params = {p.name: p.clamp(values.get(p.name)) for p in spec.params}
    result = await spec.run(target, mac, **params)
    print(result if isinstance(result, str) else json.dumps(result, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    vendors = [d.vendor for d in DRIVERS]
    types = sorted({t for d in DRIVERS for t in d.types})

    pl = sub.add_parser("list", help="enumerate nearby devices by BT MAC address")
    pl.add_argument("--vendor", dest="vendors", action="append", choices=vendors)
    pl.add_argument("--type", dest="types", action="append", choices=types)
    pl.add_argument("--all", action="store_true",
                    help="include devices hidden by default (encrypted / no MAC broadcast)")
    pl.add_argument("--duration", type=float, default=10.0)
    pl.add_argument("--json", action="store_true")

    sub.add_parser("schema", help="print the driver registry as JSON")

    # One subcommand per action name across all drivers, with the union of their params.
    actions: dict[str, list[tuple[str, core.Action]]] = {}
    for d in DRIVERS:
        for a in d.actions.values():
            actions.setdefault(a.name, []).append((d.label, a))
    for name, impls in actions.items():
        helptext = "; ".join(f"{label}: {a.description}" for label, a in impls)
        pa = sub.add_parser(name, help=helptext)
        pa.add_argument("mac")
        pa.add_argument("--vendor", dest="vendors", action="append", choices=vendors)
        seen: set[str] = set()
        for _, a in impls:
            for prm in a.params:
                if prm.name not in seen:
                    seen.add(prm.name)
                    pa.add_argument(f"--{prm.name}", type=int, help=prm.description)

    args = p.parse_args()
    try:
        if args.cmd == "schema":
            print(json.dumps({"drivers": [d.schema() for d in DRIVERS]}, indent=2))
        elif args.cmd == "list":
            asyncio.run(cmd_list(args.vendors, args.types, args.all, args.duration, args.json))
        else:
            values = {k: v for k, v in vars(args).items() if k not in ("cmd", "mac", "vendors")}
            asyncio.run(cmd_act(args.cmd, args.mac, args.vendors, values))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
