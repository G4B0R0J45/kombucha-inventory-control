"""
One-time importer: AppSheet 'Movimientos' export  ->  movements.csv + catalog.

El Fermentario ran first on AppSheet (Google Sheets backend). This script
migrates that history into the format inventory_system.py expects, following
the same error-handling policy as the main program:

  1. Never overwrite data silently.
  2. Never compute stock from data that failed validation.
  3. Always tell the user exactly what is wrong and how to fix it.

Expected input (CSV UTF-8, the shape AppSheet/Google Sheets exports):
  ID_Movimiento, Fecha, Tipo de movimiento, Lote, Sabor,
  ID_Producto_Escaneado, Cantidad (botellas), Responsable, Observaciones

Mapping rules (all reported on screen):
  - Rows are sorted by Fecha (stable: same-day rows keep their file order)
    because FIFO follows row order and AppSheet exports are not guaranteed
    to be chronological.
  - 'Entrada' -> entry, 'Salida' -> exit.
  - Fecha (a date without time) becomes 'YYYY-MM-DD 00:00:00'.
  - Entries keep their lot. Exits keep lot_number EMPTY (the main program
    derives exit lots by FIFO); the lot AppSheet recorded is preserved as
    documentation inside the note: '[AppSheet lot: ...]'.
  - movement_id is renumbered 1..N in chronological order (AppSheet hex IDs
    do not fit the integer format; the original export stays as backup).
  - Flavor spellings that differ only by case/spacing are unified
    interactively (the catalog cannot hold two of them anyway).
  - Missing flavors are added to the catalog with minimum_stock = 0 and
    missing staff are added: tune both later in option 5.

Usage:
  python3 import_appsheet.py <appsheet_export.csv>
"""
import csv
import json
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "inventory_data.json")
MOVEMENTS_FILE = os.path.join(SCRIPT_DIR, "movements.csv")

# Must match inventory_system.py.
CSV_FIELDS = ["movement_id", "date_time", "staff_name", "flavor_name",
              "lot_number", "movement_type", "bottle_quantity", "note"]
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
FLAVOR_QR_PREFIX = "FLAVOR:"

REQUIRED_COLUMNS = ["Fecha", "Tipo de movimiento", "Lote", "Sabor",
                    "Cantidad (botellas)", "Responsable", "Observaciones"]
TYPE_MAP = {"entrada": "entry", "salida": "exit",
            "entry": "entry", "exit": "exit"}
INPUT_DATE_FORMATS = ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                      "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]


def parse_date(text):
    """Returns a datetime for any accepted input shape, or None."""
    for date_format in INPUT_DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    return None


def read_export(path):
    """Reads and validates the AppSheet export. Returns a list of
    (line_number, parsed_date, row) tuples ready to convert. Every problem
    is collected and reported at once; the import aborts if any exists."""
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            header = reader.fieldnames or []
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        sys.exit(f"ERROR: could not read '{path}' ({e}).")

    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        sys.exit("ERROR: the export is missing expected column(s): "
                 + ", ".join(missing)
                 + "\nExport the 'Movimientos' sheet again as CSV without "
                   "renaming its columns.")

    rows = []
    skipped_empty = []
    problems = []
    for line_number, row in enumerate(raw_rows, start=2):  # line 1 = header
        values = {column: (row.get(column) or "").strip()
                  for column in REQUIRED_COLUMNS}
        if not any(values.values()):
            skipped_empty.append(line_number)   # blank export artifact
            continue

        parsed_date = parse_date(values["Fecha"])
        if parsed_date is None:
            problems.append(f"Row {line_number}: unreadable Fecha "
                            f"'{values['Fecha']}' (expected DD/MM/YYYY).")
        if values["Tipo de movimiento"].lower() not in TYPE_MAP:
            problems.append(f"Row {line_number}: Tipo de movimiento "
                            f"'{values['Tipo de movimiento']}' must be "
                            "'Entrada' or 'Salida'.")
        if not values["Sabor"]:
            problems.append(f"Row {line_number}: Sabor is empty.")
        if not values["Lote"]:
            problems.append(f"Row {line_number}: Lote is empty.")
        elif values["Lote"].upper().startswith(FLAVOR_QR_PREFIX):
            problems.append(f"Row {line_number}: Lote '{values['Lote']}' starts "
                            f"with '{FLAVOR_QR_PREFIX}', a reserved prefix.")
        if not values["Responsable"]:
            problems.append(f"Row {line_number}: Responsable is empty.")
        quantity = values["Cantidad (botellas)"]
        if not quantity.isdigit() or int(quantity) <= 0:
            problems.append(f"Row {line_number}: Cantidad (botellas) "
                            f"'{quantity}' must be an integer > 0.")

        rows.append((line_number, parsed_date, values))

    if skipped_empty:
        print(f"Skipped {len(skipped_empty)} completely empty row(s) "
              f"(file line(s): {', '.join(map(str, skipped_empty))}).")
    if problems:
        sys.exit("ERROR: the export has invalid rows. Nothing was imported. "
                 "Fix them and run again:\n  - " + "\n  - ".join(problems))
    if not rows:
        sys.exit("ERROR: the export has no data rows.")
    return rows


