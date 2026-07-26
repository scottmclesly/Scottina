# Longan I2C-CAN module — MCP2515 mask/filter forwarding bench

**What this gates.** Scottina Light's CAN gauge (`Light-CAN-Gauge-TODO.md`,
Phases 2–4) assumes it can program MCP2515 acceptance masks/filters through
the Longan I2C-CAN module and have the module drop non-matching frames in
firmware. If the module's firmware ignores filter register writes, Light must
read the whole bus and filter in software on a 192 KB device — which changes
the gauge design. This bench answers one yes/no question: **does the Longan
firmware forward mask/filter register writes?**

Scope: diagnostics. The traffic generator is bench equipment run in a shell;
it never becomes a Scottina feature.

---

## ⚠ STATUS — BENCH NOT RUN (blocked on hardware), 2026-07-25

**The six-step procedure below did not run, because the module is not present
on this box and there is no CAN traffic source. The filter-forwarding question
is therefore UNANSWERED: Light's Phases 2–4 are neither validated nor
invalidated. Do not treat this as a pass.**

Evidence collected this session (Prime, Raspberry Pi 5):

| Check | Result |
|---|---|
| `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` | present |
| `i2c-dev` loaded at boot | **no** — had to `sudo modprobe i2c-dev` (non-persistent) |
| `/dev/i2c-1` after modprobe | present (RP1 "Synopsys DesignWare I2C adapter") |
| `i2cdetect -y 1` | **completely empty — no device at 0x25, nothing at any address** |
| `i2cget -y 1 0x25` | `Read failed` (no ACK) |
| CAN traffic source | **none** — no `mcp2515` overlay in config.txt, no `can0`, no `slcan0`/CanTick link up |

Conclusion: the header I2C bus itself is healthy (it enumerates and scans
clean), but the Longan module is not wired/powered/responding on it, and even
if it were, no second CAN node is present to put frames on the bus. Both are
required. Re-run every step below on a box with the module attached **and** a
live CAN traffic source, then fill in the "Raw counts" table.

Two prerequisites to make it runnable next time:
1. **Load `i2c-dev` at boot** so `/dev/i2c-1` exists without a manual
   `modprobe` — `echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf`.
2. **A CAN traffic source on the same bus** — CanTick dialed in (`slcan0`), or
   the `gs_usb` USB-CAN adapter (`can0`, now present on Prime but DOWN and a
   single node) up at 250 k with a second node.

**Preferred traffic source: the synthetic bench kit** ([`tools/bench/`](../tools/bench/README.md)),
not `cangen`. It emits the `synthetic-marine` PGNs at known fixed ids, so
every id on the bus means something and the filter output is unambiguous. In
particular **`SYN Noise` (`0x3F108`) is the natural negative-control target**:
it is always on the bus and never selected, so a filter that admits the
selected ids and drops `SYN Noise` is unambiguous in the count — better than
`cangen`'s random ids. Program the positive filter for a selected id (e.g.
`SYN Feedback A`, `0x3F100`) and the negative-control filter for `SYN Noise`.

---

## Register addresses — from the library source, not guessed

Read from the Longan I2C-CAN Arduino library, branch `master`:

