# Files — USB offload & decode tables

Scottina's **Files** screen is how data gets on and off the boat without a
laptop: plug a USB stick into any Pi port and a **Files tile appears on the
Home screen** (and disappears again when you pull the stick, like the other
device tiles). It does two jobs:

1. **Offload capture logs** — everything the tools record lands in
   `/opt/kilodash/captures/` (candump `.log`, logic-analyser `.sr`, SDR
   `.cu8` IQ, Kismet, Wi-Fi sniff CSVs). Copy them to the stick one at a
   time or all at once, then read them on a laptop with the usual tools
   (PulseView, Wireshark, SavvyCAN, …).
2. **Exchange CAN decode tables** — carry **DBC** files (CAN signal
   databases) and **NMEA2000** definitions on the stick and import them into
   `/opt/kilodash/tables/`, the canonical place decoding tools on the Pi
   (the Node-RED DBC flow, Signal K / canboatjs) read them from. Export goes
   the other way, so the stick doubles as the backup of your table set.

Copies **never delete the originals** — `captures/` stays authoritative
until you clear it yourself (Pi Health shows disk usage).

---

## Using it

1. Plug in a USB stick (FAT32/exFAT as it comes from the shop is fine; ext4
   works too). The **Files** tile appears on Home.
2. Tap the tile. The stick is mounted automatically at `/media/usb` and the
   screen opens. Top to bottom:

| Control | What it does |
|---|---|
| **USB stick card** | Partition name and free space, with a mounted/not-mounted state colour. |
| **Eject / Mount** | `Eject` syncs all pending writes and unmounts — wait for the *"safe to remove"* toast before pulling the stick. After ejecting (or if auto-mount failed) the button becomes `Mount`. |
| **Copy all logs → USB (n)** | Copies every capture not already on the stick. The count shows how many are pending; the button reads *"All logs on stick ✓"* when there's nothing left to do. |
| **Tables ← USB** | Imports decode tables from the stick into `/opt/kilodash/tables/`. |
| **Tables → USB** | Exports `/opt/kilodash/tables/` onto the stick (backup / carry to another machine). |
| Status strip | What the current background job is doing, and its result. |
| **Capture list** | Every file in `captures/`, newest first, with its size. **Tap a row to copy just that file.** Rows already on the stick are dimmed and marked **✓ on stick**. |

Copies run in the background — the UI stays responsive, and a toast reports
the result. One job runs at a time.

3. Tap **Eject**, wait for *"safe to remove"*, pull the stick. Leaving the
   screen (Back) also syncs and unmounts, so you can never strand a dirty
   mount.

## Where things go on the stick

```
<stick>/
└── scottina/
    ├── captures/     ← offloaded logs (same filenames as on the Pi)
    └── tables/       ← exported decode tables
```

## Decode tables: what gets imported

**Tables ← USB** scans the stick's **root folder** and **`scottina/tables/`**
for these extensions and copies matches into `/opt/kilodash/tables/`
(existing files with the same name are overwritten — the stick wins):

| Extension | What it is |
|---|---|
| `.dbc` | CAN signal database (the industry-standard Vector format) |
| `.kcd`, `.sym` | Alternative CAN database formats (Kayak, PCAN) |
| `.n2k`, `.json`, `.xml` | NMEA2000 / canboat PGN definitions |

So the workflow for a new boat/vehicle is: put `engine.dbc` (or a canboat
`pgns.json`) in the root of any stick, plug it in, **Tables ← USB**, done.

One nuance since the CAN/NMEA2K split ([TABLES.md](../TABLES.md)): imported
files land in the `tables/` **inbox**, where they are inert. To feed the
[NMEA2K screen](NMEA2K.md)'s live decode they must pass validation once —
open the **Tables** tile → converter → *Installed* tab → **Validate &
ingest**. **Tables → USB** exports both the inbox and the installed store
(tables + manifests, flat) — the SD shape Scottina Light reads — and emits
Light's browse index (`<name>.index.json` + `pgn-<num>.json`, TABLES.md
§5b/§5c) alongside it, so Light's CAN gauge can offer a PGN picker.

## Notes & guardrails

- **Rootfs safety:** a USB drive that carries a system partition (e.g. an
  SSD you boot from) is **never** offered as offload media — only free
  sticks make the tile appear.
- **Multi-partition / installer sticks:** partitions are tried in order and
  the first that mounts **writable** wins (an Ubuntu-installer stick's
  read-only iso9660 partition is skipped automatically). If *nothing* on
  the stick is writable it mounts read-only: the card shows `read-only`,
  offload/export are disabled, but **Tables ← USB** still works.
- **Pulled without ejecting?** Nothing wedges: the mount is lazily detached
  on the way out. But any copy that was mid-write is truncated — re-copy it;
  the source in `captures/` is untouched.
- Only the **first** USB mass-storage stick found is used; one stick at a
  time.
- Hidden working files (dotfiles like `.scan.csv`) are not listed or copied.
