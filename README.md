# ip3-power-switch

**Make the front-panel power-profile button work on Linux** for AI mini-PCs built around the IP3 Tech AMD Strix Halo mainboard — including the **Corsair AI Workstation 300**, **Beelink GTR / SER AI**, **GMK EVO-X1 / EVO-X2**, and other rebadges of the same reference design.

> Keywords for search: Corsair AI Workstation 300 Linux, AMD Ryzen AI Max+ 395, Strix Halo Linux fan button, IP3 Tech IP3POWERSWITCH WMI, ACPI WMI 99D89064-8D50-42BB-BEA9-155B2E5D0FCD, EC FCMO FCMI, power-profiles-daemon Strix Halo, Linux Quiet Balanced Performance Sustained mode mini PC, home automation, Home Assistant MQTT power profile select.

---

## TL;DR

The white front-panel button on these machines is documented as cycling Quiet → Balanced → Performance modes — but on Linux, pressing it appears to do nothing. There is no Corsair / Beelink / GMK Linux driver, no `acpi_listen` event, no input-device key, no kernel log line. The signal vanishes.

Despite that, **the embedded controller (EC) absolutely sees the press** and updates an internal mode register. The Linux kernel just doesn't know which WMI event GUID to subscribe to. This repo:

1. Documents the IP3 WMI / EC interface (GUIDs, ACPI methods, EC offsets).
2. Provides a tiny Python bridge that exposes the mode to **Home Assistant** via MQTT discovery — read the current mode, set any of the four modes (yes, four — the WMI accepts a fourth, undocumented mode beyond the three the button cycles).
3. Writes directly to the EC byte that the BIOS's own ACPI method writes when the button is pressed, so all hardware behaviours (fan curves, PSU hybrid relay, OSD code) propagate correctly.

End result: a 3- or 4-way pill toggle on your HA dashboard, with no out-of-tree kernel modules, no proprietary tools, and no `acpi-call`.

---

## What I had to figure out (the short version)

- The button fires WMI event GUID **`8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C`** with an 8-byte payload. No mainline Linux driver subscribes, so it's discarded.
- The mode is set/queried by WMI method GUID **`99D89064-8D50-42BB-BEA9-155B2E5D0FCD`** on a device named `IP3POWERSWITCH` (`_HID PNP0C14`). The ACPI method is `\_SB.WMIB.WMAA(arg0, arg1, arg2)`.
- The method's set branch writes one byte: `EC[0x32] = 0x80 | mode`. Reading EC byte `0x31` gives the current mode (0..3).
- Therefore: writing `0x80|mode` to EC offset `0x32` via `/sys/kernel/debug/ec/ec0/io` (with `ec_sys` `write_support=1`) does exactly what the BIOS does, without needing an out-of-tree kernel module to invoke ACPI.

Full investigation in [`research/INVESTIGATION.md`](research/INVESTIGATION.md).

---

## What you get

A Home Assistant `select` entity called **Power profile** with these options:

