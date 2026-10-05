"""Pure price-sheet parsing helpers used by the ordering workflows."""

import csv
import importlib
import io
from email.utils import parseaddr
from pathlib import Path

def _parse_price_value(value) -> float:
    try:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        text = str(value).strip().replace("$", "").replace(",", "")
        if not text:
            return 0.0
        return round(float(text), 2)
    except Exception:
        return 0.0


def _normalize_header(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


_FILENAME_STRIP_WORDS = {
    "price", "prices", "sheet", "list", "pricelist", "pricesheets", "catalog",
    "catalogue", "wholesale", "2024", "2025", "2026", "2027", "updated",
    "new", "current", "final", "revised", "v1", "v2", "v3",
}


def _company_name_from_filename(file_name: str) -> str:
    """Derive a clean company name from a price-sheet filename."""
    import re
    stem = Path(str(file_name or "")).stem
    clean = re.sub(r"[_\-]+", " ", stem).strip()
    words = [w for w in clean.split() if w.lower() not in _FILENAME_STRIP_WORDS]
    result = " ".join(words).strip()
    if not result:
        return stem.replace("_", " ").replace("-", " ").strip().title()
    return result.title()


def _detect_company_meta_from_table(headers: list[str], rows: list[list]) -> dict:
    """Scan table headers and rows for company name, rep name, and rep email."""
    company_idx = _find_header_index(
        headers,
        ["company", "company name", "vendor", "vendor name", "brand",
         "brand name", "supplier", "distributor", "manufacturer"],
    )
    rep_idx = _find_header_index(headers, ["rep", "sales rep", "rep name", "vendor rep", "account rep"])
    rep_email_idx = _find_header_index(headers, ["rep email", "sales rep email", "email", "vendor rep email"])

    company_name = ""
    rep_name = ""
    rep_email = ""

    for row in (rows or []):
        if not isinstance(row, list):
            continue
        if not company_name and 0 <= company_idx < len(row):
            val = str(row[company_idx] or "").strip()
            if val and val.lower() not in {"none", "n/a", "-", ""}:
                company_name = val
        if not rep_name and 0 <= rep_idx < len(row):
            val = str(row[rep_idx] or "").strip()
            if val and val.lower() not in {"none", "n/a", "-", ""}:
                rep_name = val
        if not rep_email and 0 <= rep_email_idx < len(row):
            val = parseaddr(str(row[rep_email_idx] or "").strip())[1].strip()
            if val and "@" in val:
                rep_email = val
        if company_name and rep_name and rep_email:
            break

    return {"company": company_name, "rep_name": rep_name, "rep_email": rep_email}


def _find_header_index(headers: list[str], candidates: list[str]) -> int:
    normalized_candidates = {_normalize_header(c) for c in candidates}
    # Pass 1: exact match
    for idx, header in enumerate(headers):
        if _normalize_header(header) in normalized_candidates:
            return idx
    # Pass 2: substring match (header contains a candidate OR candidate contains header)
    for idx, header in enumerate(headers):
        norm_h = _normalize_header(header)
        if not norm_h:
            continue
        for cand in normalized_candidates:
            if cand and (cand in norm_h or norm_h in cand):
                return idx
    return -1


def _find_table_header_row(table: list[list]) -> int:
    """Scan the first 15 rows and return the index of the row most likely to be
    the column header.  Prefer rows where most cells are text (not numbers),
    have several non-empty cells, and are followed by rows containing numbers.
    """
    if not table:
        return 0
    best_idx = 0
    best_score = -1
    for i, row in enumerate(table[:15]):
        if not isinstance(row, list):
            continue
        non_empty = [str(c or "").strip() for c in row if str(c or "").strip()]
        if len(non_empty) < 2:
            continue
        text_cells = 0
        for val in non_empty:
            try:
                float(val.replace("$", "").replace(",", ""))
            except Exception:
                text_cells += 1
        text_ratio = text_cells / len(non_empty)
        if text_ratio < 0.4:
            continue
        # Check that at least one data row below has a numeric value
        has_numeric_below = False
        for data_row in table[i + 1: i + 8]:
            if not isinstance(data_row, list):
                continue
            for cell in data_row:
                v = str(cell or "").strip().replace("$", "").replace(",", "")
                try:
                    float(v)
                    has_numeric_below = True
                    break
                except Exception:
                    pass
            if has_numeric_below:
                break
        if not has_numeric_below:
            continue
        score = text_ratio * len(non_empty) - i * 0.15
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _find_price_col_by_values(rows: list[list], exclude_idxs: set, prefer_larger: bool = True) -> int:
    """Fallback: find a column that is mostly numeric with price-range values."""
    col_total: dict = {}
    col_numeric: dict = {}
    col_sum: dict = {}
    for row in (rows or [])[:30]:
        if not isinstance(row, list):
            continue
        for idx, cell in enumerate(row):
            if idx in exclude_idxs:
                continue
            val = str(cell or "").strip().replace("$", "").replace(",", "")
            if not val:
                continue
            col_total[idx] = col_total.get(idx, 0) + 1
            try:
                fval = float(val)
                # Keep value range realistic for cigar sheet prices and avoid zip/UPC columns.
                if 1 <= fval <= 2_500:
                    col_numeric[idx] = col_numeric.get(idx, 0) + 1
                    col_sum[idx] = col_sum.get(idx, 0.0) + fval
            except Exception:
                pass
    best = -1
    best_ratio = 0.55
    best_avg = 0.0
    for idx, total in col_total.items():
        numeric = col_numeric.get(idx, 0)
        if total == 0:
            continue
        ratio = numeric / total
        avg = col_sum.get(idx, 0.0) / max(numeric, 1)
        if not (2 <= avg <= 2_000):
            continue
        if ratio >= best_ratio:
            if prefer_larger and avg > best_avg:
                best_avg = avg
                best_ratio = ratio
                best = idx
            elif not prefer_larger:
                best_ratio = ratio
                best = idx
    return best


def _extract_prices_from_row_text(row: list) -> list[float]:
    """Extract dollar-like prices from all text cells in a row."""
    import re

    values: list[float] = []
    for cell in row or []:
        text = str(cell or "").strip()
        if not text:
            continue
        for m in re.findall(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text):
            price = _parse_price_value(m)
            if price > 0:
                values.append(price)
    # Keep order but remove exact duplicates to avoid noisy repeats.
    deduped: list[float] = []
    for v in values:
        if not deduped or abs(deduped[-1] - v) > 1e-9:
            deduped.append(v)
    return deduped


def _parse_price_rows_from_table(headers: list[str], rows: list[list]) -> list[dict]:
    sku_idx = _find_header_index(headers, ["sku", "item code", "item #", "code", "upc", "barcode", "item no", "item number"])
    name_idx = _find_header_index(headers, ["name", "item", "description", "product", "product name", "item description", "product description", "title"])
    box_price_idx = _find_header_index(
        headers,
        [
            "box price", "price per box", "box cost", "case price", "carton price",
            "wholesale box", "box", "case", "box wholesale", "case cost",
            "wholesale", "wholesale price", "dealer price", "dealer cost",
            "your price", "our price", "cost", "unit cost", "price",
        ],
    )
    stick_price_idx = _find_header_index(
        headers,
        [
            "single price", "stick price", "cigar price", "unit price",
            "price each", "each", "single", "msrp", "retail", "retail price",
            "suggested retail", "srp",
        ],
    )
    cigars_per_box_idx = _find_header_index(
        headers,
        [
            "cigars per box", "sticks per box", "qty per box", "quantity per box",
            "box qty", "pack size", "count", "box count", "qty", "quantity",
            "ct", "size",
        ],
    )
    rep_idx = _find_header_index(headers, ["rep", "sales rep", "rep name", "vendor rep", "account rep"])
    rep_email_idx = _find_header_index(headers, ["rep email", "sales rep email", "email", "vendor rep email"])

    # If no box/stick price column found by name, fall back to numeric value detection
    exclude_non_price = {sku_idx, name_idx, cigars_per_box_idx, rep_idx, rep_email_idx} - {-1}
    if box_price_idx < 0 and stick_price_idx < 0:
        box_price_idx = _find_price_col_by_values(rows, exclude_non_price, prefer_larger=True)
    elif box_price_idx < 0:
        box_price_idx = _find_price_col_by_values(rows, exclude_non_price | {stick_price_idx}, prefer_larger=True)

    parsed = []
    for row in rows:
        if not isinstance(row, list):
            continue
        sku = str(row[sku_idx]).strip() if 0 <= sku_idx < len(row) and row[sku_idx] is not None else ""
        name = str(row[name_idx]).strip() if 0 <= name_idx < len(row) and row[name_idx] is not None else ""
        if not name and sku:
            name = sku
        if not name:
            continue
        # Skip common non-product lines that leak from vendor sheets.
        name_l = name.lower()
        if "philadelphia" in name_l or name_l in {"pa", "pennsylvania"}:
            continue
        # Skip rows that look like sub-headers or separators
        if name.lower() in {"name", "item", "description", "product", "product name", "sku", "code"}:
            continue

        box_price = _parse_price_value(row[box_price_idx]) if 0 <= box_price_idx < len(row) else 0.0
        stick_price = _parse_price_value(row[stick_price_idx]) if 0 <= stick_price_idx < len(row) else 0.0
        cigars_per_box = 0
        if 0 <= cigars_per_box_idx < len(row):
            try:
                cigars_per_box = max(0, int(float(str(row[cigars_per_box_idx]).strip() or "0")))
            except Exception:
                cigars_per_box = 0

        # Fallback for rows where both wholesale and MSRP live inside the product text.
        text_prices = _extract_prices_from_row_text(row)
        if len(text_prices) >= 2:
            # Convention in many sheets/PDF exports: larger is wholesale/box, smaller is per-stick MSRP.
            inferred_box = max(text_prices)
            inferred_stick = min(text_prices)
            if box_price <= 0 or (box_price > 0 and inferred_box > box_price * 1.2):
                box_price = inferred_box
            if stick_price <= 0:
                stick_price = inferred_stick
        elif len(text_prices) == 1 and box_price <= 0:
            box_price = text_prices[0]

        source_price_type = "box"

        # Guard against single-cigar prices being used as box prices.
        if cigars_per_box > 1 and box_price > 0 and box_price <= 60:
            computed = round(box_price * cigars_per_box, 2)
            if computed > box_price:
                box_price = computed
                source_price_type = "computed_from_stick"
        if cigars_per_box > 1 and box_price > 0 and stick_price > 0 and box_price <= stick_price * 1.05:
            box_price = round(stick_price * cigars_per_box, 2)
            source_price_type = "computed_from_stick"

        if box_price <= 0 and stick_price > 0 and cigars_per_box > 0:
            box_price = round(stick_price * cigars_per_box, 2)
            source_price_type = "computed_from_stick"
        elif box_price <= 0 and stick_price > 0:
            box_price = stick_price
            source_price_type = "single_or_unknown"
        elif len(text_prices) >= 2 and box_price > 0:
            source_price_type = "embedded_wholesale"

        if box_price <= 0:
            continue

        rep_name = str(row[rep_idx]).strip() if 0 <= rep_idx < len(row) and row[rep_idx] is not None else ""
        rep_email = (
            parseaddr(str(row[rep_email_idx]).strip())[1].strip()
            if 0 <= rep_email_idx < len(row) and row[rep_email_idx] is not None
            else ""
        )
        parsed.append(
            {
                "sku": sku,
                "name": name,
                "box_price": box_price,
                "unit_cost": box_price,
                "boxes": 0,
                "quantity": 0,
                "stick_price": stick_price,
                "cigars_per_box": cigars_per_box,
                "source_price_type": source_price_type,
                "rep_name": rep_name,
                "rep_email": rep_email,
                "notes": "",
            }
        )
    return parsed


def parse_price_sheet_upload_with_meta(file_name: str, file_bytes: bytes) -> tuple[list[dict], dict, str]:
    """Parse price sheet and also return company/rep metadata detected from the sheet.
    Returns (rows, meta_dict, error_string). meta_dict has: company, rep_name, rep_email.
    """
    name = str(file_name or "").strip().lower()
    meta: dict = {"company": "", "rep_name": "", "rep_email": ""}

    if name.endswith(".csv"):
        text = file_bytes.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        table = [list(row) for row in reader]
        if len(table) < 2:
            return [], meta, "CSV file does not contain enough rows."
        header_row = _find_table_header_row(table)
        headers = [str(c or "") for c in table[header_row]]
        data_rows = table[header_row + 1:]
        meta = _detect_company_meta_from_table(headers, data_rows)
        rows = _parse_price_rows_from_table(headers, data_rows)
        return rows, meta, ""

    if name.endswith(".xlsx"):
        try:
            openpyxl = importlib.import_module("openpyxl")
        except Exception:
            return [], meta, "openpyxl is required for .xlsx uploads."
        try:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        except Exception as exc:
            return [], meta, f"Could not parse .xlsx workbook: {exc}"

        best_rows: list[dict] = []
        best_meta: dict = dict(meta)
        best_score = -1

        # Scan all sheets and pick the one with the strongest parsed result.
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            table = [list(row) for row in ws.iter_rows(values_only=True)]
            if len(table) < 2:
                continue
            header_row = _find_table_header_row(table)
            headers = [str(c or "") for c in table[header_row]]
            data_rows = table[header_row + 1:]
            sheet_meta = _detect_company_meta_from_table(headers, data_rows)
            sheet_rows = _parse_price_rows_from_table(headers, data_rows)

            # Score favors more valid rows and useful metadata.
            score = len(sheet_rows)
            if sheet_meta.get("company"):
                score += 5
            if sheet_meta.get("rep_email"):
                score += 3

            if score > best_score:
                best_score = score
                best_rows = sheet_rows
                best_meta = sheet_meta

        if best_rows:
            return best_rows, best_meta, ""

        # Fallback to pandas for uncommon workbook edge-cases.
        try:
            pandas = importlib.import_module("pandas")
            xls = pandas.ExcelFile(io.BytesIO(file_bytes))
            best_rows = []
            best_meta = dict(meta)
            best_score = -1
            for sheet_name in xls.sheet_names:
                df = pandas.read_excel(xls, sheet_name=sheet_name, header=None)
                if df.empty:
                    continue
                table = [list(r) for r in df.fillna("").to_numpy().tolist()]
                header_row = _find_table_header_row(table)
                headers = [str(c or "") for c in table[header_row]]
                data_rows = table[header_row + 1:]
                sheet_meta = _detect_company_meta_from_table(headers, data_rows)
                sheet_rows = _parse_price_rows_from_table(headers, data_rows)
                score = len(sheet_rows)
                if sheet_meta.get("company"):
                    score += 5
                if sheet_meta.get("rep_email"):
                    score += 3
                if score > best_score:
                    best_score = score
                    best_rows = sheet_rows
                    best_meta = sheet_meta
            if best_rows:
                return best_rows, best_meta, ""
        except Exception:
            pass

        return [], meta, "Excel workbook parsed but no valid order rows were found across sheets."

    if name.endswith(".xls"):
        try:
            pandas = importlib.import_module("pandas")
            xls = pandas.ExcelFile(io.BytesIO(file_bytes))
            best_rows: list[dict] = []
            best_meta: dict = dict(meta)
            best_score = -1
            for sheet_name in xls.sheet_names:
                df = pandas.read_excel(xls, sheet_name=sheet_name, header=None)
                if df.empty:
                    continue
                table = [list(r) for r in df.fillna("").to_numpy().tolist()]
                header_row = _find_table_header_row(table)
                headers = [str(c or "") for c in table[header_row]]
                data_rows = table[header_row + 1:]
                sheet_meta = _detect_company_meta_from_table(headers, data_rows)
                sheet_rows = _parse_price_rows_from_table(headers, data_rows)
                score = len(sheet_rows)
                if sheet_meta.get("company"):
                    score += 5
                if sheet_meta.get("rep_email"):
                    score += 3
                if score > best_score:
                    best_score = score
                    best_rows = sheet_rows
                    best_meta = sheet_meta
            if best_rows:
                return best_rows, best_meta, ""
            return [], meta, "Could not find valid order rows in .xls workbook."
        except Exception:
            return [], meta, "Could not parse .xls. Save as .xlsx and upload again."

    if name.endswith(".pdf"):
        try:
            pypdf = importlib.import_module("pypdf")
        except Exception:
            return [], meta, "pypdf is required for PDF uploads."
        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            lines = []
            for page in reader.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    clean = " ".join(str(line).strip().split())
                    if clean:
                        lines.append(clean)
        except Exception as exc:
            return [], meta, f"Could not parse PDF: {exc}"
        rows = _parse_pdf_price_lines(lines)
        if not rows:
            return [], meta, "No orderable rows detected in PDF. Try an Excel or CSV price sheet for best results."
        return rows, meta, ""

    return [], meta, "Unsupported file type. Use .csv, .xlsx, or .pdf."


def upsert_ordering_company_from_sheet(
    pg: SyncPostgrestClient,
    company_name: str,
    rep_name: str,
    rep_email: str,
    source_file: str,
    order_rows: list[dict],
) -> dict:
    """Create or update a company profile and save the order draft. Returns the company dict."""
    company_name = str(company_name or "").strip()
    if not company_name:
        return {}
    companies = load_ordering_companies(pg)
    target_id = hashlib.sha1(company_name.lower().encode("utf-8")).hexdigest()[:12]
    match = next((c for c in companies if str(c.get("id") or "").strip() == target_id), None)
    clean_rows = [
        _clean_ordering_item_row(item)
        for item in (order_rows or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if match:
        updated_company = dict(match)
        if rep_name:
            updated_company["rep_name"] = rep_name
        if rep_email:
            updated_company["rep_email"] = rep_email
        updated_company["source_file"] = source_file
        updated_company["order_rows"] = clean_rows
        updated_company["active"] = True
        updated = [updated_company if str(c.get("id") or "").strip() == target_id else c for c in companies]
    else:
        updated_company = {
            "id": target_id,
            "company": company_name,
            "rep_name": rep_name or "",
            "rep_email": rep_email or "",
            "active": True,
            "source_file": source_file,
            "order_note": "",
            "order_rows": clean_rows,
        }
        updated = list(companies) + [updated_company]
    save_ordering_companies(pg, updated)
    return updated_company


def _parse_pdf_price_lines(lines: list[str]) -> list[dict]:
    """Parse text lines extracted from a PDF price sheet.

    Expects format:  Item Name  [Size]  [Packing]  $Wholesale  $MSRP_per_cigar
    Lines with fewer than two dollar-sign prices are skipped (section headers, addresses, etc.)
    """
    import re

    # Match one or two dollar prices, greedy at end of line
    price_pat = re.compile(r'\$(\d[\d,\.]*)')
    # Packing: "Box of 25", "Cube of 100", "Boat of 50" etc.
    packing_pat = re.compile(r'\b(Box|Cube|Boat|Tin|Pack)\s+of\s+(\d+)\b', re.IGNORECASE)
    # Size: "6.5 x 44", "4.375 x 44", "6.625 X 48"
    size_pat = re.compile(r'\b\d+\.?\d*\s*[xX]\s*\d+\.?\d*\b')
    # Phone / zip junk
    junk_pat = re.compile(r'\d{5}|\d{3}[•\-]\d{3}[•\-]\d{4}|www\.|@|ashtondistrib|confidential|effective|price list|wholesale cigar|townsend|philadelphia', re.IGNORECASE)

    parsed = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 4:
            continue
        if junk_pat.search(line):
            continue

        prices = price_pat.findall(line)
        if len(prices) < 2:
            # Single price only — could be a valid line if it's a wholesale-only sheet
            # but for this PDF format, skip to avoid junk
            continue

        # Last two prices: wholesale (second-to-last) and MSRP per cigar (last)
        wholesale_raw = prices[-2]
        msrp_raw = prices[-1]
        wholesale = _parse_price_value(wholesale_raw)
        msrp = _parse_price_value(msrp_raw)

        if wholesale <= 0:
            continue

        # Extract packing to get cigars_per_box
        packing_match = packing_pat.search(line)
        cigars_per_box = 0
        packing_text = ""
        if packing_match:
            packing_text = packing_match.group(0)
            try:
                cigars_per_box = int(packing_match.group(2))
            except Exception:
                pass

        # Build clean name: strip prices, packing, size from the line
        clean = line
        # Remove all $X.XX price tokens
        clean = re.sub(r'\$[\d,\.]+', '', clean)
        # Remove packing text
        if packing_text:
            clean = clean.replace(packing_text, '')
        # Remove size patterns
        clean = re.sub(r'\b\d+\.?\d*\s*[xX]\s*\d+\.?\d*\b', '', clean)
        # Collapse whitespace
        name = ' '.join(clean.split()).strip().strip('.,;:-')

        if not name or len(name) < 2:
            continue
        # Skip if name is purely numeric or looks like a page number
        if re.match(r'^[\d\s]+$', name):
            continue

        parsed.append(
            {
                "sku": "",
                "name": name,
                "box_price": wholesale,
                "unit_cost": wholesale,
                "boxes": 0,
                "quantity": 0,
                "stick_price": msrp,
                "cigars_per_box": cigars_per_box,
                "source_price_type": "pdf_wholesale",
                "rep_name": "",
                "rep_email": "",
                "notes": packing_text,
            }
        )
    return parsed


def parse_price_sheet_upload(file_name: str, file_bytes: bytes) -> tuple[list[dict], str]:
    name = str(file_name or "").strip().lower()
    if name.endswith(".csv"):
        text = file_bytes.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        table = [list(row) for row in reader]
        if len(table) < 2:
            return [], "CSV file does not contain enough rows."
        return _parse_price_rows_from_table(table[0], table[1:]), ""

    if name.endswith(".xlsx") or name.endswith(".xls"):
        try:
            pandas = importlib.import_module("pandas")
            df = pandas.read_excel(io.BytesIO(file_bytes))
            if df.empty:
                return [], "Excel sheet does not contain enough rows."
            headers = [str(col or "") for col in list(df.columns)]
            rows = [list(row) for row in df.fillna("").to_numpy().tolist()]
            return _parse_price_rows_from_table(headers, rows), ""
        except Exception:
            pass

    if name.endswith(".xlsx"):
        try:
            openpyxl = importlib.import_module("openpyxl")
        except Exception:
            return [], "openpyxl is required for .xlsx uploads. Install dependencies from requirements.txt."
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        table = [list(row) for row in ws.iter_rows(values_only=True)]
        if len(table) < 2:
            return [], "Excel sheet does not contain enough rows."
        return _parse_price_rows_from_table([str(h or "") for h in table[0]], table[1:]), ""

    if name.endswith(".xls"):
        return [], "Could not parse .xls. Save as .xlsx and upload again."

    if name.endswith(".pdf"):
        try:
            pypdf = importlib.import_module("pypdf")
        except Exception:
            return [], "pypdf is required for PDF uploads. Install dependencies from requirements.txt."

        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            lines = []
            for page in reader.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    clean = " ".join(str(line).strip().split())
                    if clean:
                        lines.append(clean)
        except Exception as exc:
            return [], f"Could not parse PDF: {exc}"

        parsed = _parse_pdf_price_lines(lines)
        if not parsed:
            return [], "No orderable rows detected in PDF. Try an Excel or CSV price sheet for best results."
        return parsed, ""

    return [], "Unsupported file type. Use .csv, .xlsx, or .pdf."
