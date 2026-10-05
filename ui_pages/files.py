import base64
import datetime
import mimetypes
import re
from email.utils import parseaddr
from pathlib import Path

import requests
import streamlit as st
from postgrest import SyncPostgrestClient

from files.invoice_parser import (
    _enhance_scan,
    _images_to_pdf,
    _ocr_text,
    detect_invoice_company,
    extract_invoice_date,
    extract_invoice_number,
)
from files.storage import (
    FILES_BUCKET,
    FILES_CATEGORIES,
    FILES_SETUP_SQL,
    _safe_storage_name,
    _storage_headers,
    _storage_url,
    storage_delete_file,
    storage_download_file,
    storage_list_files,
    storage_list_folders,
    storage_upload_file,
)


def _reset_invoice_scan():
    counter = int(st.session_state.get("inv_scan_counter", 0)) + 1
    st.session_state["inv_scan_pages"] = []
    st.session_state["inv_scan_open"] = False
    st.session_state["inv_scan_reviewing"] = False
    st.session_state["inv_scan_counter"] = counter


def _render_scan_thumbs(pages: list[bytes]):
    per_row = 4
    for start in range(0, len(pages), per_row):
        cols = st.columns(per_row)
        for offset, col in enumerate(cols):
            idx = start + offset
            if idx >= len(pages):
                break
            col.image(pages[idx], caption=f"Page {idx + 1}", width="stretch")
            if col.button("Remove", key=f"inv_scan_rm_{idx}_{len(pages)}"):
                pages.pop(idx)
                if not pages:
                    st.session_state["inv_scan_open"] = True
                    st.session_state["inv_scan_reviewing"] = False
                st.rerun()


def _render_invoice_scanner(pg: SyncPostgrestClient, company_options: list[str], companies: list[dict]):
    ss = st.session_state
    pages: list[bytes] = ss.setdefault("inv_scan_pages", [])
    flash = ss.pop("inv_flash", "")
    if flash:
        st.success(flash)

    if not ss.get("inv_scan_open") and not pages:
        if st.button("📷 Scan invoice with camera", key="inv_scan_start"):
            ss["inv_scan_open"] = True
            st.rerun()
        return

    st.markdown("**Scan invoice**")
    counter = int(ss.setdefault("inv_scan_counter", 0))
    if ss.get("inv_scan_open"):
        shot = st.camera_input("Take a photo of each invoice page", key=f"inv_cam_{counter}")
        if shot is not None:
            pages.append(_enhance_scan(shot.getvalue()))
            ss["inv_scan_counter"] = counter + 1
            st.rerun()
        if pages:
            st.caption(f"{len(pages)} page(s) scanned so far. They will be saved together as one PDF. Keep taking photos for more pages, then tap Done scanning.")
            _render_scan_thumbs(pages)
        b1, b2 = st.columns(2)
        if pages and b1.button(f"Done scanning ({len(pages)} page{'s' if len(pages) != 1 else ''})", type="primary", key="inv_scan_done"):
            with st.spinner("Reading the invoice..."):
                text = ""
                company = date = number = None
                for page_bytes in pages[:3]:
                    text += "\n" + _ocr_text(page_bytes)
                    company = detect_invoice_company(text, companies)
                    date = extract_invoice_date(text)
                    if company and date:
                        break
                number = extract_invoice_number(text)
            ss["inv_scan_open"] = False
            ss["inv_scan_reviewing"] = True
            ss["inv_rev_company"] = company if company in company_options else "Other (type a name)"
            ss["inv_rev_company_new"] = "" if company in company_options else (company or "")
            ss["inv_rev_date"] = date or datetime.date.today()
            ss["inv_rev_number"] = number or ""
            ss["inv_rev_detected"] = bool(company), bool(date)
            st.rerun()
        if b2.button("Cancel", key="inv_scan_cancel"):
            _reset_invoice_scan()
            st.rerun()
        return

    detected_company, detected_date = ss.get("inv_rev_detected", (False, False))
    st.caption(f"{len(pages)} page(s) scanned. They will be saved as one PDF. Check the details below, then save.")
    if not (detected_company and detected_date):
        st.info("Couldn't read everything automatically. Please fill in what's missing.")
    _render_scan_thumbs(pages)
    company = st.selectbox("Company", company_options + ["Other (type a name)"], key="inv_rev_company")
    if company == "Other (type a name)":
        company = _safe_storage_name(st.text_input("Company name", key="inv_rev_company_new"))
        if company == "file":
            company = ""
    inv_date = st.date_input("Invoice date", key="inv_rev_date")
    number = st.text_input("Invoice number (optional)", key="inv_rev_number").strip()
    name_parts = [company or "Unknown", inv_date.strftime("%Y-%m-%d"), "Invoice"] + ([number] if number else [])
    file_name = _safe_storage_name("_".join(name_parts).replace(" ", "_")) + ".pdf"
    st.caption(f"Will save as Invoices/{company or 'Unknown'}/{inv_date.year}/{file_name}")
    r1, r2, r3 = st.columns(3)
    if r1.button("Save invoice", type="primary", key="inv_rev_save"):
        try:
            folder = f"Invoices/{company or 'Unknown'}/{inv_date.year}"
            storage_upload_file(folder, file_name, _images_to_pdf(pages), "application/pdf")
            _reset_invoice_scan()
            ss["inv_flash"] = f"Saved {file_name} to {folder}."
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save scan: {exc}")
    if r2.button("Add page", key="inv_rev_more"):
        ss["inv_scan_open"] = True
        ss["inv_scan_reviewing"] = False
        st.rerun()
    if r3.button("Discard", key="inv_rev_discard"):
        _reset_invoice_scan()
        st.rerun()


