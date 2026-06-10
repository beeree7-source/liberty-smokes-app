import csv
import re
from pathlib import Path

OUT_DIR = Path(r"c:\Users\beere\OneDrive\Desktop\Liberty Files\Price Sheets\cleaned_output")

PRICE_IN_NAME_RE = re.compile(r"\$\s*\d")
PACKING_IN_NAME_RE = re.compile(r"\b(Box|Cube|Boat|Tin|Pack)\s+of\s+\d+\b", re.IGNORECASE)
SIZE_IN_NAME_RE = re.compile(r"\b\d+\.?\d*\s*[xX]\s*\d+\.?\d*\b")
ADDRESS_RE = re.compile(r"philadelphia|\bpa\b|\b\d{5}\b", re.IGNORECASE)


def to_float(v):
    try:
        return float(str(v or "").replace(",", "").strip())
    except Exception:
        return 0.0


def to_int(v):
    try:
        return int(float(str(v or "").replace(",", "").strip()))
    except Exception:
        return 0


def audit_file(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    issues = {
        "rows": len(rows),
        "empty_product": 0,
        "price_in_name": 0,
        "packing_in_name": 0,
        "size_in_name": 0,
        "address_like_name": 0,
        "box_price_nonpositive": 0,
        "box_price_suspicious_high": 0,
        "missing_source": 0,
        "duplicate_keys": 0,
    }

    seen = set()

    for r in rows:
        product = str(r.get("Product") or "").strip()
        sku = str(r.get("SKU") or "").strip()
        box_price = to_float(r.get("Box Price"))
        source = str(r.get("Price Source") or "").strip()

        if not product:
            issues["empty_product"] += 1
        if PRICE_IN_NAME_RE.search(product):
            issues["price_in_name"] += 1
        if PACKING_IN_NAME_RE.search(product):
            issues["packing_in_name"] += 1
        if SIZE_IN_NAME_RE.search(product):
            issues["size_in_name"] += 1
        if ADDRESS_RE.search(product):
            if "box" not in product.lower() and "cigar" not in product.lower():
                issues["address_like_name"] += 1

        if box_price <= 0:
            issues["box_price_nonpositive"] += 1
        if box_price > 2000:
            issues["box_price_suspicious_high"] += 1

        if not source:
            issues["missing_source"] += 1

        key = (sku.lower(), product.lower(), round(box_price, 2))
        if key in seen:
            issues["duplicate_keys"] += 1
        seen.add(key)

    return issues


def main():
    files = sorted([p for p in OUT_DIR.glob("*.csv") if p.name.lower() != "_parse_summary.csv"])
    if not files:
        print("No output CSV files found.")
        return

    print("file,rows,empty_product,price_in_name,packing_in_name,size_in_name,address_like_name,box_price_nonpositive,box_price_suspicious_high,missing_source,duplicate_keys")
    totals = {
        "rows": 0,
        "empty_product": 0,
        "price_in_name": 0,
        "packing_in_name": 0,
        "size_in_name": 0,
        "address_like_name": 0,
        "box_price_nonpositive": 0,
        "box_price_suspicious_high": 0,
        "missing_source": 0,
        "duplicate_keys": 0,
    }

    for p in files:
        a = audit_file(p)
        for k in totals:
            totals[k] += a[k]
        print(
            f"{p.name},{a['rows']},{a['empty_product']},{a['price_in_name']},{a['packing_in_name']},{a['size_in_name']},{a['address_like_name']},{a['box_price_nonpositive']},{a['box_price_suspicious_high']},{a['missing_source']},{a['duplicate_keys']}"
        )

    print(
        f"TOTAL,{totals['rows']},{totals['empty_product']},{totals['price_in_name']},{totals['packing_in_name']},{totals['size_in_name']},{totals['address_like_name']},{totals['box_price_nonpositive']},{totals['box_price_suspicious_high']},{totals['missing_source']},{totals['duplicate_keys']}"
    )


if __name__ == "__main__":
    main()
