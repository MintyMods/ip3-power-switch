# Investigation: making the IP3POWERSWITCH button work on Linux

This is the full reverse-engineering walkthrough. If you just want the working bridge, go back to the [main README](../README.md).

## The puzzle

The Corsair AI Workstation 300 (and other IP3-Tech-based AMD Strix Halo mini-PCs) ships with a small white button on the front panel. The manual says it cycles three modes — Quiet, Balanced, Performance — and shows an OSD toast on Windows confirming the new mode.

On Linux, pressing the button has no observable effect:

- Nothing in `/dev/input/event*`
- Nothing in `/dev/hidraw*`
- Nothing in `journalctl -k`
- `power-profiles-daemon` does not change profile
- `lsusb` shows no Corsair device — the button is not on USB

The button is wired to the embedded controller (EC) via the SuperIO chip, not USB. Its only software-visible side effect is firing an ACPI WMI event — and no Linux kernel driver subscribes to that GUID, so the event is silently dropped.

## Step 1 — find the WMI GUIDs

`/sys/bus/wmi/devices/` lists every WMI GUID the BIOS exposes:

```
$ ls /sys/bus/wmi/devices/
05901221-D566-11D1-B2F0-00A0C9062910        # standard MS — BMOF metadata
05901221-D566-11D1-B2F0-00A0C9062910-1
8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C        # vendor — DEVTYPE=event
99D89064-8D50-42BB-BEA9-155B2E5D0FCD        # vendor — DEVTYPE=method
```

`DEVTYPE` is in each device's `uevent` file. The two vendor GUIDs split cleanly into one event (probably the button press) and one callable method.

## Step 2 — decode the BMOF

