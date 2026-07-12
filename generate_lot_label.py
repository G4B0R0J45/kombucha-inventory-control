"""
QR label generator for El Fermentario.

Two label types, matching the two prompts in inventory_system.py:

  LOT labels     QR payload = the lot code alone (e.g. "LOT202606-K114").
                 Scanning one at any lot prompt "types" the exact code the
                 program expects. Backwards compatible with labels already
                 printed.
  FLAVOR labels  QR payload = "FLAVOR:<name>" (e.g. "FLAVOR:Jamaica").
                 Scanning one at the flavor prompt selects that flavor.
                 The prefix is what keeps the two types safe: a flavor
                 label scanned at a lot prompt is rejected with a clear
                 message instead of becoming a bogus lot code.

The flavor is always printed as human-readable text on the label too, so
staff can tell labels apart without scanning them.

Requires:  pip install qrcode[pil]
Output:    labels/label_<flavor>_<lot>.png        (lot labels)
           labels/flavor_label_<flavor>.png       (flavor labels)
"""
import json
import os
import sys

try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing dependency. Install it with:")
    print("  pip install qrcode[pil]")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "inventory_data.json")
LABELS_DIR = os.path.join(SCRIPT_DIR, "labels")

# Must match inventory_system.py: payload prefix that marks a FLAVOR label.
FLAVOR_QR_PREFIX = "FLAVOR:"


def load_flavor_names():
    """Reads the catalog (if present) so labels use the exact flavor spelling."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
        return sorted(data.get("flavors", {}))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []


def pick_flavor(flavor_names):
    """Numbered pick from the catalog; free text is also accepted."""
    if not flavor_names:
        return input("Flavor name (Enter to quit): ").strip()
    print("Flavors:")
    for position, name in enumerate(flavor_names, start=1):
        print(f"{position}. {name}")
    choice = input("Choose a number, type a flavor name, or Enter to quit: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(flavor_names):
        return flavor_names[int(choice) - 1]
    if choice and choice not in flavor_names:
        print(f"Note: '{choice}' is not in the catalog; the label will only "
              "scan correctly once that flavor exists.")
    return choice


def load_font(size):
    """DejaVu when available (Linux); Pillow's default font otherwise."""
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def safe_filename(text):
    """Keeps letters, digits, dashes and underscores for the file name."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


def make_label(payload, top_line, bottom_line, filename):
    """Builds one PNG: QR (encoding `payload`) + two readable caption lines."""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    caption_height = 70
    width = max(qr_image.width, 320)
    label = Image.new("RGB", (width, qr_image.height + caption_height), "white")
    label.paste(qr_image, ((width - qr_image.width) // 2, 0))

    draw = ImageDraw.Draw(label)
    lines = ((top_line, load_font(26), 4), (bottom_line, load_font(20), 38))
    for text, font, offset in lines:
        text_width = draw.textlength(text, font=font)
        draw.text(((width - text_width) // 2, qr_image.height + offset),
                  text, fill="black", font=font)

    os.makedirs(LABELS_DIR, exist_ok=True)
    path = os.path.join(LABELS_DIR, filename)
    label.save(path)
    return path


def generate_lot_labels(flavor_names):
    """One label per lot: QR payload is the lot code alone."""
    while True:
        flavor_name = pick_flavor(flavor_names)
        if not flavor_name:
            return
        while True:
            lot_number = input("Lot code (Enter to finish this flavor): ").strip()
            if not lot_number:
                break
            if lot_number.upper().startswith(FLAVOR_QR_PREFIX):
                print(f"Error: lot codes must not start with '{FLAVOR_QR_PREFIX}' "
                      "(that prefix is reserved for flavor labels).")
                continue
            filename = (f"label_{safe_filename(flavor_name)}_"
                        f"{safe_filename(lot_number)}.png")
            try:
                path = make_label(lot_number, flavor_name, lot_number, filename)
            except OSError as e:
                print(f"ERROR: the label could not be saved ({e}).")
                continue
            print(f"Saved: {path}")
        again = input("Another flavor? (y/n): ").strip().lower()
        if again != "y":
            return


def generate_flavor_labels(flavor_names):
    """One label per flavor: QR payload is FLAVOR:<name>."""
    while True:
        flavor_name = pick_flavor(flavor_names)
        if not flavor_name:
            return
        filename = f"flavor_label_{safe_filename(flavor_name)}.png"
        try:
            path = make_label(FLAVOR_QR_PREFIX + flavor_name,
                              flavor_name, "FLAVOR", filename)
        except OSError as e:
            print(f"ERROR: the label could not be saved ({e}).")
            continue
        print(f"Saved: {path}")
        again = input("Another flavor label? (y/n): ").strip().lower()
        if again != "y":
            return


def main():
    print("=== El Fermentario - QR labels ===")
    flavor_names = load_flavor_names()
    print("1. Lot labels (QR = lot code; scan at any lot prompt)")
    print("2. Flavor labels (QR = FLAVOR:<name>; scan at the flavor prompt)")
    choice = input("Choose an option: ").strip()
    if choice == "1":
        generate_lot_labels(flavor_names)
    elif choice == "2":
        generate_flavor_labels(flavor_names)
    else:
        print("Invalid option.")
        return
    print("Done.")


if __name__ == "__main__":
    main()