- Register map: [`I2C_CAN_dfs.h`](https://github.com/Longan-Labs/I2C_CAN_Arduino/blob/master/I2C_CAN_dfs.h)
- `init_Mask` / `init_Filt` / `begin` / receive: [`Longan_I2C_CAN_Arduino.cpp`](https://github.com/Longan-Labs/I2C_CAN_Arduino/blob/master/Longan_I2C_CAN_Arduino.cpp)

These are the module's **own I2C register map** (the firmware exposes a
register interface; it is not the raw MCP2515 SPI register space).

| Constant | Addr | Role |
|---|---|---|
| `REG_ADDR` | `0x01` | module I2C address |
| `REG_DNUM` | `0x02` | **number of RX frames buffered** (poll for availability) |
| `REG_BAUD` | `0x03` | **CAN bitrate** (write speed code, read back to confirm) |
| `REG_SEND` | `0x30` | TX frame |
| `REG_RECV` | `0x40` | **RX frame** (16-byte read) |
| `REG_ADDR_SET` | `0x51` | change module I2C address |
| `REG_MASK0` | `0x60` | **acceptance mask 0** (RXB0) |
| `REG_MASK1` | `0x65` | **acceptance mask 1** (RXB1) |
| `REG_FILT0` | `0x70` | **filter 0** (RXB0) |
| `REG_FILT1` | `0x80` | filter 1 (RXB0) |
| `REG_FILT2` | `0x90` | **filter 2** (RXB1) |
| `REG_FILT3` | `0xA0` | filter 3 (RXB1) |
| `REG_FILT4` | `0xB0` | filter 4 (RXB1) |
| `REG_FILT5` | `0xC0` | filter 5 (RXB1) |

Baud speed codes (written to `REG_BAUD`): `CAN_250KBPS = 15 (0x0F)`,
`CAN_500KBPS = 16`, `CAN_1000KBPS = 18`, … (`CAN_5KBPS = 1` … enumerated 1–18).

### How `begin()`, `init_Mask`, `init_Filt` encode over I2C

`begin(speed)` writes one byte to `REG_BAUD` then reads it back:

```c
IIC_CAN_SetReg(REG_BAUD, speedset);         // Wire: [0x03][speedset]
IIC_CAN_GetReg(REG_BAUD, &back);            // write [0x03], read 1 byte
```

`init_Mask(num, ext, id)` / `init_Filt(num, ext, id)` each write **5 bytes**
to the selected register — `[ext, id>>24, id>>16, id>>8, id>>0]` (ID
big-endian):

```c
// init_Mask: mask = (num==0) ? REG_MASK0 : REG_MASK1
// init_Filt: filt = (7+num)*0x10   →  0x70,0x80,0x90,0xA0,0xB0,0xC0
unsigned char dta[5] = { ext, id>>24, id>>16, id>>8, id>>0 };
IIC_CAN_SetReg(reg, 5, dta);                // Wire: [reg][ext][b3][b2][b1][b0]
```

Receive: `checkReceive()` reads `REG_DNUM` (>0 ⇒ a frame is available);
`readMsgBufID()` reads **16 bytes** from `REG_RECV` laid out as
`[id0..id3 big-endian][ext][rtr][len][data0..7][checksum]`.

### As `i2c-tools` commands (bus 1, addr `0x25`)

```sh
# --- baud: set 250 kbit/s (code 0x0F) and read back ---
i2ctransfer -y 1 w2@0x25 0x03 0x0f
i2ctransfer -y 1 w1@0x25 0x03 r1@0x25          # expect 0x0f

# --- program mask/filter for standard ID 0x123 (ext=0), exact 11-bit match ---
# mask 0x7FF on BOTH receive buffers, target ID in a filter on each buffer,
# so neither buffer defaults to accept-all (see note below).
i2ctransfer -y 1 w6@0x25 0x60 0x00 0x00 0x00 0x07 0xff   # MASK0 = 0x7FF
i2ctransfer -y 1 w6@0x25 0x65 0x00 0x00 0x00 0x07 0xff   # MASK1 = 0x7FF
i2ctransfer -y 1 w6@0x25 0x70 0x00 0x00 0x00 0x01 0x23   # FILT0 = 0x123 (RXB0)
i2ctransfer -y 1 w6@0x25 0x90 0x00 0x00 0x00 0x01 0x23   # FILT2 = 0x123 (RXB1)

# --- clear filters: mask 0x000 accepts everything ---
i2ctransfer -y 1 w6@0x25 0x60 0x00 0x00 0x00 0x00 0x00   # MASK0 = 0
i2ctransfer -y 1 w6@0x25 0x65 0x00 0x00 0x00 0x00 0x00   # MASK1 = 0

# --- drain + count: poll REG_DNUM, pop each frame from REG_RECV ---
i2ctransfer -y 1 w1@0x25 0x02 r1@0x25          # frames buffered
i2ctransfer -y 1 w1@0x25 0x40 r16@0x25         # pop one 16-byte frame
```

> **MCP2515 gotcha that makes or breaks step 5.** The MCP2515 has two receive
> buffers with independent masks: `MASK0`/`FILT0,FILT1` gate RXB0,
> `MASK1`/`FILT2..5` gate RXB1. A buffer whose mask is left at 0 accepts
> **everything**. If you program only `MASK0`/`FILT0`, RXB1 still passes all
> traffic and the negative control (step 5) will show frames even when the
> firmware *is* honouring filters — a false "ignored". Program **both** masks
> and put the target ID in a filter on **each** buffer, as above.

---

## Wiring (as bench-configured)

Longan module → Prime's 40-pin header:

| Signal | Header pin |
|---|---|
| SDA | 3 |
| SCL | 5 |
| GND | 6 |
| VCC | **2 (5 V)** — deliberately not Grove 3.3 V |

The 5 V rail is deliberate: it also settles whether the MCP2551 transceiver
rail was why an earlier 3.3 V trial showed a healthy `0x25` ACK but **zero
frames** (a 3.3 V-starved MCP2551 ACKs on the logic side while failing to
receive on the bus side). If 5 V still yields zero frames, suspect the
transceiver/wiring or missing 120 Ω termination — not the filters (see
interpretation).

---

## Procedure — run all six; the negative control is the point

Traffic source: `cangen` on a CAN HAT `can0`, or CanTick `slcan0`. Use fixed
standard IDs. Example generators (bench bus we own; keep in a shell):

```sh
# three distinct fixed IDs on the bus at ~10 frames/s each
cangen can0 -I 123 -L 8 -g 100 -n 0 &      # 0x123
cangen can0 -I 200 -L 8 -g 100 -n 0 &      # 0x200
cangen can0 -I 3AA -L 8 -g 100 -n 0 &      # 0x3AA
```

A drain-and-count helper (bench harness — **untested here, module absent**):

```sh
# count_frames SECONDS  → tallies popped frame IDs for SECONDS
count_frames() {
  local end=$(( $(date +%s) + $1 )) id
  declare -A seen
  while [ "$(date +%s)" -lt "$end" ]; do
    local n=$(i2ctransfer -y 1 w1@0x25 0x02 r1@0x25 2>/dev/null)
    n=$(( n ))
    while [ "$n" -gt 0 ]; do
      # pop one frame; ID = first 3 nibbles of bytes 0..3 (standard ID)
      local f=$(i2ctransfer -y 1 w1@0x25 0x40 r16@0x25 2>/dev/null)
      id=$(echo "$f" | awk '{printf "%03X", (strtonum($1)*256+strtonum($2))%2048}')
      seen[$id]=$(( ${seen[$id]:-0} + 1 ))
      n=$(( n - 1 ))
    done
    sleep 0.05
  done
  local total=0; for id in "${!seen[@]}"; do total=$(( total + seen[$id] )); \
    echo "  0x$id: ${seen[$id]}"; done
  echo "  TOTAL: $total  (IDs: ${!seen[*]})"
}
```

1. **Detect** — `i2cdetect -y 1`; confirm `0x25`.
2. **Bitrate** — set 250 k via `REG_BAUD` (`0x0f`), read back, confirm equal.
3. **Baseline (no filters)** — masks cleared; `count_frames 30`. Record count
   and the set of IDs seen.
4. **Positive** — program mask `0x7FF` + filter for one ID **on the bus** (both
   buffers, per the gotcha). Drain, `count_frames 30`. Expect **only that ID**.
5. **Negative control** — program a filter for an ID **not on the bus**
   (e.g. `0x555`). Drain, `count_frames 30`. Expect **zero**. Without this,
   step 4 passing could equally mean the module stopped receiving entirely.
6. **Restore** — clear filters (mask `0x000`); `count_frames 30`. Confirm the
   baseline ID set returns — proves the filters caused the change and the
   module did not wedge.

### Raw counts — fill in when run

| Step | Filter programmed | 30 s frame count | ID set seen |
|---|---|---|---|
| 3 baseline | none | _(pending)_ | _(pending)_ |
| 4 positive | mask 0x7FF, filt 0x123 | _(pending)_ | _(pending)_ |
| 5 negative | filt 0x555 (absent) | _(pending)_ | _(pending)_ |
| 6 restore | none | _(pending)_ | _(pending)_ |

---

## Interpretation (decide from the filled table)

- **Step 4 shows only the filtered ID AND step 5 shows zero** → firmware
  forwards mask/filter writes. **Light proceeds as written (Phases 2–4 valid).**
- **Step 4 count/ID set ≈ baseline (filter had no effect)** → filter writes are
  **ignored** by the firmware. **Light's Phases 2–4 are invalid** — the gauge
  cannot rely on hardware acceptance filtering and must read the full bus and
  filter in software. State this plainly; do not soften it.
- **Zero frames at any bitrate even with no filters (step 3 == 0)** →
  transceiver or wiring, not filters. On 5 V this points at the MCP2551 rail
  being ruled out — check **120 Ω termination** across CAN-H/CAN-L and the bus
  wiring before concluding anything about filters.