The `05901221-…` device has a `bmof` file: a binary description of the vendor classes. Patching its size header (the BIOS encodes it differently than `bmfdec` expects) and feeding it to [`pali/bmfdec`](https://github.com/pali/bmfdec) yields:

```
Class 2:
  Name=IP3_WMIEvent
  Superclassname=WmiEvent
  Provider=WmiProv
  Description=IP3 WMI Event
  guid={8FAFC061-22DA-46E2-91DB-1FE3D7E5FF3C}
  Variable: InstanceName, Active, EventDetail (UInt8[8])
```

So the `8FAFC061-…` GUID *is* the event the button fires, and it carries 8 bytes of payload describing what happened.

The method's BMOF entry isn't included in this blob — it lives in the device's own ACPI scope.

## Step 3 — disassemble the DSDT

```bash
sudo apt install acpica-tools
sudo cp /sys/firmware/acpi/tables/DSDT /tmp/DSDT
cd /tmp && iasl -d DSDT
```

Searching `DSDT.dsl` for the GUID's packed-byte form (`64 90 D8 99 50 8D BB 42 BE A9 15 5B 2E 5D 0F CD`) finds:

```asl
Device (WMIB)
{
    Name (_HID, "PNP0C14")          // standard WMI device
    Name (_UID, "IP3POWERSWITCH")   // <-- the smoking gun
    Name (_WDG, Buffer (0x28) { ...GUID..., 0x41, 0x41, 0x01, 0x02, ... })
    // 0x41,0x41 = "AA" object_id, 0x02 = METHOD flag
    Name (WQBA, Buffer (0x07C4) { ... another BMOF blob ... })

    Method (WMAA, 3, NotSerialized) { ... }
}
```

So the method's ASL name is **`WMAA`** (object_id "AA"), and the full path is `\_SB.WMIB.WMAA`.

## Step 4 — read WMAA's body

The actual method body shows nine sub-functions dispatched on `Arg1`:

| `Arg1` | Function | What it does |
|---|---|---|
| `0x01` | **Set mode** | `EC0.FCMI = 0x80\|mode`, `FEBC[1] = 0x12/13/11/14`, fires the event |
| `0x02` | **Get mode** | returns `EC0.FCMO` (0..3) |
| `0x03` | Set fan PWM | manual override of FAN1/FAN2 |
| `0x04` | Read fan RPM | combines FN1H/FN1L/FN2H/FN2L into a dword |
| `0x09` | Set keyboard backlight | (laptop-firmware leftover; no keyboard on this device) |
| `0x0A` | Read KB backlight | same |
| `0x0B` | Read CPU/GPU temps | (`sensors` already gives us this) |
| `0x0C` | Set "smart fan" flags | per-fan tuning (SEF0..SEF3) |
| `0x0D` | Get OSD code | the value Windows iCUE renders as a toast |

Excerpt of the set branch:

```asl
If ((Local1 == One))                                  // mode 1 = Balanced
{
    ^^AMW0.FEBC [Zero] = One
    ^^PCI0.SBRG.EC0.FCMI = 0x81
    ^^AMW0.FEBC [One] = 0x13
    Local2 = Zero
}
If ((Local1 == 0x02))                                 // mode 2 = Performance
{
    ^^AMW0.FEBC [Zero] = One
    ^^PCI0.SBRG.EC0.FCMI = 0x82
    ^^AMW0.FEBC [One] = 0x11
    Local2 = Zero
}
```

So a "set mode" is just two EC writes:

1. `EC0.FCMI = 0x80 | mode` — tells the EC to switch
2. `FEBC[1] = <event code>` — fills the event payload buffer for the WMI event

If we don't care about firing the WMI event (Linux has nothing to receive it anyway), the **set** simplifies to one byte: `EC[0x32] = 0x80|mode`.

## Step 5 — find FCMO and FCMI in the EC operation region

```asl
OperationRegion (ECMM, EmbeddedControl, ...) {
    ...
    Offset (0x31), FCMO, 8,    // mode output (read this for current state)
    Offset (0x32), FCMI, 8,    // mode input (write this to change)
    Offset (0x33), ...
    ...
}
```

So FCMO is at EC offset `0x31` and FCMI at `0x32`. Both are single bytes.

## Step 6 — verify with a differential EC dump

To confirm without invasive writes, we used the user's button presses themselves:

1. `modprobe ec_sys` — exposes `/sys/kernel/debug/ec/ec0/io` (256-byte snapshot of the entire EC space)
2. Dump 256 bytes
3. User presses the button
4. Dump again
5. Repeat for 4 dumps
6. Diff: which bytes change in a perfect 3-mode cycle?

Result:

| EC offset | s1 | s2 | s3 | s4 | Conclusion |
|---|---|---|---|---|---|
| `0x31` (FCMO) | 0 | 1 | 2 | 0 | mode register, cycles 0→1→2→0 |
| `0x56` | 2 | 4 | 1 | 2 | derived value (likely OSD or fan-curve index) |
| `0x70` | 33 | 34 | 35 | 36 | press counter — increments every press |

Snapshot 4 returns to snapshot 1's mode value. Confirmed: `0x31` is FCMO, the mode is real.

## Step 7 — write FCMI and watch FCMO follow

```bash
sudo modprobe -r ec_sys
sudo modprobe ec_sys write_support=1
printf '\x82' | sudo dd of=/sys/kernel/debug/ec/ec0/io \
                bs=1 count=1 seek=50 conv=notrunc       # FCMI = 0x82 (Perf)
sleep 0.5
sudo dd if=/sys/kernel/debug/ec/ec0/io bs=1 count=1 skip=49 2>/dev/null | xxd
# 00000000: 02
```

The EC's MCU picked up the FCMI write and updated FCMO. Mode actually switched.

## Step 8 — note about acpi_call

Originally we'd planned to invoke WMAA via the `acpi_call` kernel module, but on kernel 6.19 the headers reference `gcc-15` flags that no Ubuntu 24.04 gcc supports. Rather than chase the toolchain mismatch, we observed that calling WMAA's set branch is functionally identical to writing FCMI directly (modulo firing a WMI event nobody is listening to) and skipped the out-of-tree module entirely.

This makes the whole project work with **no kernel modules outside of mainline** — `ec_sys` is in mainline since forever.

## Step 9 — mode 4 (the secret one)

The `WMAA` set branch handles `Arg1 = 0x01` with `Arg2` values 0, 1, 2, **and 3**:

```asl
If ((Local1 == 0x03))   // mode 3 — undocumented
{
    ^^AMW0.FEBC [Zero] = One
    ^^PCI0.SBRG.EC0.FCMI = 0x83
    ^^AMW0.FEBC [One] = 0x14
    Local2 = Zero
}
```

The front-panel button only cycles 0/1/2. Setting mode 3 via FCMI works (FCMO updates to `0x03`), the system stays stable, and idle power dropped slightly in our test (~1 W). What it actually does under sustained load is unconfirmed. Treat as experimental.

## Summary

| Layer | What | Why we use it |
|---|---|---|
| ACPI WMI event GUID | `8FAFC061-…` (`IP3_WMIEvent`) | What the button fires. **Not used** — no kernel driver subscribes. |
| ACPI WMI method GUID | `99D89064-…` (`IP3POWERSWITCH`, `\_SB.WMIB.WMAA`) | Documented but bypassed (would need `acpi_call`). |
| EC offset `0x31` (FCMO) | current mode, read-back | What we read to know the state. |
| EC offset `0x32` (FCMI) | mode-change request | What we write to change mode. |
| `ec_sys write_support=1` | mainline kernel module | The **only** thing we need beyond stock Ubuntu. |

Total userspace daemon: ~150 lines of Python.
