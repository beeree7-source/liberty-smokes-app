import csv
import re
import io
import xml.etree.ElementTree as ET
from pathlib import Path

import app

INPUT_DIR = Path(r"c:\Users\beere\OneDrive\Desktop\Liberty Files\Price Sheets")
OUTPUT_DIR = INPUT_DIR / "cleaned_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONVERTED_DIR = OUTPUT_DIR / "converted_xlsx"
CONVERTED_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown"


def clean_row(row: dict, source_file: str, company: str) -> dict:
    box_price = float(row.get("box_price") or row.get("unit_cost") or 0.0)
    cigars_per_box = int(row.get("cigars_per_box") or 0)
    return {
        "Company": company,
        "Source File": source_file,
        "SKU": str(row.get("sku") or "").strip(),
        "Product": str(row.get("name") or "").strip(),
        "Box Price": round(box_price, 2),
        "Cigars/Box": cigars_per_box,
        "Price Source": str(row.get("source_price_type") or "").strip(),
        "Rep Name": str(row.get("rep_name") or "").strip(),
        "Rep Email": str(row.get("rep_email") or "").strip(),
    }


def parse_with_app_parser(file_name: str, data: bytes) -> tuple[list[dict], dict, str, str]:
    rows, meta, err = app.parse_price_sheet_upload_with_meta(file_name, data)
    method = "app_parser"
    return rows, meta, err, method


def convert_xls_to_xlsx_bytes(file_path: Path) -> tuple[bytes | None, str]:
    """Convert legacy .xls into .xlsx bytes for downstream parsing."""
    try:
        import pandas as pd
    except Exception as exc:
        return None, f"pandas missing for .xls conversion: {exc}"

    try:
        sheets = pd.read_excel(file_path, sheet_name=None, header=None, engine="xlrd")
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for name, df in sheets.items():
                safe_name = str(name or "Sheet1")[:31]
                df.to_excel(writer, index=False, header=False, sheet_name=safe_name)
        payload = buf.getvalue()
        # Save converted file for auditability.
        converted_path = CONVERTED_DIR / f"{file_path.stem}.xlsx"
        converted_path.write_bytes(payload)
        return payload, ""
    except Exception as exc:
        return None, f".xls conversion failed: {exc}"


def extract_pdf_lines_fallback(pdf_bytes: bytes) -> tuple[list[str], str]:
    """Try multiple text/OCR fallbacks for hard PDFs.
    Returns (lines, method_used).
    """
    lines: list[str] = []

    # Fallback 0: pdfplumber line extraction.
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                for line in txt.splitlines():
                    clean = " ".join(str(line).strip().split())
                    if clean:
                        lines.append(clean)
        if lines:
            return lines, "pdfplumber_text"
    except Exception:
        pass

    # Fallback 1: PyMuPDF text extraction.
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            txt = page.get_text("text") or ""
            for line in txt.splitlines():
                clean = " ".join(str(line).strip().split())
                if clean:
                    lines.append(clean)
        if lines:
            return lines, "fitz_text"
    except Exception:
        pass

    # Fallback 2: OCR with pytesseract + PyMuPDF raster pages.
    try:
        import fitz  # type: ignore
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=220)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
            txt = pytesseract.image_to_string(img) or ""
            for line in txt.splitlines():
                clean = " ".join(str(line).strip().split())
                if clean:
                    lines.append(clean)
        if lines:
            return lines, "pytesseract_ocr"
    except Exception:
        pass

    return [], "none"


def parse_pdf_lines_generic(lines: list[str]) -> list[dict]:
    """Looser PDF line parser for vendor files without explicit '$' symbols.
    Looks for two trailing decimal numbers (wholesale + stick/msrp) and keeps wholesale.
    """
    out = []
    for raw in lines or []:
        line = " ".join(str(raw or "").strip().split())
        if not line:
            continue
        low = line.lower()
        if "philadelphia" in low or "price list" in low or "effective" in low:
            continue

        # Capture prices with optional dollar signs.
        matches = re.findall(r"\$?([0-9]{1,4}(?:\.[0-9]{1,2})?)", line)
        prices = []
        for m in matches:
            try:
                v = float(m)
            except Exception:
                continue
            if 1 <= v <= 2500:
                prices.append(v)

        if len(prices) < 2:
            continue

        wholesale = max(prices[-2], prices[-1])
        stick = min(prices[-2], prices[-1])

        name = line
        name = re.sub(r"\$?[0-9]{1,4}(?:\.[0-9]{1,2})?", "", name)
        name = re.sub(r"\b\d+\.?\d*\s*[xX]\s*\d+\.?\d*\b", "", name)
        name = re.sub(r"\b(Box|Cube|Boat|Tin|Pack)\s+of\s+\d+\b", "", name, flags=re.IGNORECASE)
        name = " ".join(name.split()).strip(" -,:;")
        if not name:
            continue

        out.append(
            {
                "sku": "",
                "name": name,
                "box_price": round(float(wholesale), 2),
                "unit_cost": round(float(wholesale), 2),
                "boxes": 0,
                "quantity": 0,
                "stick_price": round(float(stick), 2),
                "cigars_per_box": 0,
                "source_price_type": "pdf_generic_fallback",
                "rep_name": "",
                "rep_email": "",
                "notes": "",
            }
        )
    return out


