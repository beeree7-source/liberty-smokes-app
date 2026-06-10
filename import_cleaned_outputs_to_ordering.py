import csv
from pathlib import Path

import app

INPUT_DIR = Path(r"c:\Users\beere\OneDrive\Desktop\Liberty Files\Price Sheets\cleaned_output")


def _to_float(value) -> float:
    try:
        return round(float(str(value or "").replace(",", "").strip() or "0"), 2)
    except Exception:
        return 0.0


def _to_int(value) -> int:
    try:
        return int(float(str(value or "").replace(",", "").strip() or "0"))
    except Exception:
        return 0


def main() -> None:
    pg = app.get_postgrest_client()

    files = sorted(
        [
            p
            for p in INPUT_DIR.glob("*.csv")
            if p.is_file() and p.name.lower() != "_parse_summary.csv"
        ]
    )

    if not files:
        print(f"No cleaned csv files found in: {INPUT_DIR}")
        return

    existing_companies = app.load_ordering_companies(pg)
    existing_reps = app.load_ordering_sales_reps(pg)

    company_map = {str(c.get("company") or "").strip().lower(): dict(c) for c in existing_companies}

    imported_files = 0
    imported_rows = 0

    for path in files:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            file_rows = list(reader)

        if not file_rows:
            continue

        company_name = str(file_rows[0].get("Company") or "").strip()
        if not company_name:
            company_name = app._company_name_from_filename(path.stem)

        key = company_name.lower()
        current = company_map.get(key)
        if not current:
            current = {
                "id": "",
                "company": company_name,
                "rep_name": "",
                "rep_email": "",
                "active": True,
                "source_file": "",
                "order_note": "",
                "order_rows": [],
            }

        # Merge row-by-row by (sku, name, box_price) to avoid duplicates on reruns.
        existing_items = {}
        for r in current.get("order_rows") or []:
            sku = str((r or {}).get("sku") or "").strip().lower()
            name = str((r or {}).get("name") or "").strip().lower()
            price = _to_float((r or {}).get("box_price") or (r or {}).get("unit_cost"))
            existing_items[(sku, name, price)] = dict(r)

        rep_name = str(current.get("rep_name") or "").strip()
        rep_email = str(current.get("rep_email") or "").strip()

        for row in file_rows:
            sku = str(row.get("SKU") or "").strip()
            name = str(row.get("Product") or "").strip()
            if not name:
                continue

            box_price = _to_float(row.get("Box Price"))
            cigars_per_box = _to_int(row.get("Cigars/Box"))
            source_price = str(row.get("Price Source") or "").strip() or "imported"

            rep_name_row = str(row.get("Rep Name") or "").strip()
            rep_email_row = str(row.get("Rep Email") or "").strip()
            if rep_name_row and not rep_name:
                rep_name = rep_name_row
            if rep_email_row and not rep_email:
                rep_email = rep_email_row

            item_key = (sku.lower(), name.lower(), box_price)
            existing_items[item_key] = {
                "sku": sku,
                "name": name,
                "box_price": box_price,
                "unit_cost": box_price,
                "boxes": 0,
                "quantity": 0,
                "stick_price": 0.0,
                "cigars_per_box": max(0, cigars_per_box),
                "source_price_type": source_price,
                "rep_name": rep_name_row,
                "rep_email": rep_email_row,
                "notes": "",
            }
            imported_rows += 1

        merged_items = list(existing_items.values())
        merged_items.sort(key=lambda r: (str(r.get("name") or "").lower(), str(r.get("sku") or "").lower()))

        current["company"] = company_name
        current["rep_name"] = rep_name
        current["rep_email"] = rep_email
        current["active"] = True
        current["source_file"] = f"Imported from cleaned_output ({path.name})"
        current["order_rows"] = merged_items

        company_map[key] = current
        imported_files += 1

    final_companies = list(company_map.values())
    app.save_ordering_companies(pg, final_companies)

    # Upsert reps from imported company defaults into rep directory.
    merged_reps = app.upsert_company_reps_into_sales_reps(final_companies, existing_reps)
    app.save_ordering_sales_reps(pg, merged_reps)

    print(f"Imported files: {imported_files}")
    print(f"Processed rows: {imported_rows}")
    print(f"Companies saved: {len(final_companies)}")
    print(f"Sales reps saved: {len(merged_reps)}")


if __name__ == "__main__":
    main()
