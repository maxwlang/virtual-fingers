# virtual-fingers

Control Bluetooth LE smart-home devices directly from a Mac, without a hub,
cloud account or Wi-Fi, and expose them as [OpenCode](https://opencode.ai) tools
so an agent can press buttons and flip plugs. Built on
[bleak](https://github.com/hbldh/bleak) (CoreBluetooth backend).

Supported vendors:

| Vendor | Devices | Actions |
|---|---|---|
| SwitchBot | Bot (WoHand); other models are listed only | press, hold, on, off, back, info |
| Shelly | Gen2+ plugs and relays with BLE RPC | toggle, on, off, status |

## Setup

Requires macOS, Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/maxwlang/virtual-fingers
cd virtual-fingers
uv sync
```

The first scan triggers the macOS Bluetooth permission prompt for your terminal
(or for OpenCode, when used through the plugin).

## CLI

```sh
uv run vf.py list                                  # every supported device nearby
uv run vf.py list --vendor shelly --type plug      # filter (both flags repeatable)
uv run vf.py list --all --json                     # include encrypted / MAC-less ads
uv run vf.py press <MAC>                           # SwitchBot Bot
uv run vf.py hold <MAC> --seconds 5
uv run vf.py toggle <MAC>                          # Shelly plug / relay
uv run vf.py status <MAC> --channel 1
uv run vf.py schema                                # driver registry as JSON
```

Actions find the device by MAC with a short live scan and fall back to the
`devices.json` cache that `list` writes (MAC to macOS CBUUID). Run `list` once
with the device nearby before acting on it.

## OpenCode plugin

Add the plugin directory to the `plugins` list in your OpenCode config:

```jsonc
"plugins": ["/path/to/virtual-fingers/opencode-plugin"]
```

Each driver contributes two tools:

| Tool | Purpose |
|---|---|
| `list_switchbots` | scan for SwitchBots (`duration`, `types`, `include_encrypted`) |
| `press_switchbot` | `{ mac, action?, seconds? }` |
| `list_shellies` | scan for Shellys (`duration`, `types`) |
| `switch_shelly` | `{ mac, action?, channel? }` |

Actions that physically change something ask for permission the first time per
device MAC; read-only actions (`info`, `status`) don't. The plugin runs
`.venv/bin/python` from the repo; set `VIRTUAL_FINGERS_PYTHON` to override.

## Adding a vendor

1. Create `vf/drivers/<vendor>.py` with a `Driver` subclass implementing
   `classify(device, adv)`, which returns a record (`mac`, `cb_uuid`, `name`,
   `model`, `type`, `rssi`, ...) for ads it recognizes or `None` otherwise.
   Optionally implement `finalize` and `hidden_by_default`.
2. Declare its actions: `Action(name, description, run, actuates=..., params=(Param(...),))`,
   where `run(target, mac, **params)` connects with `BleakClient(target)`.
3. Instantiate it as `DRIVER` with its `ListTool` / `ActTool` metadata and add it
   to `DRIVERS` in `vf/drivers/__init__.py`. Order there is classification
   priority when two drivers recognize the same device.

The CLI subcommands and the OpenCode tools are generated from the registry, so
nothing else needs to change.

## How MAC addressing works on macOS

CoreBluetooth never exposes real Bluetooth MACs, only per-Mac random UUIDs.
Both vendors embed the real MAC in their advertisements, which is what the
drivers key on:

- **SwitchBot** (2023+ firmware): manufacturer data, company ID `0x0969`, bytes
  0-5. Service data on `fd3d` carries the model char (byte 0) and battery %
  (byte 2). Encrypted (`fcd2`) and old-generation (`0d00`) ads carry no MAC and
  are hidden by default.
- **Shelly**: manufacturer data, company ID `0x0BA9`, as blocks: `0x01` flags
  (2 B), `0x0A` MAC (6 B, little-endian), `0x0B` model id (2 B). The advertised
  name (`ShellyPlugUSG4-AABBCCDDEEFF`) also ends in the MAC.

## Protocol notes

- **SwitchBot** ([SwitchBotAPI-BLE](https://github.com/OpenWonderLabs/SwitchBotAPI-BLE)):
  write `0x57 <cmd> <payload>` to `cba20002`, read the status byte notified on
  `cba20003` (`01` OK, `02` error, `03` busy, `05` unsupported in this mode, `07`
  encrypted). Hold uses the action list `5701 03 <sec> 04 00`. `5702` returns
  battery, firmware and press/switch mode.
- **Shelly** BLE RPC, service `5f6d4f53-5f52-5043-5f53-56435f49445f`: write the
  frame length (u32 BE) to tx_ctl, the JSON-RPC frame to data, poll rx_ctl for
  the response length, then read data until complete. On macOS, connect with
  `services=[rpc service]`; full discovery fails with CBErrorDomain 8 on one of
  the device's other characteristics. Devices with auth enabled return 401;
  digest auth is not implemented.

## License

MIT