def parse_spreadsheetml_xml_bytes(xml_bytes: bytes, source_name: str) -> tuple[list[dict], dict, str]:
    """Parse legacy XML Spreadsheet 2003 content (often mislabeled as .xls)."""
    try:
        text = xml_bytes.decode("utf-8", errors="ignore")
        root = ET.fromstring(text)
    except Exception as exc:
        return [], {"company": "", "rep_name": "", "rep_email": ""}, f"XML parse failed: {exc}"

    ns = {
        "ss": "urn:schemas-microsoft-com:office:spreadsheet",
    }

    best_rows: list[dict] = []
    best_meta: dict = {"company": "", "rep_name": "", "rep_email": ""}
    best_score = -1

    worksheets = root.findall(".//ss:Worksheet", ns)
    for ws in worksheets:
        table = ws.find("ss:Table", ns)
        if table is None:
            continue

        rows_table: list[list] = []
        for row_el in table.findall("ss:Row", ns):
            row_vals: list = []
            cur_idx = 1
            for cell_el in row_el.findall("ss:Cell", ns):
                idx_attr = cell_el.get("{urn:schemas-microsoft-com:office:spreadsheet}Index")
                if idx_attr:
                    try:
                        target_idx = int(idx_attr)
                        while cur_idx < target_idx:
                            row_vals.append("")
                            cur_idx += 1
                    except Exception:
                        pass
                data_el = cell_el.find("ss:Data", ns)
                row_vals.append((data_el.text or "") if data_el is not None else "")
                cur_idx += 1
            rows_table.append(row_vals)

        if len(rows_table) < 2:
            continue

        header_row = app._find_table_header_row(rows_table)
        headers = [str(c or "") for c in rows_table[header_row]]
        data_rows = rows_table[header_row + 1:]
        sheet_meta = app._detect_company_meta_from_table(headers, data_rows)
        sheet_rows = app._parse_price_rows_from_table(headers, data_rows)

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
    return [], best_meta, f"No valid rows parsed from XML spreadsheet: {source_name}"


def main() -> None:
    files = sorted(
        [
            p
            for p in INPUT_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in {".pdf", ".xlsx", ".xls", ".csv"}
        ]
    )

    summary_rows = []

    for path in files:
        try:
            data = path.read_bytes()
            rows, meta, err, method = parse_with_app_parser(path.name, data)

            # Option 3: convert .xls automatically and retry parser.
            if path.suffix.lower() == ".xls" and (err or not rows):
                converted_bytes, conv_err = convert_xls_to_xlsx_bytes(path)
                if converted_bytes:
                    rows, meta, err, method = parse_with_app_parser(f"{path.stem}.xlsx", converted_bytes)
                    if not err:
                        method = "xls_converted_to_xlsx"
                else:
                    # Some vendor files are XML/spreadsheet files with wrong .xls extension.
                    if "expected bof record" in str(conv_err).lower() or data.startswith(b"<?xml"):
                        rows, meta, err = parse_spreadsheetml_xml_bytes(data, path.name)
                        method = "xls_xml_spreadsheetml"
                        if err:
                            err = conv_err or err
                    else:
                        err = conv_err or err

            # Option 2: OCR/text fallback for PDFs with no parsed rows.
            if path.suffix.lower() == ".pdf" and (err or not rows):
                lines, pdf_method = extract_pdf_lines_fallback(data)
                if lines:
                    fallback_rows = app._parse_pdf_price_lines(lines)
                    if not fallback_rows:
                        fallback_rows = parse_pdf_lines_generic(lines)
                    if fallback_rows:
                        rows = fallback_rows
                        meta = {"company": "", "rep_name": "", "rep_email": ""}
                        err = ""
                        method = f"pdf_fallback_{pdf_method}"

            if err:
                summary_rows.append(
                    {
                        "file": path.name,
                        "status": "error",
                        "rows": 0,
                        "company": "",
                        "error": err,
                        "method": method,
                    }
                )
                continue
            if not rows:
                summary_rows.append(
                    {
                        "file": path.name,
                        "status": "empty",
                        "rows": 0,
                        "company": "",
                        "error": "No rows parsed",
                        "method": method,
                    }
                )
                continue

            company = str(meta.get("company") or "").strip()
            if not company:
                company = app._company_name_from_filename(path.name)

            cleaned = [clean_row(r, path.name, company) for r in rows if str((r or {}).get("name") or "").strip()]
            out_name = f"{slugify(company)}__{slugify(path.stem)}.csv"
            out_path = OUTPUT_DIR / out_name

            with out_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "Company",
                        "Source File",
                        "SKU",
                        "Product",
                        "Box Price",
                        "Cigars/Box",
                        "Price Source",
                        "Rep Name",
                        "Rep Email",
                    ],
                )
                writer.writeheader()
                writer.writerows(cleaned)

            summary_rows.append(
                {
                    "file": path.name,
                    "status": "ok",
                    "rows": len(cleaned),
                    "company": company,
                    "error": "",
                    "output": out_path.name,
                    "method": method,
                }
            )
        except Exception as exc:
            summary_rows.append(
                {
                    "file": path.name,
                    "status": "exception",
                    "rows": 0,
                    "company": "",
                    "error": str(exc),
                    "method": "exception",
                }
            )

    summary_path = OUTPUT_DIR / "_parse_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["file", "status", "rows", "company", "output", "method", "error"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary_rows:
            payload = {k: row.get(k, "") for k in fieldnames}
            writer.writerow(payload)

    ok = sum(1 for r in summary_rows if r.get("status") == "ok")
    print(f"Processed {len(files)} file(s). Successful: {ok}. Output folder: {OUTPUT_DIR}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