def simplified(name):
    """Key used to detect same-flavor spellings: case- and space-insensitive
    ('Limón con hierba buena' == 'Limón con hierbabuena')."""
    return "".join(name.lower().split())


def flavor_profiles(rows, mapping):
    """Per-flavor counts (rows, bottles in, bottles out) with any already
    accepted mapping applied. The in/out profile is the best clue for
    deciding whether two spellings are the same product."""
    profiles = {}
    for _, _, values in rows:
        name = values["Sabor"]
        name = mapping.get(name, name)
        record = profiles.setdefault(name, {"rows": 0, "in": 0, "out": 0})
        record["rows"] += 1
        quantity = int(values["Cantidad (botellas)"])
        if values["Tipo de movimiento"].lower() in ("entrada", "entry"):
            record["in"] += quantity
        else:
            record["out"] += quantity
    return profiles


def describe(name, profiles):
    record = profiles[name]
    return (f"{name} ({record['rows']} rows, "
            f"in {record['in']} / out {record['out']} bottles)")


def unify_flavor_spellings(rows, mapping):
    """Pass 1 — near-certain duplicates: names that only differ by case or
    spacing. The catalog cannot hold two of them anyway, so unifying is the
    default (Enter); 'k' keeps them separate."""
    profiles = flavor_profiles(rows, mapping)
    groups = {}
    for name in profiles:
        groups.setdefault(simplified(name), []).append(name)
    duplicated = [sorted(names, key=lambda n: -profiles[n]["rows"])
                  for names in groups.values() if len(names) > 1]
    if not duplicated:
        return

    print("\n--- Duplicate flavor spellings found ---")
    for names in duplicated:
        print("These spellings appear to be the same flavor:")
        for position, name in enumerate(names, start=1):
            print(f"  {position}. {describe(name, profiles)}")
        choice = input("Unify every row under which spelling? "
                       "(number, Enter = 1, 'k' = keep separate): ").strip().lower()
        if choice == "k":
            print("Kept separate.\n")
            continue
        index = int(choice) if choice.isdigit() and 1 <= int(choice) <= len(names) else 1
        canonical = names[index - 1]
        for name in names:
            if name != canonical:
                mapping[name] = canonical
        print(f"Unified as '{canonical}'.\n")


