import json
import os
import logging
from datetime import datetime

# ------------------------------------------------------------
# LOGGING SETUP (Project #10) — FIXED FOR FLAT REPO STRUCTURE
# ------------------------------------------------------------
# Since inventory_system.py lives at the repo root, logs/ goes right next to it.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_filename = os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    filename=log_filename,
    level=logging.INFO,
    format="%(asctime)s — [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    filemode="a"
)

logger = logging.getLogger(__name__)

DATA_FILE = os.path.join(SCRIPT_DIR, "inventory_data.json")


def load_inventory():
    """Loads the stored inventory. Acts as the IPO's 'database' source."""
    if not os.path.exists(DATA_FILE):
        logger.info("No existing inventory file found at %s. Starting fresh.", DATA_FILE)
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            logger.info("Inventory loaded successfully from %s (%d flavors)", DATA_FILE, len(data))
            return data
    except json.JSONDecodeError as e:
        logger.error("Failed to parse inventory file %s: %s", DATA_FILE, e)
        return {}
    except Exception as e:
        logger.error("Unexpected error loading inventory: %s", e)
        return {}


def save_inventory(inventory):
    """Persists the inventory back to disk after each movement."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as file:
            json.dump(inventory, file, indent=4, ensure_ascii=False)
        logger.info("Inventory saved to %s", DATA_FILE)
    except Exception as e:
        logger.error("Failed to save inventory to %s: %s", DATA_FILE, e)


def process_movement(movement_type, bottle_quantity, previous_stock, minimum_stock):
    """
    Core business logic. Mirrors the approved IPO PROCESS block exactly.
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


def display_result(flavor_name, lot_number, current_stock, transaction_status, stock_status):
    """Prints the transaction result exactly as defined in the IPO OUTPUT block."""
    print(f"Flavor: {flavor_name}")
    print(f"Lot Number: {lot_number}")
    print(f"Updated Stock: {current_stock}")
    print(f"Transaction: {transaction_status}")
    print(f"Inventory Status: {stock_status}")


def register_movement(inventory):
    """Collects INPUT values and runs one full movement transaction."""
    flavor_name = input("Flavor name: ").strip()
    lot_number = input("Lot number: ").strip()
    movement_type = input("Movement type (entry/exit): ").strip().lower()

    try:
        bottle_quantity = int(input("Bottle quantity: ").strip())
    except ValueError:
        print("Error: bottle quantity must be a whole number.\n")
        logger.error("Invalid bottle quantity input for flavor '%s'", flavor_name)
        return

    if bottle_quantity <= 0:
        print("Error: bottle quantity must be greater than zero.\n")
        logger.error("Bottle quantity must be > 0, got %d for '%s'", bottle_quantity, flavor_name)
        return

    if flavor_name in inventory:
        previous_stock = inventory[flavor_name]["current_stock"]
        minimum_stock = inventory[flavor_name]["minimum_stock"]
        logger.info("Existing flavor '%s' selected. Previous stock: %d", flavor_name, previous_stock)
    else:
        previous_stock = 0
        print(f"'{flavor_name}' is a new flavor.")
        logger.warning("New flavor '%s' created", flavor_name)
        try:
            minimum_stock = int(input("Set its minimum stock level: ").strip())
        except ValueError:
            print("Error: minimum stock must be a whole number.\n")
            logger.error("Invalid minimum stock input for new flavor '%s'", flavor_name)
            return

    current_stock, transaction_status, stock_status = process_movement(
        movement_type, bottle_quantity, previous_stock, minimum_stock
    )

    # Log transaction outcome
    if transaction_status == "Success":
        logger.info("Movement successful: %s %s | Qty: %d | Stock: %d → %d",
                    movement_type, flavor_name, bottle_quantity, previous_stock, current_stock)
    else:
        logger.error("Movement failed: %s %s | Qty: %d | Reason: %s",
                     movement_type, flavor_name, bottle_quantity, transaction_status)

    # Log stock alert
    if stock_status == "LOW STOCK":
        logger.warning("LOW STOCK for '%s': current=%d, minimum=%d",
                       flavor_name, current_stock, minimum_stock)

    inventory[flavor_name] = {
        "lot_number": lot_number,
        "current_stock": current_stock,
        "minimum_stock": minimum_stock,
    }
    save_inventory(inventory)

    print()
    display_result(flavor_name, lot_number, current_stock, transaction_status, stock_status)
    print()


def view_inventory(inventory):
    """Displays a summary table of every flavor currently tracked."""
    if not inventory:
        print("Inventory is empty.\n")
        logger.info("Inventory view: empty")
        return

    print(f"{'Flavor':<20}{'Lot Number':<18}{'Stock':<10}{'Minimum':<10}{'Status'}")
    print("-" * 68)
    for flavor_name, record in inventory.items():
        stock_status = "LOW STOCK" if record["current_stock"] <= record["minimum_stock"] else "OK"
        print(f"{flavor_name:<20}{record['lot_number']:<18}{record['current_stock']:<10}{record['minimum_stock']:<10}{stock_status}")
    print()
    logger.info("Inventory view displayed (%d flavors)", len(inventory))


def main():
    logger.info("=== El Fermentario Inventory System Started ===")
    inventory = load_inventory()

    while True:
        print("=== El Fermentario - Kombucha Inventory Control ===")
        print("1. Register a movement (entry / exit)")
        print("2. View current inventory")
        print("3. Exit")
        choice = input("Choose an option: ").strip()
        print()

        if choice == "1":
            register_movement(inventory)
        elif choice == "2":
            logger.info("User requested inventory view")
            view_inventory(inventory)
        elif choice == "3":
            logger.info("User exited the system")
            print("Closing the system. See you soon!")
            break
        else:
            logger.warning("Invalid menu option selected: '%s'", choice)
            print("Invalid option. Please choose 1, 2, or 3.\n")


if __name__ == "__main__":
    main()