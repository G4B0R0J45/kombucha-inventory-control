import json
import csv
import os
import logging
from datetime import datetime

# ------------------------------------------------------------
# LOGGING SETUP (Project #10) — FIXED FOR FLAT REPO STRUCTURE
# ------------------------------------------------------------
# Since inventory_system.py lives at the repo root, logs/ goes right next to it.
# If the log folder or file cannot be used, the program falls back to console
# logging instead of crashing before it even starts.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log")
    logging.basicConfig(
        filename=log_filename,
        level=logging.INFO,
        format="%(asctime)s — [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        filemode="a"
    )
except OSError as e:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s — [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    print(f"WARNING: could not open the log file ({e}). Logging to console instead.\n")

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# DATA FILES
# ------------------------------------------------------------
# movements.csv       -> SOURCE OF TRUTH. Every successful movement is one row:
#                        movement_id, date_time, staff_name, flavor_name,
#                        lot_number, movement_type, bottle_quantity, note.
#                        Current stock is always derived by replaying this file.
# inventory_data.json -> CATALOG only: flavors (with their minimum_stock) and
#                        the staff list. It does not store stock.
#
# Error-handling policy for both files:
#   1. Never overwrite data silently.
#   2. Never compute stock from data that failed validation.
#   3. Always tell the user exactly what is wrong and how to fix it.
#
# CSV files are read with utf-8-sig and rewritten with utf-8-sig so that a
# file saved by Excel ("CSV UTF-8", which adds an invisible BOM) still loads,
# and so that Excel displays accented flavor names correctly.
DATA_FILE = os.path.join(SCRIPT_DIR, "inventory_data.json")
MOVEMENTS_FILE = os.path.join(SCRIPT_DIR, "movements.csv")

CSV_FIELDS = ["movement_id", "date_time", "staff_name", "flavor_name",
              "lot_number", "movement_type", "bottle_quantity", "note"]
LEGACY_CSV_FIELDS = CSV_FIELDS[:-1]  # file format before the 'note' column
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Accepted when READING dates for reports: rows edited by hand or re-saved
# by Excel may not match the exact format the program writes.
DATE_PARSE_FORMATS = [DATE_FORMAT, "%Y-%m-%d %H:%M",
                      "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"]
HISTORY_PAGE_SIZE = 15

# QR label conventions (shared with generate_lot_label.py):
#   - LOT labels encode the lot code alone, so a scan works as-is at any
#     lot prompt (backwards compatible with labels already printed).
#   - FLAVOR labels encode "FLAVOR:<name>". The prefix is what makes two
#     label types safe: a flavor label scanned at a lot prompt is detected
#     and rejected instead of silently becoming a bogus lot code.
FLAVOR_QR_PREFIX = "FLAVOR:"


def looks_like_flavor_label(text):
    """True when the text carries the flavor-label QR prefix."""
    return text.upper().startswith(FLAVOR_QR_PREFIX)


class InventoryDataError(Exception):
    """Raised when a data file exists but its content cannot be trusted.
    The program refuses to run on corrupted data instead of guessing."""


# ------------------------------------------------------------
# CATALOG PERSISTENCE (flavors + staff)
# ------------------------------------------------------------
def validate_catalog(data):
    """Returns a list of structural problems found in the catalog."""
    problems = []

    flavors = data.get("flavors")
    if not isinstance(flavors, dict):
        problems.append("'flavors' must be an object of flavor_name: {minimum_stock}.")
    else:
        for flavor_name, record in flavors.items():
            if not isinstance(record, dict):
                problems.append(f"Flavor '{flavor_name}' must be an object.")
                continue
            minimum_stock = record.get("minimum_stock")
            if not isinstance(minimum_stock, int) or minimum_stock < 0:
                problems.append(f"Flavor '{flavor_name}' needs an integer 'minimum_stock' >= 0.")

    staff = data.get("staff")
    if not isinstance(staff, list) or any(not isinstance(name, str) or not name.strip()
                                          for name in staff):
        problems.append("'staff' must be a list of non-empty names.")

    return problems


def load_catalog():
    """Loads and validates the catalog.
    Raises InventoryDataError when the file exists but cannot be trusted."""
    if not os.path.exists(DATA_FILE):
        logger.info("No catalog file found at %s. Starting fresh.", DATA_FILE)
        return {"flavors": {}, "staff": []}

    try:
        with open(DATA_FILE, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse catalog file %s: %s", DATA_FILE, e)
        raise InventoryDataError(
            f"{DATA_FILE} is not valid JSON ({e}).\n"
            "Fix the file by hand or restore it from git, then run the program again."
        )
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Could not read catalog file %s: %s", DATA_FILE, e)
        raise InventoryDataError(f"Could not read {DATA_FILE}: {e}")

    if isinstance(data, dict) and "flavors" in data:
        data.setdefault("staff", [])
        problems = validate_catalog(data)
        if problems:
            logger.error("Catalog validation failed: %s", problems)
            raise InventoryDataError(
                f"{DATA_FILE} has structural problems:\n  - " + "\n  - ".join(problems) +
                "\nFix the file by hand or restore it from git."
            )
        logger.info("Catalog loaded from %s (%d flavors, %d staff)",
                    DATA_FILE, len(data["flavors"]), len(data["staff"]))
        return data

    if isinstance(data, dict) and any(isinstance(record, dict) and "current_stock" in record
                                      for record in data.values()):
        # The pre-Unit-III format stored the stock inside the JSON. It was
        # only ever used with test data, so it is rejected instead of migrated.
        logger.error("Old pre-history inventory format detected in %s", DATA_FILE)
        raise InventoryDataError(
            f"{DATA_FILE} uses the old format (stock stored in the JSON). This version "
            "does not migrate it: delete inventory_data.json and movements.csv to start "
            "fresh, or restore a current pair of files from git."
        )

    raise InventoryDataError(f"{DATA_FILE} does not look like a catalog.")


def save_catalog(catalog):
    """Atomically persists the catalog: writes a temp file first and then
    replaces the real one, so a crash mid-write cannot corrupt it.
    Returns True on success; on failure it warns the user and returns False."""
    temp_path = DATA_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(catalog, file, indent=4, ensure_ascii=False)
        os.replace(temp_path, DATA_FILE)
        logger.info("Catalog saved to %s", DATA_FILE)
        return True
    except OSError as e:
        logger.error("Failed to save catalog to %s: %s", DATA_FILE, e)
        print(f"ERROR: the catalog could not be saved ({e}).")
        return False


# ------------------------------------------------------------
# MOVEMENT HISTORY PERSISTENCE (source of truth)
# ------------------------------------------------------------
def validate_movement_row(row, row_number, seen_ids):
    """Validates one CSV row. Returns an error description, or None if valid.
    Valid movement_ids are registered in seen_ids to detect duplicates.
    The note column is free text and may be empty."""
    if any(row.get(field) is None for field in CSV_FIELDS) or row.get(None) is not None:
        return f"Row {row_number}: wrong number of columns."

    movement_id = row["movement_id"].strip()
    if not movement_id.isdigit() or int(movement_id) <= 0:
        return f"Row {row_number}: movement_id '{movement_id}' must be a positive integer."
    normalized_id = str(int(movement_id))
    if normalized_id in seen_ids:
        return f"Row {row_number}: duplicated movement_id '{normalized_id}'."
    seen_ids.add(normalized_id)

    if not row["date_time"].strip():
        return f"Row {row_number}: date_time is empty."
    if not row["staff_name"].strip():
        return f"Row {row_number}: staff_name is empty."
    if not row["flavor_name"].strip():
        return f"Row {row_number}: flavor_name is empty."
    if row["movement_type"] not in ("entry", "exit"):
        return (f"Row {row_number}: movement_type '{row['movement_type']}' "
                "must be 'entry' or 'exit'.")

    quantity = row["bottle_quantity"].strip()
    if not quantity.isdigit() or int(quantity) <= 0:
        return (f"Row {row_number}: bottle_quantity '{quantity}' "
                "must be an integer greater than zero.")

    if row["movement_type"] == "entry" and not row["lot_number"].strip():
        return f"Row {row_number}: an entry needs a lot_number."

    if looks_like_flavor_label(row["lot_number"]):
        return (f"Row {row_number}: lot_number '{row['lot_number']}' starts with "
                f"'{FLAVOR_QR_PREFIX}', a prefix reserved for flavor QR labels.")

    return None


def load_movements():
    """Loads and validates the full movement history. Because this file is the
    source of truth, the program refuses to run if any row is invalid: it
    reports every problem (row by row) instead of computing wrong stock.
    An empty or missing file is not an error: the header is (re)created.
    A legacy file without the 'note' column is upgraded automatically."""
    try:
        if not os.path.exists(MOVEMENTS_FILE) or os.path.getsize(MOVEMENTS_FILE) == 0:
            with open(MOVEMENTS_FILE, "w", newline="", encoding="utf-8-sig") as file:
                csv.DictWriter(file, fieldnames=CSV_FIELDS).writeheader()
            logger.info("Created new movements file at %s", MOVEMENTS_FILE)
            return []
    except OSError as e:
        logger.error("Could not create movements file: %s", e)
        raise InventoryDataError(f"Could not create {MOVEMENTS_FILE}: {e}")

    try:
        with open(MOVEMENTS_FILE, "r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            header = reader.fieldnames
            movements = list(reader)
    except csv.Error as e:
        logger.error("Movements file is not readable as CSV: %s", e)
        raise InventoryDataError(f"{MOVEMENTS_FILE} could not be parsed as CSV ({e}).")
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Could not read movements file: %s", e)
        raise InventoryDataError(
            f"Could not read {MOVEMENTS_FILE}: {e}\n"
            "If the file was edited with Excel, save it again as 'CSV UTF-8'."
        )

    if header == LEGACY_CSV_FIELDS:
        # File created before the 'note' column existed: upgrade it in place.
        damaged = [str(row_number) for row_number, row in enumerate(movements, start=2)
                   if row.get(None) is not None
                   or any(row.get(field) is None for field in LEGACY_CSV_FIELDS)]
        if damaged:
            raise InventoryDataError(
                f"{MOVEMENTS_FILE} uses the legacy format AND has rows with a wrong "
                f"number of columns (rows: {', '.join(damaged)}). Fix those rows so the "
                "automatic upgrade can run."
            )
        for row in movements:
            row["note"] = ""
        if not save_movements(movements):
            raise InventoryDataError(
                f"{MOVEMENTS_FILE} needs the new 'note' column but could not be rewritten."
            )
        print("movements.csv upgraded: 'note' column added.\n")
        logger.warning("Movements file upgraded with the 'note' column (%d rows)",
                       len(movements))
        header = CSV_FIELDS

    if header != CSV_FIELDS:
        logger.error("Movements file has unexpected header: %s", header)
        raise InventoryDataError(
            f"{MOVEMENTS_FILE} has an unexpected header.\n"
            f"Expected: {','.join(CSV_FIELDS)}\n"
            f"Found:    {','.join(header) if header else '(none)'}"
        )

    problems = []
    seen_ids = set()
    for row_number, row in enumerate(movements, start=2):  # line 1 is the header
        for field in CSV_FIELDS:
            if row.get(field) is not None:
                row[field] = row[field].strip()
        problem = validate_movement_row(row, row_number, seen_ids)
        if problem:
            problems.append(problem)
        else:
            row["movement_id"] = str(int(row["movement_id"]))

    if problems:
        logger.error("Movement history validation failed (%d problems)", len(problems))
        raise InventoryDataError(
            f"{MOVEMENTS_FILE} has invalid rows. It is the source of truth, so the "
            "program will not run until they are fixed:\n  - " + "\n  - ".join(problems) +
            "\nOpen the file, correct those rows (or restore it from git) and run again."
        )

    logger.info("Movement history loaded (%d movements)", len(movements))
    return movements


def save_movements(movements):
    """Atomically rewrites the whole history file (used after corrections or
    renames). Returns True on success; warns the user and returns False on
    failure, so callers can keep memory and disk consistent."""
    temp_path = MOVEMENTS_FILE + ".tmp"
    try:
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(movements)
        os.replace(temp_path, MOVEMENTS_FILE)
        logger.info("Movement history rewritten (%d movements)", len(movements))
        return True
    except OSError as e:
        logger.error("Failed to save movements file: %s", e)
        print(f"ERROR: the movement history could not be saved ({e}).")
        return False


def append_movement(movement):
    """Appends one successful movement to the history file. Plain utf-8 here:
    appending with utf-8-sig would inject a BOM in the middle of the file.
    Returns True on success and False on failure (the caller rolls back)."""
    try:
        with open(MOVEMENTS_FILE, "a", newline="", encoding="utf-8") as file:
            csv.DictWriter(file, fieldnames=CSV_FIELDS).writerow(movement)
        logger.info("Movement %s appended to history", movement["movement_id"])
        return True
    except OSError as e:
        logger.error("Failed to append movement: %s", e)
        return False


def next_movement_id(movements):
    """Sequential id for a new movement: highest existing id + 1."""
    if not movements:
        return 1
    return max(int(movement["movement_id"]) for movement in movements) + 1


# ------------------------------------------------------------
# CORE LOGIC
# ------------------------------------------------------------
def compute_stock(movements):
    """
    Derives the current stock by replaying the whole history. Entries add
    bottles to their lot; exits consume bottles from the OLDEST lots first
    (FIFO — Python dicts keep insertion order, so lot order = arrival order).
    Rows are already validated by load_movements(), so values can be trusted.
    Returns (stock, inconsistencies) where:
        stock = { flavor_name: {"lots": {lot_number: quantity}, "total": int} }
    An inconsistency means some exit could not be fully covered by the stock
    available at that point of the history (only possible after bad edits).
    """
    stock = {}
    inconsistencies = []

    for movement in movements:
        flavor_name = movement["flavor_name"]
        record = stock.setdefault(flavor_name, {"lots": {}, "total": 0})
        quantity = int(movement["bottle_quantity"])

        if movement["movement_type"] == "entry":
            lot_number = movement["lot_number"]
            record["lots"][lot_number] = record["lots"].get(lot_number, 0) + quantity
            record["total"] += quantity
        else:  # exit -> FIFO over this flavor's lots
            remaining = quantity
            for lot_number in list(record["lots"]):
                if remaining == 0:
                    break
                taken = min(record["lots"][lot_number], remaining)
                record["lots"][lot_number] -= taken
                remaining -= taken
                if record["lots"][lot_number] == 0:
                    del record["lots"][lot_number]
            record["total"] -= quantity - remaining
            if remaining > 0:
                inconsistencies.append(
                    f"Movement {movement['movement_id']}: exit of {quantity} "
                    f"'{flavor_name}' exceeds the stock available at that point by {remaining}."
                )
    return stock, inconsistencies


def get_flavor_stock(stock, flavor_name):
    """Safe accessor: returns the {'lots', 'total'} record for a flavor."""
    return stock.get(flavor_name, {"lots": {}, "total": 0})


def process_movement(movement_type, bottle_quantity, previous_stock, minimum_stock):
    """
    Core business logic. Mirrors the approved IPO PROCESS block exactly.
    previous_stock and minimum_stock are per FLAVOR (sum of all its lots).
    Returns a tuple: (current_stock, transaction_status, stock_status).
    """
    # Step 1 - Validate movement type
    if movement_type == "entry":
        current_stock = previous_stock + bottle_quantity
        transaction_status = "Success"
    elif movement_type == "exit":
        if bottle_quantity <= previous_stock:
            current_stock = previous_stock - bottle_quantity
            transaction_status = "Success"
        else:
            current_stock = previous_stock
            transaction_status = "Error: Insufficient stock"
    else:
        # Hardening beyond the IPO: guards against a typo'd movement_type.
        current_stock = previous_stock
        transaction_status = "Error: Invalid movement type"

    # Step 2 - Evaluate stock alert
    if current_stock <= minimum_stock:
        stock_status = "LOW STOCK"
    else:
        stock_status = "OK"

    return current_stock, transaction_status, stock_status


def display_result(flavor_name, lot_display, current_stock, transaction_status, stock_status):
    """Prints the transaction result exactly as defined in the IPO OUTPUT block."""
    print(f"Flavor: {flavor_name}")
    print(f"Lot Number: {lot_display}")
    print(f"Updated Stock: {current_stock}")
    print(f"Transaction: {transaction_status}")
    print(f"Inventory Status: {stock_status}")


# ------------------------------------------------------------
# PICKERS (kill the typos: choose by number instead of typing)
# ------------------------------------------------------------
def normalize_movement_type(raw):
    """Maps user input to a canonical movement type. Single-letter shortcuts
    are accepted so 'entry'/'exit' never have to be typed in full.
    Returns 'entry', 'exit', or None when the input is not recognized."""
    text = raw.strip().lower()
    if text in ("e", "entry"):
        return "entry"
    if text in ("x", "exit"):
        return "exit"
    return None


def pick_movement_type():
    """Asks for the movement type and re-asks until the answer is valid, so
    an invalid type is caught HERE — the user never gets to type a quantity
    or a note for a movement that was already doomed."""
    while True:
        movement_type = normalize_movement_type(input("Movement type (e = entry / x = exit): "))
        if movement_type is not None:
            return movement_type
        print("Error: type 'e' (entry) or 'x' (exit).")


def find_flavor_case_insensitive(catalog, flavor_name):
    """Returns the existing flavor whose name matches ignoring case, or None.
    Prevents accidental near-duplicates like 'Jamaica' vs 'jamaica'."""
    lowered = flavor_name.lower()
    for existing in catalog["flavors"]:
        if existing.lower() == lowered:
            return existing
    return None


def create_flavor(catalog, persist=True):
    """Asks for a new flavor's name and minimum stock and adds it to the
    catalog in memory. Persists it only when persist=True (the registration
    flow defers saving until the movement succeeds). Returns name or None."""
    flavor_name = input("New flavor name: ").strip()
    if not flavor_name:
        print("Error: flavor name cannot be empty.\n")
        return None
    existing = find_flavor_case_insensitive(catalog, flavor_name)
    if existing:
        print(f"Error: a flavor with that name already exists: '{existing}'.\n")
        return None
    try:
        minimum_stock = int(input("Set its minimum stock level: ").strip())
    except ValueError:
        print("Error: minimum stock must be a whole number.\n")
        logger.error("Invalid minimum stock input for new flavor '%s'", flavor_name)
        return None
    if minimum_stock < 0:
        print("Error: minimum stock cannot be negative.\n")
        return None

    catalog["flavors"][flavor_name] = {"minimum_stock": minimum_stock}
    if persist:
        if not save_catalog(catalog):
            del catalog["flavors"][flavor_name]
            print("The flavor was NOT created.\n")
            return None
        logger.warning("New flavor '%s' created (minimum %d)", flavor_name, minimum_stock)
        print(f"Flavor '{flavor_name}' created.\n")
    return flavor_name


def discard_new_flavor(catalog, flavor_name, flavor_is_new):
    """Removes a not-yet-persisted flavor from memory when its first movement
    is aborted or fails, so no ghost flavors are left behind."""
    if flavor_is_new and flavor_name in catalog["flavors"]:
        del catalog["flavors"][flavor_name]


def pick_flavor(catalog):
    """Selects the flavor by number (with 5-20 flavors this is faster and
    kills typos). Returns (flavor_name, is_new); (None, False) if invalid."""
    flavors = sorted(catalog["flavors"])
    if not flavors:
        print("No flavors yet — let's create the first one.")
        flavor_name = create_flavor(catalog, persist=False)
        return (flavor_name, flavor_name is not None)

    print("Flavors:")
    for position, name in enumerate(flavors, start=1):
        print(f"{position}. {name}")
    choice_raw = input("Choose a flavor number, 'n' for a new flavor, or scan a code: ").strip()
    if choice_raw.lower() == "n":
        flavor_name = create_flavor(catalog, persist=False)
        return (flavor_name, flavor_name is not None)
    if looks_like_flavor_label(choice_raw):
        # Scanned flavor label: the QR payload is FLAVOR:<name>.
        scanned_name = choice_raw[len(FLAVOR_QR_PREFIX):].strip()
        existing = find_flavor_case_insensitive(catalog, scanned_name)
        if existing:
            return (existing, False)
        print(f"Error: the scanned flavor '{scanned_name}' is not in the catalog. "
              "Create it first ('n' here, or option 5).\n")
        return (None, False)
    try:
        index = int(choice_raw)
    except ValueError:
        # Not a number and not a flavor label: maybe a name typed by hand.
        existing = find_flavor_case_insensitive(catalog, choice_raw)
        if existing:
            return (existing, False)
        print("Error: choose a number from the list, 'n', or scan a FLAVOR label "
              "(a lot label does not identify the flavor).\n")
        return (None, False)
    if not 1 <= index <= len(flavors):
        print("Error: choose a valid number or 'n'.\n")
        return (None, False)
    return (flavors[index - 1], False)


def pick_lot(flavor_name, stock):
    """For entries: lists the flavor's lots that still have stock, so a batch
    can be topped up without retyping long, similar codes. 'n' asks for a new
    code. A full code typed or scanned with a QR reader is also accepted:
    if it exists it is selected, otherwise it can be used as the new code.
    Returns the lot number, or None if the input was invalid."""
    lots = get_flavor_stock(stock, flavor_name)["lots"]
    if not lots:
        lot_number = input("Lot number: ").strip()
        if not lot_number:
            print("Error: lot number cannot be empty for an entry.\n")
            return None
        if looks_like_flavor_label(lot_number):
            print("Error: that is a FLAVOR label; a lot code is needed here.\n")
            return None
        return lot_number

    print(f"Existing lots for '{flavor_name}':")
    for position, (lot_number, quantity) in enumerate(lots.items(), start=1):
        print(f"{position}. {lot_number} ({quantity} bottles)")
    choice_raw = input("Choose a lot number, 'n' for a new lot, or scan a code: ").strip()
    if not choice_raw:
        print("Error: choose a valid number, 'n', or a lot code.\n")
        return None
    if looks_like_flavor_label(choice_raw):
        print("Error: that is a FLAVOR label; scan the LOT label instead.\n")
        return None
    if choice_raw.lower() == "n":
        lot_number = input("New lot code: ").strip()
        if not lot_number:
            print("Error: lot number cannot be empty for an entry.\n")
            return None
        if looks_like_flavor_label(lot_number):
            print("Error: that is a FLAVOR label; a lot code is needed here.\n")
            return None
        return lot_number
    try:
        index = int(choice_raw)
    except ValueError:
        # Not a number: a lot code typed by hand or scanned with a QR reader.
        if choice_raw in lots:
            return choice_raw  # existing lot -> top it up directly
        confirm = input(f"Use '{choice_raw}' as a NEW lot code? (y/n): ").strip().lower()
        if confirm == "y":
            return choice_raw
        print("Movement cancelled.\n")
        return None
    if not 1 <= index <= len(lots):
        print("Error: choose a valid number, 'n', or a lot code.\n")
        return None
    return list(lots)[index - 1]


# ------------------------------------------------------------
# MOVEMENT REGISTRATION
# ------------------------------------------------------------
def pick_staff(catalog):
    """Selects who is registering the movement. With 2-3 people, a numbered
    pick avoids typos in the history. Returns the name, or None if invalid."""
    staff = catalog["staff"]
    if not staff:
        name = input("No staff registered yet. Your name: ").strip()
        if not name:
            print("Error: staff name cannot be empty.\n")
            return None
        staff.append(name)
        if not save_catalog(catalog):
            print("WARNING: the staff list could not be saved; "
                  "the name will be used for this session only.")
        logger.info("Staff member '%s' added (first run)", name)
        return name

    print("Who is registering this movement? (manage names in option 5)")
    for position, name in enumerate(staff, start=1):
        print(f"{position}. {name}")
    choice = input("Choose a number: ").strip()
    try:
        index = int(choice)
    except ValueError:
        print("Error: choose a valid number.\n")
        return None
    if not 1 <= index <= len(staff):
        print("Error: choose a valid number.\n")
        return None
    return staff[index - 1]


def register_movement(catalog, movements):
    """Collects INPUT values, runs one movement and appends it to the history.
    A brand-new flavor is only persisted if its movement succeeds; on any
    failure or abort it is discarded, so no ghost flavors are created. If the
    movement cannot be written to disk, it is rolled back from memory too."""
    staff_name = pick_staff(catalog)
    if staff_name is None:
        return

    flavor_name, flavor_is_new = pick_flavor(catalog)
    if flavor_name is None:
        return

    minimum_stock = catalog["flavors"][flavor_name]["minimum_stock"]

    movement_type = pick_movement_type()

    try:
        bottle_quantity = int(input("Bottle quantity: ").strip())
    except ValueError:
        print("Error: bottle quantity must be a whole number.\n")
        logger.error("Invalid bottle quantity input for flavor '%s'", flavor_name)
        discard_new_flavor(catalog, flavor_name, flavor_is_new)
        return
    if bottle_quantity <= 0:
        print("Error: bottle quantity must be greater than zero.\n")
        logger.error("Bottle quantity must be > 0, got %d for '%s'", bottle_quantity, flavor_name)
        discard_new_flavor(catalog, flavor_name, flavor_is_new)
        return

    # PROCESS: previous_stock is loaded from the history before calculating.
    # Computed here once so the lot picker can reuse the same snapshot.
    stock_before, _ = compute_stock(movements)

    if movement_type == "entry":
        lot_number = pick_lot(flavor_name, stock_before)
        if lot_number is None:
            discard_new_flavor(catalog, flavor_name, flavor_is_new)
            return
    else:
        lot_number = ""  # exits do not ask for a lot: FIFO decides

    note = input("Note (optional, Enter to skip): ").strip()

    previous_stock = get_flavor_stock(stock_before, flavor_name)["total"]

    current_stock, transaction_status, stock_status = process_movement(
        movement_type, bottle_quantity, previous_stock, minimum_stock
    )

    lot_display = lot_number if lot_number else "-"

    if transaction_status == "Success":
        if flavor_is_new:
            if not save_catalog(catalog):
                del catalog["flavors"][flavor_name]
                print("Movement cancelled: the new flavor could not be saved.\n")
                return
            logger.warning("New flavor '%s' created (minimum %d)", flavor_name, minimum_stock)

        movement = {
            "movement_id": str(next_movement_id(movements)),
            "date_time": datetime.now().strftime(DATE_FORMAT),
            "staff_name": staff_name,
            "flavor_name": flavor_name,
            "lot_number": lot_number,
            "movement_type": movement_type,
            "bottle_quantity": str(bottle_quantity),
            "note": note,
        }
        movements.append(movement)
        if not append_movement(movement):
            movements.pop()  # roll back: memory must match the file
            if flavor_is_new:
                del catalog["flavors"][flavor_name]
                save_catalog(catalog)  # best effort to undo the flavor on disk
            print("\nERROR: the movement could not be written to movements.csv, "
                  "so it was NOT registered.\n")
            return

        if movement_type == "exit":
            # FIFO breakdown: diff lots before vs after, to tell the user
            # which physical lots the bottles must be taken from.
            stock_after, _ = compute_stock(movements)
            lots_after = get_flavor_stock(stock_after, flavor_name)["lots"]
            breakdown = []
            for lot, qty_before in get_flavor_stock(stock_before, flavor_name)["lots"].items():
                taken = qty_before - lots_after.get(lot, 0)
                if taken > 0:
                    breakdown.append(f"{lot} ({taken})")
            lot_display = ", ".join(breakdown)

        logger.info("Movement successful: %s %s | Qty: %d | Stock: %d -> %d | By: %s",
                    movement_type, flavor_name, bottle_quantity,
                    previous_stock, current_stock, staff_name)
    else:
        if flavor_is_new:
            # The whole point of the fix: a failed movement must not leave
            # a brand-new flavor behind in the catalog.
            del catalog["flavors"][flavor_name]
            print(f"Note: the new flavor '{flavor_name}' was NOT created "
                  "because the movement failed.")
        logger.error("Movement failed: %s %s | Qty: %d | Reason: %s",
                     movement_type, flavor_name, bottle_quantity, transaction_status)

    if stock_status == "LOW STOCK":
        logger.warning("LOW STOCK for '%s': current=%d, minimum=%d",
                       flavor_name, current_stock, minimum_stock)

    print()
    display_result(flavor_name, lot_display, current_stock, transaction_status, stock_status)
    print()


# ------------------------------------------------------------
# VIEWS
# ------------------------------------------------------------
def view_inventory(catalog, movements):
    """Displays every flavor with its total stock, minimum and lot breakdown.
    Flavors found in the history but missing from the catalog are shown too,
    flagged, instead of being silently hidden."""
    stock, inconsistencies = compute_stock(movements)

    if not catalog["flavors"] and not stock:
        print("Inventory is empty.\n")
        logger.info("Inventory view: empty")
        return

    for problem in inconsistencies:
        print(f"WARNING (history inconsistency): {problem}")
    if inconsistencies:
        print()
        logger.warning("Inventory view found %d history inconsistencies", len(inconsistencies))

    # The flavor column stretches to the longest name (same idea as the
    # history view), so real names like 'Naranja con albahaca y clavo'
    # never break the alignment.
    name_width = max([19] + [len(name) for name in catalog["flavors"]]
                     + [len(name) for name in stock]) + 1
    print(f"{'Flavor':<{name_width}}{'Total':<10}{'Minimum':<10}{'Status'}")
    print("-" * (name_width + 30))
    for flavor_name in sorted(catalog["flavors"]):
        record = get_flavor_stock(stock, flavor_name)
        minimum_stock = catalog["flavors"][flavor_name]["minimum_stock"]
        stock_status = "LOW STOCK" if record["total"] <= minimum_stock else "OK"
        print(f"{flavor_name:<{name_width}}{record['total']:<10}{minimum_stock:<10}{stock_status}")
        for lot_number, quantity in record["lots"].items():
            print(f"    lot {lot_number}: {quantity}")

    for flavor_name in sorted(stock):
        if flavor_name in catalog["flavors"]:
            continue
        record = stock[flavor_name]
        if record["total"] == 0 and not record["lots"]:
            continue
        print(f"{flavor_name:<{name_width}}{record['total']:<10}{'-':<10}NOT IN CATALOG")
        for lot_number, quantity in record["lots"].items():
            print(f"    lot {lot_number}: {quantity}")

    print()
    logger.info("Inventory view displayed (%d flavors)", len(catalog["flavors"]))


def shorten(text, width):
    """Truncates long text so the history columns stay aligned."""
    return text if len(text) <= width else text[:width - 1] + "…"


def print_history_rows(rows):
    """Prints one aligned table of movement rows (notes truncated). The lot
    column stretches to the longest code being shown (capped at 24), so real
    codes like 'LOT202606-K114' are never cut off."""
    lot_width = min(24, max([10] + [len(m["lot_number"]) for m in rows]) + 2)
    print(f"{'ID':<5}{'Date / Time':<21}{'Staff':<10}{'Flavor':<14}"
          f"{'Lot':<{lot_width}}{'Type':<7}{'Qty':<5}{'Note'}")
    print("-" * (86 + lot_width))
    for movement in rows:
        lot_display = movement["lot_number"] if movement["lot_number"] else "-"
        note_display = movement["note"] if movement["note"] else "-"
        print(f"{movement['movement_id']:<5}{movement['date_time']:<21}"
              f"{shorten(movement['staff_name'], 9):<10}"
              f"{shorten(movement['flavor_name'], 13):<14}"
              f"{shorten(lot_display, lot_width - 1):<{lot_width}}"
              f"{movement['movement_type']:<7}"
              f"{movement['bottle_quantity']:<5}"
              f"{shorten(note_display, 24)}")


def view_history(movements, limit=HISTORY_PAGE_SIZE, interactive=True):
    """Displays the movement history. Only the last `limit` rows by default,
    so hundreds of movements do not flood the screen; the user can then show
    everything or filter by flavor."""
    if not movements:
        print("No movements registered yet.\n")
        logger.info("History view: empty")
        return

    recent = movements[-limit:]
    if len(movements) > limit:
        print(f"Showing the last {len(recent)} of {len(movements)} movements.")
    print_history_rows(recent)
    print()
    logger.info("History view displayed (%d of %d movements)", len(recent), len(movements))

    if not interactive or len(movements) <= limit:
        return
    choice = input("Enter = back | 'a' = show all | flavor name = filter: ").strip()
    print()
    if not choice:
        return
    if choice.lower() == "a":
        print_history_rows(movements)
        print()
        logger.info("Full history displayed (%d movements)", len(movements))
        return
    filtered = [m for m in movements if m["flavor_name"].lower() == choice.lower()]
    if filtered:
        print_history_rows(filtered)
        print()
        logger.info("History filtered by '%s' (%d movements)", choice, len(filtered))
    else:
        print(f"No movements for '{choice}'.\n")


# ------------------------------------------------------------
# SALES REPORT (derived from the history, like everything else)
# ------------------------------------------------------------
def movement_month(date_text):
    """Best-effort 'YYYY-MM' bucket for one movement. The program always
    writes DATE_FORMAT, but rows edited by hand or with Excel may come in
    other shapes; anything unreadable lands in the 'unknown' bucket instead
    of crashing the report."""
    for date_format in DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(date_text, date_format).strftime("%Y-%m")
        except ValueError:
            continue
    return "unknown"


def sales_report(movements):
    """Sales control derived from the source of truth: every EXIT counts as
    a sale (use the note field to flag exceptions such as breakage or
    samples). Shows total bottles out per flavor and per month."""
    exits = [m for m in movements if m["movement_type"] == "exit"]
    if not exits:
        print("No exits registered yet, so there are no sales to report.\n")
        logger.info("Sales report: no exits")
        return

    bottles_by_flavor = {}
    bottles_by_month = {}
    for movement in exits:
        quantity = int(movement["bottle_quantity"])
        flavor_name = movement["flavor_name"]
        month = movement_month(movement["date_time"])
        bottles_by_flavor[flavor_name] = bottles_by_flavor.get(flavor_name, 0) + quantity
        bottles_by_month[month] = bottles_by_month.get(month, 0) + quantity

    total_bottles = sum(bottles_by_flavor.values())

    print("SALES REPORT (every exit counts as a sale)")
    print()
    name_width = max([19] + [len(name) for name in bottles_by_flavor]) + 1
    print(f"{'By flavor':<{name_width}}{'Bottles'}")
    print("-" * (name_width + 10))
    for flavor_name, bottles in sorted(bottles_by_flavor.items(), key=lambda item: -item[1]):
        print(f"{flavor_name:<{name_width}}{bottles}")
    print()
    print(f"{'By month':<20}{'Bottles'}")
    print("-" * 30)
    months = sorted(month for month in bottles_by_month if month != "unknown")
    if "unknown" in bottles_by_month:
        months.append("unknown")
    for month in months:
        print(f"{month:<20}{bottles_by_month[month]}")
    print()
    print(f"Total: {total_bottles} bottles across {len(exits)} exit movements.")
    print()
    logger.info("Sales report displayed (%d exits, %d bottles)", len(exits), total_bottles)


# ------------------------------------------------------------
# HISTORY CORRECTION
# ------------------------------------------------------------
def edit_movement(catalog, movements):
    """
    Corrects or deletes one movement from the history. Because the history is
    the source of truth, the change is validated first: if the corrected
    history would leave any exit without enough stock, it is rejected. The
    file is saved BEFORE memory is updated, so both always stay in sync.
    Note: editing the date is documentary only — FIFO order follows the row
    order (the IDs), never the date, so a date fix cannot reshuffle stock.
    """
    if not movements:
        print("No movements to correct.\n")
        return

    view_history(movements, interactive=False)
    raw_id = input("Movement ID to correct (any ID; Enter to cancel): ").strip()
    if not raw_id:
        print()
        return

    target = None
    for movement in movements:
        if movement["movement_id"] == raw_id:
            target = movement
            break
    if target is None:
        print("Error: no movement with that ID.\n")
        return

    action = input("Edit (e) or delete (x) this movement?: ").strip().lower()

    if action == "x":
        confirm = input("Delete it permanently from the history? (y/n): ").strip().lower()
        if confirm != "y":
            print("Correction cancelled.\n")
            return
        candidate = [m for m in movements if m["movement_id"] != raw_id]
    elif action == "e":
        print("Press Enter to keep the current value.")
        edited = dict(target)

        date_raw = input(f"Date/time [{target['date_time']}] (YYYY-MM-DD HH:MM:SS): ").strip()
        if date_raw:
            try:
                parsed_date = datetime.strptime(date_raw, DATE_FORMAT)
            except ValueError:
                print("Error: the date must have the format YYYY-MM-DD HH:MM:SS.\n")
                return
            edited["date_time"] = parsed_date.strftime(DATE_FORMAT)

        staff_name = input(f"Staff name [{target['staff_name']}]: ").strip()
        if staff_name:
            edited["staff_name"] = staff_name

        flavor_name = input(f"Flavor name [{target['flavor_name']}]: ").strip()
        if flavor_name:
            canonical = find_flavor_case_insensitive(catalog, flavor_name)
            if canonical is None:
                print("Error: that flavor does not exist in the catalog. "
                      "Create or rename it first (option 5).\n")
                return
            edited["flavor_name"] = canonical

        movement_raw = input(f"Movement type (e = entry / x = exit) "
                             f"[{target['movement_type']}]: ").strip()
        if movement_raw:
            movement_type = normalize_movement_type(movement_raw)
            if movement_type is None:
                print("Error: movement type must be 'e' (entry) or 'x' (exit).\n")
                return
            edited["movement_type"] = movement_type

        if edited["movement_type"] == "entry":
            current_lot = target["lot_number"] if target["lot_number"] else "(none)"
            lot_number = input(f"Lot number [{current_lot}]: ").strip()
            if lot_number:
                if looks_like_flavor_label(lot_number):
                    print("Error: that is a FLAVOR label, not a lot code.\n")
                    return
                edited["lot_number"] = lot_number
            elif not target["lot_number"]:
                print("Error: an entry needs a lot number.\n")
                return
        else:
            edited["lot_number"] = ""  # exits carry no lot: FIFO decides

        quantity_raw = input(f"Bottle quantity [{target['bottle_quantity']}]: ").strip()
        if quantity_raw:
            try:
                bottle_quantity = int(quantity_raw)
            except ValueError:
                print("Error: bottle quantity must be a whole number.\n")
                return
            if bottle_quantity <= 0:
                print("Error: bottle quantity must be greater than zero.\n")
                return
            edited["bottle_quantity"] = str(bottle_quantity)

        current_note = target["note"] if target["note"] else "(none)"
        note_raw = input(f"Note [{current_note}] ('-' to clear): ").strip()
        if note_raw == "-":
            edited["note"] = ""
        elif note_raw:
            edited["note"] = note_raw

        candidate = [edited if m["movement_id"] == raw_id else m for m in movements]
    else:
        print("Error: choose 'e' or 'x'.\n")
        return

    # Validate the corrected history before persisting it. Only NEW problems
    # block the change (pre-existing ones should not lock the editor).
    _, problems_before = compute_stock(movements)
    _, problems_after = compute_stock(candidate)
    new_problems = [p for p in problems_after if p not in problems_before]
    if new_problems:
        print("Change rejected — it would make the history inconsistent:")
        for problem in new_problems:
            print(f"  - {problem}")
        print()
        logger.error("History correction rejected for movement %s (%d problems)",
                     raw_id, len(new_problems))
        return

    if not save_movements(candidate):
        print("The correction was NOT applied: the file on disk is unchanged.\n")
        return
    movements[:] = candidate
    print("History updated and stock recalculated.\n")
    logger.warning("History corrected: movement %s (%s)",
                   raw_id, "deleted" if action == "x" else "edited")


# ------------------------------------------------------------
# CATALOG EDITOR
# ------------------------------------------------------------
def rename_flavor(catalog, movements):
    """Renames a flavor and propagates the change to the whole history.
    The history is saved first; the catalog only changes if that succeeds."""
    old_name = input("Flavor to rename: ").strip()
    if old_name not in catalog["flavors"]:
        print("Error: that flavor does not exist.\n")
        return
    new_name = input("New name: ").strip()
    if not new_name:
        print("Error: new name cannot be empty.\n")
        return
    existing = find_flavor_case_insensitive(catalog, new_name)
    if existing and existing != old_name:
        print(f"Error: that name already exists ('{existing}'); "
              "renaming would merge two histories.\n")
        return

    candidate = []
    renamed = 0
    for movement in movements:
        if movement["flavor_name"] == old_name:
            updated = dict(movement)
            updated["flavor_name"] = new_name
            candidate.append(updated)
            renamed += 1
        else:
            candidate.append(movement)

    if not save_movements(candidate):
        print("Rename cancelled: nothing was changed.\n")
        return
    movements[:] = candidate
    catalog["flavors"][new_name] = catalog["flavors"].pop(old_name)
    if not save_catalog(catalog):
        print("WARNING: the history was renamed but the catalog file was not. "
              "It will fix itself with the next successful catalog save; "
              f"if you exit now, rename '{old_name}' to '{new_name}' in "
              f"{os.path.basename(DATA_FILE)} by hand.")
    print(f"Renamed '{old_name}' to '{new_name}' ({renamed} history rows updated).\n")
    logger.warning("Flavor renamed: '%s' -> '%s' (%d movements)", old_name, new_name, renamed)


def change_minimum_stock(catalog, movements):
    """Updates a flavor's minimum stock and shows the resulting status.
    If the catalog cannot be saved, the change is reverted in memory."""
    flavor_name = input("Flavor: ").strip()
    if flavor_name not in catalog["flavors"]:
        print("Error: that flavor does not exist.\n")
        return
    current_minimum = catalog["flavors"][flavor_name]["minimum_stock"]
    try:
        minimum_stock = int(input(f"New minimum stock [{current_minimum}]: ").strip())
    except ValueError:
        print("Error: minimum stock must be a whole number.\n")
        return
    if minimum_stock < 0:
        print("Error: minimum stock cannot be negative.\n")
        return

    catalog["flavors"][flavor_name]["minimum_stock"] = minimum_stock
    if not save_catalog(catalog):
        catalog["flavors"][flavor_name]["minimum_stock"] = current_minimum
        print("The minimum was NOT changed.\n")
        return
    stock, _ = compute_stock(movements)
    total = get_flavor_stock(stock, flavor_name)["total"]
    stock_status = "LOW STOCK" if total <= minimum_stock else "OK"
    print(f"Minimum updated. Current total: {total} -> Status: {stock_status}\n")
    logger.info("Minimum stock for '%s' set to %d", flavor_name, minimum_stock)


def delete_flavor(catalog, movements):
    """Deletes a flavor. If it has history rows, asks for strong confirmation.
    The history is saved first; the catalog only changes if that succeeds."""
    flavor_name = input("Flavor to delete: ").strip()
    if flavor_name not in catalog["flavors"]:
        print("Error: that flavor does not exist.\n")
        return

    related = [m for m in movements if m["flavor_name"] == flavor_name]
    if related:
        print(f"WARNING: '{flavor_name}' has {len(related)} movements in the history.")
        print("Deleting it will also erase those rows permanently.")
        confirm = input("Type the flavor name to confirm: ").strip()
        if confirm != flavor_name:
            print("Deletion cancelled.\n")
            return
        candidate = [m for m in movements if m["flavor_name"] != flavor_name]
        if not save_movements(candidate):
            print("Deletion cancelled: nothing was changed.\n")
            return
        movements[:] = candidate

    del catalog["flavors"][flavor_name]
    if not save_catalog(catalog):
        print("WARNING: the flavor's history rows were removed but the catalog file "
              "was not updated. It will fix itself with the next successful catalog save.")
    print(f"Flavor '{flavor_name}' deleted.\n")
    logger.warning("Flavor '%s' deleted (%d history rows removed)", flavor_name, len(related))


def manage_staff(catalog):
    """Adds or removes the people allowed to register movements.
    Every change is reverted in memory if it cannot be saved to disk."""
    while True:
        if catalog["staff"]:
            print("Registered staff:")
            for position, name in enumerate(catalog["staff"], start=1):
                print(f"{position}. {name}")
        else:
            print("No staff registered.")
        print("a. Add   r. Remove   b. Back")
        choice = input("Choose an option: ").strip().lower()
        print()

        if choice == "a":
            name = input("Name to add: ").strip()
            if not name:
                print("Error: name cannot be empty.\n")
            elif name in catalog["staff"]:
                print("That name is already registered.\n")
            else:
                catalog["staff"].append(name)
                if not save_catalog(catalog):
                    catalog["staff"].remove(name)
                    print("The name was NOT added.\n")
                    continue
                logger.info("Staff member '%s' added", name)
                print(f"'{name}' added.\n")
        elif choice == "r":
            raw = input("Number to remove: ").strip()
            try:
                index = int(raw)
            except ValueError:
                print("Error: choose a valid number.\n")
                continue
            if not 1 <= index <= len(catalog["staff"]):
                print("Error: choose a valid number.\n")
                continue
            removed = catalog["staff"].pop(index - 1)
            if not save_catalog(catalog):
                catalog["staff"].insert(index - 1, removed)
                print("The name was NOT removed.\n")
                continue
            logger.info("Staff member '%s' removed", removed)
            print(f"'{removed}' removed (past history rows keep the name).\n")
        elif choice == "b":
            return
        else:
            print("Invalid option.\n")


def edit_catalog(catalog, movements):
    """Submenu to manage the catalog: add flavors directly (no movement
    needed), fix names and minimums, delete flavors, manage staff."""
    while True:
        print("--- Catalog editor ---")
        print("1. Add a new flavor")
        print("2. Rename a flavor (updates the whole history)")
        print("3. Change a flavor's minimum stock")
        print("4. Delete a flavor")
        print("5. Manage staff")
        print("6. Back to main menu")
        choice = input("Choose an option: ").strip()
        print()

        if choice == "1":
            create_flavor(catalog, persist=True)
        elif choice == "2":
            rename_flavor(catalog, movements)
        elif choice == "3":
            change_minimum_stock(catalog, movements)
        elif choice == "4":
            delete_flavor(catalog, movements)
        elif choice == "5":
            manage_staff(catalog)
        elif choice == "6":
            return
        else:
            print("Invalid option. Please choose 1-6.\n")


# ------------------------------------------------------------
# STARTUP HEALTH CHECK
# ------------------------------------------------------------
def check_consistency(catalog, movements):
    """Startup report of problems that are legal in the files but deserve
    attention. It warns and explains how to fix them; it never blocks."""
    stock, inconsistencies = compute_stock(movements)

    for problem in inconsistencies:
        print(f"WARNING (history): {problem}")

    orphans = [flavor_name for flavor_name in sorted(stock)
               if flavor_name not in catalog["flavors"]
               and (stock[flavor_name]["total"] != 0 or stock[flavor_name]["lots"])]
    for flavor_name in orphans:
        print(f"WARNING: '{flavor_name}' appears in the history but not in the catalog. "
              "Add it in the catalog editor (option 5), or correct the history (option 4).")

    if inconsistencies or orphans:
        print()
        logger.warning("Startup check: %d inconsistencies, %d flavors missing from catalog",
                       len(inconsistencies), len(orphans))


# ------------------------------------------------------------
# MAIN MENU
# ------------------------------------------------------------
def main():
    logger.info("=== El Fermentario Inventory System Started ===")
    try:
        catalog = load_catalog()
        movements = load_movements()
    except InventoryDataError as e:
        print(f"DATA ERROR:\n{e}\n")
        print("The program stops here so no data gets overwritten or miscalculated.")
        logger.error("Startup aborted: %s", e)
        return

    check_consistency(catalog, movements)

    while True:
        print("=== El Fermentario - Kombucha Inventory Control ===")
        print("1. Register a movement (entry / exit)")
        print("2. View current inventory")
        print("3. View movement history")
        print("4. Correct a movement in the history")
        print("5. Edit catalog (flavors / minimums / staff)")
        print("6. Sales report (exits by flavor and month)")
        print("7. Exit")
        try:
            choice = input("Choose an option: ").strip()
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C or end of input at the menu = clean exit, never a traceback
            print("\nClosing the system. See you soon!")
            logger.info("Session ended with Ctrl+C or end of input")
            break
        print()

        try:
            if choice == "1":
                register_movement(catalog, movements)
            elif choice == "2":
                logger.info("User requested inventory view")
                view_inventory(catalog, movements)
            elif choice == "3":
                logger.info("User requested history view")
                view_history(movements)
            elif choice == "4":
                edit_movement(catalog, movements)
            elif choice == "5":
                edit_catalog(catalog, movements)
            elif choice == "6":
                logger.info("User requested sales report")
                sales_report(movements)
            elif choice == "7":
                logger.info("User exited the system")
                print("Closing the system. See you soon!")
                break
            else:
                logger.warning("Invalid menu option selected: '%s'", choice)
                print("Invalid option. Please choose 1, 2, 3, 4, 5, 6 or 7.\n")
        except (KeyboardInterrupt, EOFError):
            # Ctrl+C inside an operation cancels it and returns to the menu
            print("\nOperation cancelled. Back to the menu.\n")
            logger.warning("Operation cancelled by the user (Ctrl+C)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last safety net: the user never sees a raw traceback; the log does.
        logger.exception("Unexpected fatal error")
        print("\nUnexpected error. Technical details were saved to the log file.")