def review_related_flavors(rows, mapping):
    """Pass 2 — names that share their first word MAY be the same product
    (e.g. entries registered as 'Piña' and exits as 'Piña con chile'). Only
    a human can tell, so NOTHING is merged unless explicitly asked here.
    This moment matters: once imported, the program's rename (option 5)
    refuses to merge two existing flavors, on purpose."""
    profiles = flavor_profiles(rows, mapping)
    groups = {}
    for name in sorted(profiles):
        groups.setdefault(name.split()[0].lower(), []).append(name)
    candidates = [names for names in groups.values() if len(names) > 1]
    if not candidates:
        return

    print("--- Possibly related flavors (review) ---")
    print("Compare the in/out bottles: entries under one name and exits under")
    print("another usually mean the SAME product was registered two ways.")
    for names in candidates:
        print("\nThese names share their first word:")
        for position, name in enumerate(names, start=1):
            print(f"  {position}. {describe(name, profiles)}")
        while True:
            directive = input("Merge? type FROM->TO numbers (e.g. 2->1), "
                              "Enter = done with this group: ").strip()
            if not directive:
                break
            parts = directive.replace(" ", "").split("->")
            if (len(parts) != 2 or not all(part.isdigit() for part in parts)
                    or not all(1 <= int(part) <= len(names) for part in parts)
                    or parts[0] == parts[1]):
                print("Format: FROM->TO with two different numbers from the list.")
                continue
            source, target = names[int(parts[0]) - 1], names[int(parts[1]) - 1]
            mapping[source] = target
            print(f"'{source}' will be imported as '{target}'.")


def resolve_mapping(mapping):
    """Follows chains (A->B, B->C becomes A->C) with a cycle guard."""
    resolved = {}
    for name in mapping:
        target = name
        visited = {name}
        while target in mapping and mapping[target] not in visited:
            target = mapping[target]
            visited.add(target)
        resolved[name] = target
    return resolved


def convert(rows, mapping):
    """Sorts chronologically and converts every AppSheet row to the
    movements.csv format."""
    ordered = sorted(rows, key=lambda item: item[1])  # stable: keeps file order
    movements = []
    for movement_id, (_, parsed_date, values) in enumerate(ordered, start=1):
        flavor_name = values["Sabor"]
        flavor_name = mapping.get(flavor_name, flavor_name)
        movement_type = TYPE_MAP[values["Tipo de movimiento"].lower()]
        note = values["Observaciones"]
        if movement_type == "entry":
            lot_number = values["Lote"]
        else:
            lot_number = ""  # FIFO decides; the AppSheet lot goes to the note
            appsheet_lot = f"[AppSheet lot: {values['Lote']}]"
            note = f"{note} {appsheet_lot}".strip()
        movements.append({
            "movement_id": str(movement_id),
            "date_time": parsed_date.strftime(DATE_FORMAT),
            "staff_name": values["Responsable"],
            "flavor_name": flavor_name,
            "lot_number": lot_number,
            "movement_type": movement_type,
            "bottle_quantity": values["Cantidad (botellas)"],
            "note": note,
        })
    return movements


def replay_check(movements):
    """Same FIFO replay the main program runs: returns the list of exits that
    exceed the stock available at their point of the history."""
    stock = {}
    inconsistencies = []
    for movement in movements:
        record = stock.setdefault(movement["flavor_name"], {})
        quantity = int(movement["bottle_quantity"])
        if movement["movement_type"] == "entry":
            lot = movement["lot_number"]
            record[lot] = record.get(lot, 0) + quantity
        else:
            remaining = quantity
            for lot in list(record):
                taken = min(record[lot], remaining)
                record[lot] -= taken
                remaining -= taken
                if record[lot] == 0:
                    del record[lot]
                if remaining == 0:
                    break
            if remaining > 0:
                inconsistencies.append(
                    f"{movement['date_time'][:10]}: exit of {quantity} "
                    f"'{movement['flavor_name']}' exceeds the stock available "
                    f"at that point by {remaining}.")
    return inconsistencies


