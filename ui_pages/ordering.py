import base64
import hashlib
import io
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode, urlparse

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from postgrest import SyncPostgrestClient

from files.storage import _safe_storage_name, storage_download_file


PRICE_LIST_TYPES = ["pdf", "xlsx", "xls", "csv", "png", "jpg", "jpeg"]


def _render_zoomable_pages(pages_b64: list[str], key: str = "", height: int = 600):
    imgs = "".join(f'<img src="data:image/png;base64,{b}">' for b in pages_b64)
    html = """
<style>
body{margin:0;font-family:sans-serif;background:#111}
#bar{display:flex;gap:6px;padding:6px;background:#222}
#bar button{flex:1;padding:8px;font-size:16px;border:0;border-radius:6px;background:#444;color:#fff}
#box{position:relative;overflow:auto;height:calc(100vh - 50px);background:#111;touch-action:pan-x pan-y;-webkit-overflow-scrolling:touch}
#inner{width:100%}
#inner img{width:100%;display:block;margin-bottom:6px;background:#fff;pointer-events:none}
</style>
<div id="wrap"><div id="bar"><button id="out">&minus;</button><button id="fit">Fit</button><button id="in">+</button><button id="fs">Fullscreen</button></div>
<div id="box"><div id="inner">__IMGS__</div></div></div>
<script>
const box=document.getElementById('box'),inner=document.getElementById('inner');
let zoom=100,dist=0,startZoom=100;
function setZoom(z,cx,cy){
  z=Math.max(100,Math.min(500,z));
  const r=z/zoom, x=(box.scrollLeft+(cx??box.clientWidth/2))*r-(cx??box.clientWidth/2), y=(box.scrollTop+(cy??box.clientHeight/2))*r-(cy??box.clientHeight/2);
  zoom=z; inner.style.width=zoom+'%'; box.scrollLeft=x; box.scrollTop=y;
}
const d=t=>Math.hypot(t[0].clientX-t[1].clientX,t[0].clientY-t[1].clientY);
box.addEventListener('touchstart',e=>{if(e.touches.length==2){dist=d(e.touches);startZoom=zoom;}},{passive:true});
box.addEventListener('touchmove',e=>{if(e.touches.length==2&&dist){e.preventDefault();
  const rect=box.getBoundingClientRect();
  const cx=(e.touches[0].clientX+e.touches[1].clientX)/2-rect.left, cy=(e.touches[0].clientY+e.touches[1].clientY)/2-rect.top;
  setZoom(startZoom*d(e.touches)/dist,cx,cy);}},{passive:false});
box.addEventListener('touchend',e=>{if(e.touches.length<2)dist=0;});
box.addEventListener('wheel',e=>{if(e.ctrlKey){e.preventDefault();setZoom(zoom*(e.deltaY<0?1.1:0.9),e.offsetX,e.offsetY);}},{passive:false});
document.getElementById('in').onclick=()=>setZoom(zoom*1.3);
document.getElementById('out').onclick=()=>setZoom(zoom/1.3);
document.getElementById('fit').onclick=()=>setZoom(100);
document.getElementById('fs').onclick=()=>{const w=document.getElementById('wrap');
  if(document.fullscreenElement){document.exitFullscreen()}else if(w.requestFullscreen){w.requestFullscreen()}};
</script>
""".replace("__IMGS__", imgs)
    components.html(html, height=height, scrolling=False)


