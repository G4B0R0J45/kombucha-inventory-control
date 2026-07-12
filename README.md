# El Fermentario — Kombucha Inventory Control

Terminal inventory system for an artisanal kombucha business. It tracks
bottle entries and exits by flavor and lot, computes current stock, raises
low-stock alerts, keeps a full movement history with notes and staff, and
produces a simple sales report.

Course project — Programming, Data Engineering & AI, Universidad Politécnica
de Yucatán (May–August 2026). Author: Gabriel Enrique Rojas Velázquez.

## Requirements

- Python 3.10+ (standard library only for the main program).
- Optional, for QR labels: `pip install qrcode[pil]`

## Quick start

```bash
python3 inventory_system.py        # main program
python3 generate_lot_label.py      # printable QR labels (lot / flavor)
python3 import_appsheet.py FILE    # one-time import of the AppSheet history
```

On first run the program creates its data files next to the script.

## Menu guide

| Option | What it does |
|-------:|--------------|
| 1 | Register a movement. Staff, flavor and lot are chosen by number (or by scanning a QR label) to avoid typos; the type is a single key (`e` = entry, `x` = exit) and is validated on the spot — a typo re-asks immediately instead of failing at the end; a note is optional. |
| 2 | View current inventory: total per flavor, minimum, LOW STOCK alert, and a per-lot breakdown. |
| 3 | View the movement history (last 15 by default; `a` shows all, or type a flavor to filter). |
| 4 | Correct or delete one movement by ID. Changes are validated against the whole history before saving. |
| 5 | Catalog editor: add / rename / delete flavors, change minimums, manage staff. |
| 6 | Sales report: bottles out per flavor and per month, derived from the history. |
| 7 | Exit. |

## Data files

| File | Role |
|------|------|
| `movements.csv` | **Source of truth.** One row per successful movement: `movement_id, date_time, staff_name, flavor_name, lot_number, movement_type, bottle_quantity, note`. |
| `inventory_data.json` | Catalog only: flavors with their `minimum_stock`, and the staff list. It does not store stock. |
| `logs/YYYY-MM-DD.log` | Daily application log (INFO / WARNING / ERROR). |
| `labels/` | PNG labels produced by `generate_lot_label.py`. |

## How stock works (design decisions)

**The history is the source of truth.** Current stock is never stored: it is
derived by replaying `movements.csv` from the first row. Entries add bottles
to their lot; exits consume bottles from the **oldest lots first (FIFO)** —
the natural policy for a product with expiration dates.

**Exits do not record a lot on purpose.** FIFO is deterministic, so which
lots each exit consumed is fully reconstructible from the file itself;
storing it would be a redundant second copy that could contradict the first
after a correction. At registration time the program *shows* the FIFO
breakdown (e.g. `L-001 (15), L-002 (3)`) so staff know which physical lots
to pull from.

**The minimum is per flavor, across lots.** The LOW STOCK alert compares the
sum of all the flavor's lots against its minimum.

**Corrections recalculate everything.** Editing or deleting a movement
(option 4) replays the corrected history first; a change that would leave
any past exit without enough stock is rejected with the exact reason.
Editing the date is documentary only: FIFO follows row order, never dates.

**Sales report assumption.** Every exit counts as a sale. Use the note field
to flag exceptions (breakage, samples, credit) — the report can learn to
exclude them in a future version.

## Error-handling policy

1. Never overwrite data silently.
2. Never compute stock from data that failed validation.
3. Always tell the user exactly what is wrong and how to fix it.

In practice: both files are validated at startup (structure, row by row);
on corruption the program prints a precise report and refuses to run.
Writes are atomic (temp file + `os.replace`), every save reports failure,
and memory rolls back whenever the disk cannot be written. `Ctrl+C` inside
an operation cancels it; at the menu it exits cleanly. Unexpected errors go
to the log with a full traceback, never to the screen.

## Editing the files by hand (Excel notes)

Prefer option 4/5 inside the program. If you must open `movements.csv`:

- Close Excel before running the program (a locked file makes saves fail —
  safely, but they fail).
- Save as **CSV UTF-8**. The program reads/writes `utf-8-sig`, so Excel's
  BOM is tolerated and accents display correctly.
- Excel may reformat dates (e.g. `11/07/2026 10:52`). Harmless: order is by
  row, and the sales report parses several date shapes (unreadable dates
  land in an `unknown` bucket).