def _render_invoices_section(pg: SyncPostgrestClient):
    from app import load_ordering_companies

    try:
        existing_companies = storage_list_folders("Invoices")
        companies = load_ordering_companies(pg)
    except Exception as exc:
        st.error(f"Could not load invoice folders: {exc}")
        return
    company_options = sorted(
        {_safe_storage_name(c["company"]) for c in companies} | set(existing_companies), key=str.lower
    )
    safe_companies = [{**c, "company": _safe_storage_name(c["company"])} for c in companies]
    _render_invoice_scanner(pg, company_options, safe_companies)
    if st.session_state.get("inv_scan_open") or st.session_state.get("inv_scan_pages"):
        return

    st.divider()
    company = st.selectbox("Company", company_options + ["Other (type a name)"], key="inv_company")
    if company == "Other (type a name)":
        company = _safe_storage_name(st.text_input("New company folder name", key="inv_company_new"))
        if company == "file":
            st.info("Type a company name to continue.")
            return

    this_year = datetime.date.today().year
    try:
        existing_years = storage_list_folders(f"Invoices/{company}")
    except Exception:
        existing_years = []
    years = sorted({str(y) for y in range(this_year, this_year - 8, -1)} | set(existing_years), reverse=True)
    year = st.selectbox("Year", years, key="inv_year")
    prefix = f"Invoices/{company}/{year}"
    st.caption(f"Folder: {prefix}")
    _render_files_section(pg, prefix, allow_email=False)