def _render_price_list_file(pg: SyncPostgrestClient, company_id: str, name: str):
    from app import _price_list_prefix, remove_company_price_list

    try:
        data = storage_download_file(_price_list_prefix(company_id), name)
    except Exception as exc:
        st.error(f"Could not load this price list: {exc}")
        return
    suffix = Path(name).suffix.lower()
    fkey = hashlib.sha1(f"{company_id}/{name}".encode("utf-8")).hexdigest()[:10]
    try:
        if suffix == ".pdf":
            import fitz

            with fitz.open(stream=data, filetype="pdf") as doc:
                pages_b64 = [
                    base64.b64encode(page.get_pixmap(dpi=150).tobytes("png")).decode()
                    for page in doc
                ]
            _render_zoomable_pages(pages_b64, key=fkey)
        elif suffix in {".png", ".jpg", ".jpeg"}:
            st.image(data)
        elif suffix == ".csv":
            st.dataframe(pd.read_csv(io.BytesIO(data)), width="stretch")
        elif suffix in {".xlsx", ".xls"}:
            st.dataframe(pd.read_excel(io.BytesIO(data)), width="stretch")
    except Exception:
        st.info("Preview unavailable for this file. Use Download to open it.")
    col_dl, col_rm = st.columns(2)
    col_dl.download_button("Download", data=data, file_name=name, key=f"price_list_download_{fkey}")
    if col_rm.button("Remove", key=f"price_list_remove_{fkey}"):
        remove_company_price_list(pg, company_id, name)
        st.rerun()


def _render_company_price_list(pg: SyncPostgrestClient, company_id: str, company_name: str, file_name: str):
    from app import (
        list_company_price_lists,
        load_price_list_labels,
        save_company_price_list,
        set_price_list_label,
    )

    try:
        files = list_company_price_lists(company_id)
    except Exception as exc:
        st.warning(f"Price lists unavailable: {exc}")
        files = []
    labels = load_price_list_labels(pg)

    def label_of(f: str) -> str:
        return labels.get(f"{company_id}/{f}") or Path(f).stem

    title = f"{company_name} Price Lists ({len(files)})" if len(files) > 1 else f"{company_name} Price List"
    with st.expander(title, expanded=False):
        if not files:
            st.caption("No price list uploaded for this company yet.")
        else:
            chosen = files[0]
            if len(files) > 1:
                chosen = st.selectbox(
                    "Price list",
                    files,
                    format_func=label_of,
                    key=f"price_list_pick_{company_id}",
                )
            fkey = hashlib.sha1(f"{company_id}/{chosen}".encode("utf-8")).hexdigest()[:10]
            name_col, btn_col = st.columns([3, 1], vertical_alignment="bottom")
            new_label = name_col.text_input(
                "Name (e.g. the company this list is for)",
                value=label_of(chosen),
                key=f"price_list_label_{fkey}",
            )
            if btn_col.button("Rename", key=f"price_list_rename_{fkey}"):
                set_price_list_label(pg, company_id, chosen, new_label)
                st.rerun()
            st.caption(f"File: {chosen}")
            _render_price_list_file(pg, company_id, chosen)

        gen_key = f"price_list_gen_{company_id}"
        gen = st.session_state.get(gen_key, 0)
        uploads = st.file_uploader(
            "Upload price list(s) — add as many as this rep needs",
            type=PRICE_LIST_TYPES,
            accept_multiple_files=True,
            key=f"price_list_upload_{company_id}_{gen}",
        )
        upload_names = {}
        for i, upload in enumerate(uploads or []):
            upload_names[i] = st.text_input(
                f"Name for {upload.name} (each name is saved as its own list)",
                value=Path(upload.name).stem,
                key=f"price_list_newname_{company_id}_{gen}_{i}",
            )
        if uploads and st.button("Save price list(s)", key=f"price_list_save_{company_id}", type="primary"):
            for i, upload in enumerate(uploads):
                label = upload_names.get(i, "").strip() or Path(upload.name).stem
                stored = _safe_storage_name(label + Path(upload.name).suffix)
                save_company_price_list(pg, company_id, stored, upload.getvalue())
                set_price_list_label(pg, company_id, stored, label)
            st.session_state[gen_key] = gen + 1
            st.rerun()