- Avoid purely numeric lot codes: Excel can eat leading zeros.
- Lot codes must not start with `FLAVOR:` — that prefix is reserved for
  flavor QR labels and the validator rejects it.
- Any structural damage (duplicated IDs, non-numeric quantities, missing
  columns) is caught at the next startup with row numbers.

## QR labels

`generate_lot_label.py` produces two kinds of PNG labels; every label also
prints the information as readable text, so staff can tell them apart
without scanning:

| Label | QR payload | Where to scan it |
|-------|------------|------------------|
| **Lot** | the lot code alone (e.g. `LOT202606-K114`) | any lot prompt (option 1 entries, option 4) |
| **Flavor** | `FLAVOR:<name>` (e.g. `FLAVOR:Jamaica`) | the flavor prompt in option 1 |

With a USB HID reader (the "gun" type, which acts as a keyboard and sends
Enter after the code), a whole entry can be registered scan-by-scan: scan
the flavor label at the flavor prompt, then the lot label at the lot
prompt. An existing lot code tops up that lot directly; a new code is
confirmed and used as-is. Labels printed before flavor labels existed keep
working unchanged.

**Why the `FLAVOR:` prefix (the safety design).** Two label types are only
safe if the program can tell them apart. Without the prefix, a flavor label
scanned by mistake at a lot prompt would silently become a bogus lot code.
With it, every lot prompt detects and rejects flavor labels with a clear
message, the flavor prompt rejects lot labels, and the file validator
refuses lot codes that start with `FLAVOR:` (the prefix is reserved). The
wrong scan is always a one-line error, never corrupted data.

## Importing the AppSheet history

The business ran first on AppSheet (Google Sheets backend). To migrate that
history: open the Google Sheet, select the **Movimientos** tab, and use
*File → Download → Comma Separated Values (.csv)* — that produces exactly
the expected shape (`ID_Movimiento, Fecha, Tipo de movimiento, Lote, Sabor,
ID_Producto_Escaneado, Cantidad (botellas), Responsable, Observaciones`).
Then:

```bash
python3 import_appsheet.py Movimientos.csv
```

The importer only fills an **empty** history (it refuses to touch an
existing `movements.csv`) and applies these rules, all reported on screen:

- Rows are validated first (dates, types, quantities…); any problem aborts
  the import with exact row numbers. Blank export rows are skipped.
- Rows are **sorted by date** (stable: same-day rows keep their file order)
  because FIFO follows row order and exports are not always chronological.
- `Entrada` → `entry`, `Salida` → `exit`; dates become `YYYY-MM-DD 00:00:00`;
  IDs are renumbered 1..N (keep the original export as backup).
- Exits keep `lot_number` empty (FIFO decides), but the lot AppSheet
  recorded is preserved inside the note as `[AppSheet lot: …]`.
- **Duplicate flavors are merged here or never.** Spellings that differ only
  by case/spacing (`EARL GREY` / `Earl grey`) are unified almost
  automatically; names sharing their first word (`Piña` / `Piña con chile`)
  are shown with their bottles-in / bottles-out profile so you can decide
  (`2->1` merges, Enter keeps them). This is the one easy moment to merge:
  after the import, option 5's rename refuses to fuse two existing flavors
  on purpose, and the fallback is editing rows one by one (option 4).
- Exits that exceed the stock available at their point (entries that were
  never captured in AppSheet) are listed and require confirmation; the main
  program shows the same warnings at startup.
- Missing flavors are added with `minimum_stock` 0 and missing staff are
  added — set the real minimums afterwards in option 5.

At the end it prints the net stock per flavor so you can compare against
the AppSheet dashboard before trusting the migration.

## Troubleshooting

- **`DATA ERROR` at startup** — one of the data files is corrupted; the
  message lists the exact rows/problems. Fix the file or restore it from
  git. The program stops on purpose so nothing gets miscalculated.
- **`movements.csv upgraded: 'note' column added.`** — one-time automatic
  upgrade of files created before the note column existed.
- **Old-format `inventory_data.json` rejected** — files from the pre-history
  version (stock stored in the JSON) are not migrated; delete the test files
  or restore a current pair from git.

## Declaración de uso de IA

Este proyecto se desarrolló con asistencia de Claude (Anthropic) para el
diseño de la arquitectura, la implementación y las pruebas, bajo la
dirección, revisión y validación del autor.

- Herramienta: Claude (Anthropic)
- Propósito: diseño de arquitectura, generación y refactorización de código,
  diseño de casos de prueba y documentación.
- Fecha: julio de 2026
