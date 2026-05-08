# EC differential snapshot: how we located FCMO

The DSDT confirms FCMO is at offset 0x31 — but if you don't have DSDT
disassembly handy, you can find the mode register by capturing the EC
state across a known cycle of button presses.

## Method

```bash
sudo modprobe ec_sys
# Run 4 times, with one button press between each:
for i in 1 2 3 4; do
  sudo cat /sys/kernel/debug/ec/ec0/io > ec_$i.bin
  read -p "Press the button, then Enter for next snapshot..."
done
```

Then diff the binary snapshots byte by byte:

```python
import sys
snaps = [open(f'ec_{i}.bin', 'rb').read() for i in range(1, 5)]
for off in range(len(snaps[0])):
    vals = [s[off] for s in snaps]
    if len(set(vals)) > 1:
        cycle = ' ★ s1==s4' if vals[0] == vals[3] else ''
        print(f'0x{off:03x}: {vals}{cycle}')
```

## Result on Corsair AI Workstation 300

| EC offset | s1 | s2 | s3 | s4 | Cycle? | What it is |
|---|---|---|---|---|---|---|
| `0x31` | 0 | 1 | 2 | 0 | ✓ | **FCMO** — current mode, 0..3 |
| `0x56` | 2 | 4 | 1 | 2 | ✓ | secondary, likely OSD code |
| `0x70` | 33 | 34 | 35 | 36 | drift | **CPUT** (CPU temp) — happened to drift during capture |

Snapshot 4 returns to snapshot 1's mode register because we cycled all
the way around (3 modes, 3 presses).

> **Lesson learned:** the monotonic increment at `0x70` initially looked like a button-press counter, but cross-checking against the DSDT showed it was just `CPUT` — the CPU temperature drifting from 33 °C to 36 °C over the 30 seconds the capture took. **Always validate diff results against the DSDT operation region** before drawing conclusions.

If you're investigating a different IP3 (or other) board, this same
technique will find the mode register for you in ~5 minutes without
needing to read the DSDT.