def load_or_create_catalog():
    """Returns the existing catalog, or a fresh one. A file that exists but
    cannot be parsed aborts the import (never overwrite data silently)."""
    if not os.path.exists(DATA_FILE):
        return {"flavors": {}, "staff": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        sys.exit(f"ERROR: {DATA_FILE} exists but could not be read ({e}). "
                 "Fix or move it, then run the import again.")
    if not isinstance(data, dict) or "flavors" not in data:
        sys.exit(f"ERROR: {DATA_FILE} does not look like a catalog. "
                 "Move it away and run the import again.")
    data.setdefault("staff", [])
    return data


def write_atomic_csv(movements):
    temp_path = MOVEMENTS_FILE + ".tmp"
    with open(temp_path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(movements)
    os.replace(temp_path, MOVEMENTS_FILE)


def write_atomic_json(catalog):
    temp_path = DATA_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(catalog, file, indent=4, ensure_ascii=False)
    os.replace(temp_path, DATA_FILE)


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 import_appsheet.py <appsheet_export.csv>")

    # Never overwrite an existing history: this is a first-migration tool.
    if os.path.exists(MOVEMENTS_FILE):
        with open(MOVEMENTS_FILE, "r", newline="", encoding="utf-8-sig") as file:
            existing_rows = max(0, sum(1 for _ in file) - 1)
        if existing_rows > 0:
            sys.exit(f"ERROR: {MOVEMENTS_FILE} already has {existing_rows} "
                     "movement(s). This importer only fills an empty history: "
                     "rename or back up that file first, then run again.")

    print("=== El Fermentario - AppSheet history import ===")
    rows = read_export(sys.argv[1])
    mapping = {}
    unify_flavor_spellings(rows, mapping)
    review_related_flavors(rows, mapping)
    movements = convert(rows, resolve_mapping(mapping))

    inconsistencies = replay_check(movements)
    if inconsistencies:
        print(f"\nWARNING: {len(inconsistencies)} exit(s) exceed the stock "
              "available at their point in the history (entries missing in "
              "AppSheet, or duplicate flavors kept separate). The main "
              "program will show these same warnings at startup:")
        for problem in inconsistencies[:10]:
            print(f"  - {problem}")
        if len(inconsistencies) > 10:
            print(f"  ... and {len(inconsistencies) - 10} more.")
        if input("Import anyway? (y/n): ").strip().lower() != "y":
            sys.exit("Import cancelled. Nothing was written.")

    catalog = load_or_create_catalog()
    flavor_names = sorted({m["flavor_name"] for m in movements})
    new_flavors = [name for name in flavor_names
                   if name not in catalog["flavors"]]
    for name in new_flavors:
        catalog["flavors"][name] = {"minimum_stock": 0}
    staff_names = sorted({m["staff_name"] for m in movements})
    new_staff = [name for name in staff_names if name not in catalog["staff"]]
    catalog["staff"].extend(new_staff)

    entries = sum(1 for m in movements if m["movement_type"] == "entry")
    print(f"\nReady to write {len(movements)} movements "
          f"({entries} entries, {len(movements) - entries} exits), "
          f"add {len(new_flavors)} flavor(s) and {len(new_staff)} staff "
          "name(s) to the catalog.")
    if input("Proceed? (y/n): ").strip().lower() != "y":
        sys.exit("Import cancelled. Nothing was written.")

    try:
        write_atomic_csv(movements)
        write_atomic_json(catalog)
    except OSError as e:
        sys.exit(f"ERROR: could not write the data files ({e}). "
                 "Close Excel if it has them open and run again.")

    print(f"\nDone. {MOVEMENTS_FILE} and {DATA_FILE} were written.")
    print(f"Date range: {movements[0]['date_time'][:10]} to "
          f"{movements[-1]['date_time'][:10]}.")

    totals = {}
    for movement in movements:
        quantity = int(movement["bottle_quantity"])
        delta = quantity if movement["movement_type"] == "entry" else -quantity
        totals[movement["flavor_name"]] = totals.get(movement["flavor_name"], 0) + delta
    print("\nNet stock per flavor (compare against the AppSheet dashboard):")
    for name in sorted(totals):
        print(f"  {name}: {totals[name]}")

    print("\nNext steps:")
    print("  - Run inventory_system.py and review the startup warnings.")
    print("  - Option 5: set the real minimum_stock per flavor (all start at 0).")
    print("  - The program's rename (option 5) refuses to merge two existing")
    print("    flavors, on purpose. If you spot another duplicate later, fix")
    print("    its rows one by one with option 4, or move movements.csv away")
    print("    and re-run this import.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nImport cancelled. Nothing was written.")