def _render_company_directory_editor(pg: SyncPostgrestClient):
    from app import load_ordering_companies, save_ordering_companies

    companies = load_ordering_companies(pg)
    with st.expander("Company Directory (Ordering)", expanded=False):
        st.caption("Add a company or change its sales rep, website or payment terms. These show on the Ordering page.")
        company_editor_rows = [
            {
                "Company": str(company.get("company") or ""),
                "Sales Rep Name": str(company.get("rep_name") or ""),
                "Rep Email": str(company.get("rep_email") or ""),
                "Rep Phone": str(company.get("rep_phone") or ""),
                "Brands": str(company.get("rep_brands") or ""),
                "Ordering Website": str(company.get("ordering_url") or ""),
                "Payment Terms": str(company.get("payment_terms") or ""),
                "Active": bool(company.get("active", True)),
            }
            for company in companies
        ]
        edited_company_rows = st.data_editor(
            company_editor_rows,
            num_rows="dynamic",
            width="stretch",
            key="ordering_company_editor_v2",
            column_config={
                "Payment Terms": st.column_config.SelectboxColumn(
                    "Payment Terms",
                    options=["", "Net 30 days", "Credit Card", "Cash", "Check"],
                    help="Choose the payment terms for this company.",
                )
            },
        )
        updated_companies = []
        existing_map = {str(c.get("company") or "").strip().lower(): c for c in companies}
        for row in edited_company_rows or []:
            company_name = str((row or {}).get("Company") or "").strip()
            if not company_name:
                continue
            key_name = company_name.lower()
            existing = existing_map.get(key_name, {})
            company_id = str(existing.get("id") or hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:12])
            updated_companies.append(
                {
                    "id": company_id,
                    "company": company_name,
                    "rep_name": str((row or {}).get("Sales Rep Name") or "").strip(),
                    "rep_email": parseaddr(str((row or {}).get("Rep Email") or "").strip())[1].strip(),
                    "rep_phone": str((row or {}).get("Rep Phone") or "").strip(),
                    "rep_brands": str((row or {}).get("Brands") or "").strip(),
                    "ordering_url": str((row or {}).get("Ordering Website") or "").strip(),
                    "payment_terms": str((row or {}).get("Payment Terms") or "").strip(),
                    "price_list_file": str(existing.get("price_list_file") or ""),
                    "active": (row or {}).get("Active") is not False and not (
                        (row or {}).get("Active") is None and key_name in existing_map and not existing.get("active", True)
                    ),
                    "source_file": str(existing.get("source_file") or ""),
                    "order_note": str(existing.get("order_note") or ""),
                    "order_rows": list(existing.get("order_rows") or []),
                }
            )

        visible_company_rows = [
            {
                "Company": company["company"],
                "Sales Rep Name": company["rep_name"],
                "Rep Email": company["rep_email"],
                "Rep Phone": company["rep_phone"],
                "Brands": company["rep_brands"],
                "Ordering Website": company["ordering_url"],
                "Payment Terms": company["payment_terms"],
                "Active": company["active"],
            }
            for company in updated_companies
        ]
        if visible_company_rows != company_editor_rows:
            save_ordering_companies(pg, updated_companies)
            st.toast("Company directory changes saved.")