| Label | Mode | EC byte (FCMI write) | EC byte (FCMO read) | PPD profile applied alongside |
|---|---|---|---|---|
| Quiet | 0 | `0x80` | `0x00` | `power-saver` |
| Balanced | 1 | `0x81` | `0x01` | `balanced` |
| Performance | 2 | `0x82` | `0x02` | `performance` |
| Sustained | 3 | `0x83` | `0x03` | `performance` (undocumented mode; characterised — see [research](research/INVESTIGATION.md#step-9--mode-4-the-secret-one)) |

Tap a pill on your dashboard → the EC switches mode, the OS power profile follows. Same effect as pressing the front-panel button on Windows with iCUE.

---

## Quick start

### Prerequisites

- An IP3-Tech-based AMD Strix Halo mini-PC (verify by checking the DSDT for `_UID, "IP3POWERSWITCH"` — see [Verifying your hardware](#verifying-your-hardware) below).
- Linux with the `ec_sys` kernel module (in mainline, no install needed).
- A working MQTT broker (Mosquitto recommended).
- Home Assistant with the MQTT integration configured.
- Python 3.9+ with `paho-mqtt` (`apt install python3-paho-mqtt`).

### Install

```bash
# 1. Clone
sudo git clone https://github.com/MintyMods/ip3-power-switch /opt/ip3-power-switch

# 2. Enable EC writes (one-time)
echo 'ec_sys' | sudo tee /etc/modules-load.d/ec_sys.conf
echo 'options ec_sys write_support=1' | sudo tee /etc/modprobe.d/ec_sys.conf
sudo modprobe -r ec_sys; sudo modprobe ec_sys  # take effect now

# 3. Set up the env file with your MQTT creds
sudo install -o root -g root -m 600 /dev/null /etc/ip3-power-bridge.env
sudo tee /etc/ip3-power-bridge.env >/dev/null <<EOF
MQTT_USER=corsair-bridge
MQTT_PASS=YOUR-PASSWORD-HERE
EOF

# 4. Install the systemd unit
sudo cp /opt/ip3-power-switch/systemd/ip3-power-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ip3-power-bridge.service

# 5. Watch the logs
sudo journalctl -u ip3-power-bridge.service -f
```

If you see `state -> Quiet` (or whichever mode you're in), you're done. The HA MQTT discovery topic publishes a `select` entity called `select.minty_ai_workstation_power_profile` (rename the `DEVICE_ID` constant in `bridge/bridge.py` if you want a different identifier).

---

## Verifying your hardware

Quick check whether your machine has the same WMI device:

```bash
sudo apt install -y acpica-tools
sudo cp /sys/firmware/acpi/tables/DSDT /tmp/DSDT
cd /tmp && iasl -d DSDT
grep -c IP3POWERSWITCH DSDT.dsl
```

If you get a count > 0, the rest of this repo applies. If not, your firmware exposes a different vendor WMI scheme — but the same investigation pattern (DSDT + EC differential snapshots) will get you there.

You can also verify the EC mode register directly:

```bash
sudo modprobe ec_sys write_support=1
sudo dd if=/sys/kernel/debug/ec/ec0/io bs=1 count=1 skip=49 2>/dev/null | xxd
# Expected: a single byte 00, 01, 02, or 03
# Press the button, run again — the byte should change
```

---

## Repo layout

```
bridge/                      Python MQTT ↔ EC bridge
  bridge.py
systemd/                     Service unit and modprobe config
  ip3-power-bridge.service
  ec_sys.conf
research/                    The full investigation, DSDT excerpts, snapshots
  INVESTIGATION.md
  WMAA-method-extract.txt
  ec-snapshot-diff.md
home-assistant/              HA MQTT setup notes + dashboard card YAML
  README.md
LICENSE                      MIT
```

---

## Caveats

- **Tested on Corsair AI Workstation 300** (kernel 6.19, Ubuntu 24.04). The IP3 WMI tree should be identical on the Beelink/GMK/etc. variants but I have not personally confirmed.
- **Sustained mode (mode 3) is undocumented but characterised.** Empirically: locks CPU at boost frequency at idle (no C-state sleep) and caps total package power lower than Performance. Useful for low-latency inference, wasteful for idle servers. Full numbers in [research/INVESTIGATION.md](research/INVESTIGATION.md).
- **Fan RPM is not readable** through the IP3 EC interface on the AI-300 — fans are on Corsair's own (USB-attached) controller which has no Linux driver. Temperatures are still readable; you just can't surface fan tach in HA without iCUE.
- **Direct EC writes are unsafe in general.** This repo only writes to one specific byte (`EC[0x32]`, `FCMI`) which is the BIOS's intended write target. Do *not* use the same approach to poke arbitrary EC bytes — that can easily brick the system.
- **No kernel driver.** A proper fix would be a small kernel platform driver subscribing to `8FAFC061-…` and exposing `platform_profile`, like Lenovo's driver does. PRs welcome from anyone with the kernel chops.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Related projects / further reading

- [`pali/bmfdec`](https://github.com/pali/bmfdec) — the BMOF decompiler used to read WMI metadata
- [`acpica-tools`](https://acpica.org/) — DSDT extraction and disassembly
- [`power-profiles-daemon`](https://gitlab.freedesktop.org/hadess/power-profiles-daemon) — the OS-side power profile manager
- [Lenovo Ideapad ACPI driver source](https://elixir.bootlin.com/linux/latest/source/drivers/platform/x86/ideapad-laptop.c) — example of a proper kernel platform driver if anyone wants to upstream this