def _render_files_section(pg: SyncPostgrestClient, category: str, allow_email: bool):
    from app import load_ordering_companies, load_smtp_settings, send_email, _render_zoomable_pages

    gen_key = f"files_upload_gen_{category}"
    if st.session_state.pop(f"files_uploaded_msg_{category}", False):
        st.success("Uploaded.")
    uploads = st.file_uploader(
        ("Upload files" if "/" in category else f"Upload {category.lower()} files"),
        accept_multiple_files=True,
        key=f"files_upload_{category}_{st.session_state.get(gen_key, 0)}",
    )
    if uploads and st.button(f"Save {len(uploads)} file(s)", type="primary", key=f"files_save_{category}"):
        failed = []
        for upload in uploads:
            try:
                storage_upload_file(category, upload.name, upload.getvalue(), str(upload.type or ""))
            except Exception as exc:
                failed.append(f"{upload.name} ({exc})")
        if failed:
            st.error("Some files failed: " + "; ".join(failed))
        else:
            st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
            st.session_state[f"files_uploaded_msg_{category}"] = True
            st.rerun()

    try:
        items = storage_list_files(category)
    except Exception as exc:
        st.error(f"Could not load files: {exc}")
        return
    if not items:
        st.caption("No files yet." if "/" in category else f"No {category.lower()} files yet.")
        return

    table = [
        {
            "File": item["name"],
            "Uploaded": str(item.get("created_at") or "")[:10],
            "Size (KB)": round(int((item.get("metadata") or {}).get("size") or 0) / 1024, 1),
        }
        for item in items
    ]
    st.dataframe(table, width="stretch", hide_index=True)

    names = [item["name"] for item in items]
    selected = st.selectbox("Select a file", names, key=f"files_select_{category}")
    try:
        data = storage_download_file(category, selected)
    except Exception as exc:
        st.error(f"Could not open file: {exc}")
        return

    c1, c2 = st.columns(2)
    c1.download_button(
        "Download",
        data=data,
        file_name=selected,
        key=f"files_dl_{category}",
    )
    if c2.button("Delete", key=f"files_del_{category}"):
        try:
            storage_delete_file(category, selected)
            st.rerun()
        except Exception as exc:
            st.error(f"Delete failed: {exc}")

    if selected.lower().endswith(".pdf"):
        with st.expander("Preview"):
            try:
                import fitz

                with fitz.open(stream=data, filetype="pdf") as doc:
                    pages_b64 = [
                        base64.b64encode(p.get_pixmap(dpi=150).tobytes("png")).decode() for p in doc
                    ]
                _render_zoomable_pages(pages_b64, key=selected)
            except Exception:
                st.info("Preview unavailable. Use Download to open it.")
    elif selected.lower().endswith((".png", ".jpg", ".jpeg")):
        with st.expander("Preview"):
            st.image(data)

    if allow_email:
        st.markdown("**Email this file**")
        try:
            companies = [c for c in load_ordering_companies(pg) if c.get("rep_email")]
            smtp_cfg = load_smtp_settings(pg)
        except Exception as exc:
            st.error(f"Could not load companies: {exc}")
            return
        labels = {f"{c['company']} ({c['rep_email']})": c["rep_email"] for c in companies}
        picked = st.multiselect("Send to company reps", list(labels), key=f"files_to_{category}")
        extra = st.text_input("Other recipient email(s), comma-separated", key=f"files_extra_{category}")
        subject = st.text_input("Subject", value=f"Liberty Smokes - {Path(selected).stem}", key=f"files_subj_{category}")
        message = st.text_area(
            "Message",
            value="Hello,\n\nPlease find our updated license attached.",
            key=f"files_msg_{category}",
        )
        if st.button("Send file", type="primary", key=f"files_send_{category}"):
            recipients = [labels[p] for p in picked] + [
                parseaddr(x)[1] for x in re.split(r"[,;\s]+", extra) if "@" in x
            ]
            recipients = list(dict.fromkeys(r for r in recipients if r))
            if not recipients:
                st.warning("Choose at least one recipient.")
            elif not all(smtp_cfg.get(k) for k in ("host", "port", "from_addr", "password")):
                st.error("Set up the Member email (SMTP) in Settings first.")
            else:
                sent, failed = 0, []
                for addr in recipients:
                    try:
                        send_email(
                            smtp_cfg["host"],
                            int(smtp_cfg["port"]),
                            smtp_cfg.get("username", ""),
                            smtp_cfg.get("password", ""),
                            addr,
                            subject,
                            message,
                            security=smtp_cfg.get("security", "SSL"),
                            from_addr=smtp_cfg.get("from_addr", ""),
                            attachments=[
                                {
                                    "filename": selected,
                                    "mime_type": mimetypes.guess_type(selected)[0] or "application/octet-stream",
                                    "content": data,
                                }
                            ],
                        )
                        sent += 1
                    except Exception:
                        failed.append(addr)
                if sent:
                    st.success(f"Sent to {sent} recipient(s).")
                if failed:
                    st.error("Failed: " + ", ".join(failed))


def page_files(pg: SyncPostgrestClient):
    st.header("Files")
    st.caption("Store licenses and invoices securely, and email licenses to companies.")
    try:
        resp = requests.post(
            _storage_url(f"object/list/{FILES_BUCKET}"),
            headers=_storage_headers(),
            json={"prefix": "", "limit": 1},
            timeout=20,
        )
        bucket_ready = resp.status_code == 200
    except Exception as exc:
        st.error(f"Could not reach Supabase Storage: {exc}")
        return
    if not bucket_ready:
        st.warning("One-time setup needed: run this in the Supabase dashboard (SQL Editor), then refresh.")
        st.code(FILES_SETUP_SQL, language="sql")
        return
    tabs = st.tabs(FILES_CATEGORIES)
    for tab, category in zip(tabs, FILES_CATEGORIES):
        with tab:
            if category == "Invoices":
                _render_invoices_section(pg)
            else:
                _render_files_section(pg, category, allow_email=(category == "Licenses"))
