"""Invoice scan enhancement, OCR, and metadata parsing helpers."""

import datetime
import io
import re

import streamlit as st


def _enhance_scan(raw: bytes) -> bytes:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("L")
    img.thumbnail((1800, 1800))
    img = ImageOps.autocontrast(img, cutoff=2)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


def _images_to_pdf(images: list[bytes]) -> bytes:
    import fitz

    pdf = fitz.open()
    for img in images:
        pix = fitz.Pixmap(img)
        page = pdf.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, stream=img)
    out = pdf.tobytes(garbage=3, deflate=True)
    pdf.close()
    return out


@st.cache_resource
def _get_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except Exception:
        return None


def _ocr_text(img_bytes: bytes) -> str:
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        result, _ = ocr(img_bytes)
    except Exception:
        return ""
    return "\n".join(str(item[1]) for item in (result or []))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_PATTERNS = [
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)"), "mdy"),
    (re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"), "mondy"),
]


def extract_invoice_date(text: str) -> datetime.date | None:
    today = datetime.date.today()
    found: list[tuple[int, int, datetime.date]] = []
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                a, b, c = match.groups()
                if kind == "ymd":
                    value = datetime.date(int(a), int(b), int(c))
                elif kind == "mdy":
                    year = int(c) + (2000 if len(c) == 2 else 0)
                    value = datetime.date(year, int(a), int(b))
                else:
                    month = _MONTHS.get(a.lower())
                    if not month:
                        continue
                    value = datetime.date(int(c), month, int(b))
            except ValueError:
                continue
            if not (2000 <= value.year <= today.year + 1):
                continue
            context = re.sub(r"\s", "", text[max(0, match.start() - 30):match.start()].lower())
            if "due" in context[-12:] or "ship" in context[-12:] or "order" in context[-12:]:
                priority = 3
            elif "invoicedate" in context[-14:] or "invdate" in context[-10:]:
                priority = 0
            elif "date" in context[-8:]:
                priority = 1
            else:
                priority = 2
            found.append((priority, match.start(), value))
    return min(found)[2] if found else None


def extract_invoice_number(text: str) -> str:
    for match in re.finditer(r"invoice\s*(?:no\.?|number|num|#)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{2,})", text, re.I):
        candidate = match.group(1)
        if any(ch.isdigit() for ch in candidate):
            return candidate
    return ""


def detect_invoice_company(text: str, companies: list[dict]) -> str:
    haystack = re.sub(r"[^a-z0-9]", "", text.lower())
    best, best_len = "", 0
    for company in companies:
        keys = [company["company"]] + [b for b in re.split(r"[,;/]", company.get("rep_brands") or "") if b.strip()]
        for key in keys:
            norm = re.sub(r"[^a-z0-9]", "", key.lower())
            if len(norm) >= 4 and norm in haystack and len(norm) > best_len:
                best, best_len = company["company"], len(norm)
    return best