def page_ordering(pg: SyncPostgrestClient):
    from app import (
        _uploads_to_attachments,
        load_ordering_companies,
        load_ordering_smtp_settings,
        load_smtp_settings,
        send_email,
    )

    st.header("Ordering")
    st.caption("Select a company to view its ordering details and email its sales rep. Add or edit companies in Settings.")

    active_companies = [company for company in load_ordering_companies(pg) if company.get("active")]
    company_options = {
        str(company.get("company") or "Unknown Company"): company
        for company in active_companies
    }
    company_choice = st.selectbox(
        "Company",
        list(company_options) if company_options else ["No active companies"],
        key="ordering_company_pick",
        disabled=not bool(company_options),
    )
    selected_company = company_options.get(company_choice) or {}
    selected_company_id = str(selected_company.get("id") or "").strip()

    if not selected_company:
        st.info("Add an active company in the Companies directory to start an email.")
    else:
        st.subheader(f"Email {selected_company.get('company', 'Sales Rep')}")
        rep_name = str(selected_company.get("rep_name") or "").strip()
        rep_email = parseaddr(str(selected_company.get("rep_email") or "").strip())[1].strip()
        rep_phone = str(selected_company.get("rep_phone") or "").strip()
        rep_brands = str(selected_company.get("rep_brands") or "").strip()
        st.write("**Sales rep:** " + (rep_name or "Not set") + (f"  ·  {rep_phone}" if rep_phone else ""))
        st.caption(f"Brands: {rep_brands}" if rep_brands else "No brands listed for this sales rep.")

        gmail_address = rep_email
        if gmail_address and "@" in gmail_address:
            gmail_url = "https://mail.google.com/mail/?" + urlencode(
                {"view": "cm", "fs": "1", "to": gmail_address}
            )
            st.link_button(gmail_address, gmail_url)

        ordering_url = str(selected_company.get("ordering_url") or "").strip()
        if ordering_url and "://" not in ordering_url:
            ordering_url = f"https://{ordering_url}"
        parsed_ordering_url = urlparse(ordering_url)
        if parsed_ordering_url.scheme.lower() in {"http", "https"} and parsed_ordering_url.netloc:
            st.link_button(ordering_url, ordering_url)
        payment_terms = str(selected_company.get("payment_terms") or "").strip()
        if payment_terms:
            st.caption(f"Payment terms: {payment_terms}")

        _render_company_price_list(
            pg,
            selected_company_id,
            str(selected_company.get("company") or ""),
            str(selected_company.get("price_list_file") or ""),
        )

        email_subject = st.text_input(
            "Subject",
            value=f"Liberty Smokes - {selected_company.get('company', '')}",
            key=f"ordering_email_subject_{selected_company_id}",
        )
        email_body = st.text_area(
            "Message",
            key=f"ordering_email_body_{selected_company_id}",
            height=180,
        )

        att_gen = st.session_state.get("ordering_att_gen", 0)
        sent_msg = st.session_state.pop("ordering_sent_msg", "")
        if sent_msg:
            st.success(sent_msg)
        email_files = st.file_uploader(
            "Attach files or images (optional)",
            accept_multiple_files=True,
            key=f"ordering_email_files_{selected_company_id}_{att_gen}",
        )

        member_smtp = load_smtp_settings(pg)
        ordering_smtp = load_ordering_smtp_settings(pg)
        rep_smtp_configured = all(ordering_smtp.get(k) for k in ("host", "from_addr", "password"))
        sender_choice = "Member SMTP (current default)"
        if rep_smtp_configured:
            sender_choice = st.radio(
                "Send using",
                ["Member SMTP (current default)", "Sales Rep SMTP profile"],
                key="ordering_sender_choice",
                horizontal=True,
            )
        smtp = ordering_smtp if sender_choice == "Sales Rep SMTP profile" else member_smtp
        smtp_ready = bool(smtp.get("host")) and bool(smtp.get("port")) and bool(smtp.get("from_addr")) and bool(smtp.get("password"))
        if not smtp_ready:
            st.info("Configure the selected SMTP profile in Settings before sending.")

        if st.button("Send Email", key="ordering_send_email", type="primary"):
            normalized_rep_email = parseaddr(str(rep_email or "").strip())[1].strip()
            if not smtp_ready:
                st.warning("SMTP is not configured.")
            elif not normalized_rep_email:
                st.warning("Sales rep email is missing or invalid.")
            elif not email_subject.strip():
                st.warning("Enter an email subject.")
            else:
                try:
                    send_email(
                        smtp["host"],
                        int(smtp["port"]),
                        smtp.get("username", ""),
                        smtp.get("password", ""),
                        normalized_rep_email,
                        email_subject.strip(),
                        email_body,
                        security=smtp.get("security", "SSL"),
                        from_addr=smtp.get("from_addr", ""),
                        attachments=_uploads_to_attachments(email_files),
                    )
                    st.session_state["ordering_att_gen"] = att_gen + 1
                    st.session_state["ordering_sent_msg"] = f"Email sent to {rep_name or selected_company.get('company', '')} ({normalized_rep_email})."
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to send email: {exc}")
