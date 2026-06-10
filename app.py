import calendar
import base64
import csv
import datetime
import hashlib
import io
import importlib
import json
import socket
from pathlib import Path
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import urlparse

import requests
import streamlit as st
from postgrest import SyncPostgrestClient
import streamlit.components.v1 as components
from streamlit.errors import StreamlitAPIException

st.set_page_config(page_title="Liberty Smokes", page_icon="🪑", layout="wide")


def get_sidebar_logo_path() -> Path | None:
    """Return the first existing local logo path for the sidebar."""
    candidates = [
        Path("assets/logo.png"),
        Path("logo.png"),
        Path("static/logo.png"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def apply_mobile_styles(enabled: bool = True):
    """Apply lightweight responsive tweaks so the app works better on phones."""
    if not enabled:
        return
    st.markdown(
        """
        <style>
        @media (max-width: 900px) {
            .block-container {
                padding-top: 0.75rem;
                padding-left: 0.75rem;
                padding-right: 0.75rem;
            }

            div[data-testid="stSidebar"] {
                min-width: 82vw !important;
                max-width: 82vw !important;
            }

            div[data-testid="column"] {
                min-width: 100% !important;
                flex: 1 1 100% !important;
            }

            .stButton > button {
                width: 100%;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── DB client ──────────────────────────────────────────────────────────────────

def get_postgrest_client() -> SyncPostgrestClient:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("Missing SUPABASE_URL or SUPABASE_KEY in .streamlit/secrets.toml.")
        st.stop()
    parsed = urlparse(str(url).strip())
    host = (parsed.hostname or "").strip()
    if not host:
        st.error("SUPABASE_URL is invalid. It must be a full URL, for example: https://your-project-ref.supabase.co")
        st.stop()
    try:
        socket.getaddrinfo(host, parsed.port or 443)
    except socket.gaierror:
        st.error(
            "Could not resolve the Supabase hostname from SUPABASE_URL. "
            "Check .streamlit/secrets.toml for typos in SUPABASE_URL and verify your internet/DNS connection."
        )
        st.stop()
    return SyncPostgrestClient(
        f"{url.rstrip('/')}/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )


# ── Utilities ──────────────────────────────────────────────────────────────────

GLOBAL_PENDING_WIDGET_RESET_KEY = "pending_global_widget_reset"

def add_one_month(date_str: str) -> str:
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    month, year = dt.month, dt.year
    if month == 12:
        month, year = 1, year + 1
    else:
        month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day).strftime("%Y-%m-%d")


def advance_billing(current_due: str, tier: str, months: int) -> str:
    if tier == "Annual":
        dt = datetime.datetime.strptime(current_due, "%Y-%m-%d").date()
        return dt.replace(year=dt.year + months).strftime("%Y-%m-%d")
    result = current_due
    for _ in range(months):
        result = add_one_month(result)
    return result


def month_start_for(date_value: datetime.date | None = None) -> str:
    date_value = date_value or datetime.date.today()
    return date_value.replace(day=1).strftime("%Y-%m-%d")


def month_label(month_start: str) -> str:
    try:
        dt = datetime.datetime.strptime(str(month_start), "%Y-%m-%d").date()
        return dt.strftime("%B %Y")
    except Exception:
        return str(month_start)


WEEKDAY_OPTIONS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_TO_INDEX = {name: idx for idx, name in enumerate(WEEKDAY_OPTIONS)}


def _normalize_weekday_name(value: str, default: str = "Friday") -> str:
    raw = str(value or "").strip().capitalize()
    return raw if raw in WEEKDAY_TO_INDEX else default


def _weekday_range_indices(start_day: str, end_day: str) -> set[int]:
    start_idx = WEEKDAY_TO_INDEX.get(_normalize_weekday_name(start_day), 4)
    end_idx = WEEKDAY_TO_INDEX.get(_normalize_weekday_name(end_day, "Sunday"), 6)
    out = {start_idx}
    while start_idx != end_idx:
        start_idx = (start_idx + 1) % 7
        out.add(start_idx)
    return out


def _weekday_label(start_day: str, end_day: str) -> str:
    return f"{_normalize_weekday_name(start_day)} through {_normalize_weekday_name(end_day, 'Sunday')}"


def sale_month_start(created_at: str) -> str:
    raw = str(created_at or "").strip()
    if len(raw) >= 10:
        maybe_date = raw[:10]
        try:
            dt = datetime.datetime.strptime(maybe_date, "%Y-%m-%d").date()
            return dt.replace(day=1).strftime("%Y-%m-%d")
        except Exception:
            pass
    return month_start_for()


def compute_sale_cost_from_items(items: list[dict]) -> float:
    total = 0.0
    for item in items or []:
        qty = int(item.get("qty") or 0)
        unit_cost = float(item.get("unit_cost", item.get("cost", 0.0)) or 0.0)
        total += unit_cost * qty
    return round(total, 2)


def reset_widget_state(
    defaults: dict[str, object],
    fallback_reset_key: str | None = None,
) -> bool:
    deferred_key = fallback_reset_key or GLOBAL_PENDING_WIDGET_RESET_KEY
    for key, value in defaults.items():
        try:
            st.session_state[key] = value
        except StreamlitAPIException:
            # If the widget is already instantiated in this run, defer reset to next rerun.
            queue_widget_reset(defaults, deferred_key)
            return False
    return True


def queue_widget_reset(defaults: dict[str, object], reset_key: str):
    existing = st.session_state.get(reset_key)
    if isinstance(existing, dict):
        merged = dict(existing)
        merged.update(defaults)
        st.session_state[reset_key] = merged
    else:
        st.session_state[reset_key] = defaults


# ── Seat helpers ───────────────────────────────────────────────────────────────

def fetch_seats(pg: SyncPostgrestClient):
    try:
        return (
            pg.from_("seats")
            .select(
                "seat_number, customer_name, drinks_consumed, alcoholic_drinks, "
                "non_alcoholic_drinks, drink_breakdown, is_occupied"
            )
            .order("seat_number")
            .execute()
            .data
            or []
        )
    except Exception:
        # Backward compatibility for schemas that don't yet have the new columns.
        return (
            pg.from_("seats")
            .select("seat_number, customer_name, drinks_consumed, is_occupied")
            .order("seat_number")
            .execute()
            .data
            or []
        )


def check_in(pg: SyncPostgrestClient, seat_number: int, customer_name: str):
    payload = {
        "customer_name": customer_name.strip(),
        "drinks_consumed": 0,
        "alcoholic_drinks": 0,
        "non_alcoholic_drinks": 0,
        "drink_breakdown": {},
        "is_occupied": True,
    }
    try:
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()
    except Exception:
        payload.pop("drink_breakdown", None)
        payload.pop("alcoholic_drinks", None)
        payload.pop("non_alcoholic_drinks", None)
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()


def add_drink_type(
    pg: SyncPostgrestClient,
    seat_number: int,
    alcoholic_drinks: int,
    non_alcoholic_drinks: int,
    drink_type: str,
):
    if drink_type == "alcoholic":
        alcoholic_drinks += 1
    else:
        non_alcoholic_drinks += 1

    payload = {
        "alcoholic_drinks": alcoholic_drinks,
        "non_alcoholic_drinks": non_alcoholic_drinks,
        "drinks_consumed": alcoholic_drinks + non_alcoholic_drinks,
    }

    try:
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()
    except Exception:
        pg.from_("seats").update(
            {"drinks_consumed": alcoholic_drinks + non_alcoholic_drinks}
        ).eq("seat_number", seat_number).execute()


def add_named_drink(
    pg: SyncPostgrestClient,
    seat_number: int,
    alcoholic_drinks: int,
    non_alcoholic_drinks: int,
    drink_type: str,
    drink_name: str,
    drink_breakdown,
):
    if drink_type == "alcoholic":
        alcoholic_drinks += 1
    else:
        non_alcoholic_drinks += 1

    breakdown = _parse_drink_breakdown(drink_breakdown)
    if drink_name:
        breakdown[drink_name] = int(breakdown.get(drink_name, 0)) + 1

    payload = {
        "alcoholic_drinks": alcoholic_drinks,
        "non_alcoholic_drinks": non_alcoholic_drinks,
        "drinks_consumed": alcoholic_drinks + non_alcoholic_drinks,
        "drink_breakdown": breakdown,
    }
    try:
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()
    except Exception:
        payload.pop("drink_breakdown", None)
        try:
            pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()
        except Exception:
            pg.from_("seats").update(
                {"drinks_consumed": alcoholic_drinks + non_alcoholic_drinks}
            ).eq("seat_number", seat_number).execute()


def clear_seat(pg: SyncPostgrestClient, seat_number: int):
    payload = {
        "customer_name": None,
        "drinks_consumed": 0,
        "alcoholic_drinks": 0,
        "non_alcoholic_drinks": 0,
        "drink_breakdown": {},
        "is_occupied": False,
    }
    try:
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()
    except Exception:
        payload.pop("drink_breakdown", None)
        payload.pop("alcoholic_drinks", None)
        payload.pop("non_alcoholic_drinks", None)
        pg.from_("seats").update(payload).eq("seat_number", seat_number).execute()


def add_seat(pg: SyncPostgrestClient, seat_number: int):
    pg.from_("seats").insert({"seat_number": seat_number}).execute()


def find_member_by_customer_name(customer_name: str, members: list) -> dict | None:
    """Return a matching active member row from a seat customer name."""
    if not customer_name or not members:
        return None
    name_lower = customer_name.lower().strip()
    for m in members:
        full = f"{m.get('first_name', '')} {m.get('last_name', '')}".lower().strip()
        full_rev = f"{m.get('last_name', '')}, {m.get('first_name', '')}".lower().strip()
        if name_lower in (full, full_rev):
            return m
    return None


def is_member(customer_name: str, members: list) -> bool:
    """Return True if customer_name matches any active member (first last or last, first)."""
    return find_member_by_customer_name(customer_name, members) is not None


# ── Member helpers ─────────────────────────────────────────────────────────────

def _locker_sort_key(member: dict):
    locker_text = str(member.get("locker") or "").strip()
    try:
        locker_number = int(locker_text)
        return (
            0,
            locker_number,
            str(member.get("last_name") or "").lower(),
            str(member.get("first_name") or "").lower(),
            int(member.get("id") or 0),
        )
    except Exception:
        return (
            1,
            float("inf"),
            locker_text.lower(),
            str(member.get("last_name") or "").lower(),
            str(member.get("first_name") or "").lower(),
            int(member.get("id") or 0),
        )

def fetch_members(pg: SyncPostgrestClient):
    try:
        members = (
            pg.from_("members")
            .select(
                "id, first_name, last_name, email, phone, tier, status, locker, gift_card_number, "
                "join_date, next_billing_date, last_reminder"
            )
            .execute()
            .data or []
        )
    except Exception:
        members = (
            pg.from_("members")
            .select(
                "id, first_name, last_name, email, tier, status, locker, "
                "join_date, next_billing_date, last_reminder"
            )
            .execute()
            .data or []
        )
    members = mark_overdue_members_inactive(pg, members)
    return sorted(members, key=_locker_sort_key)


def mark_overdue_members_inactive(pg: SyncPostgrestClient, members: list[dict]) -> list[dict]:
    today = datetime.date.today()
    normalized_members: list[dict] = []
    status_updates: list[tuple[int, str]] = []

    for member in members:
        normalized_member = dict(member)
        status = str(normalized_member.get("status") or "").strip()
        if status.lower() in {"active", "past due"}:
            try:
                due = datetime.datetime.strptime(
                    str(normalized_member.get("next_billing_date") or "").strip(),
                    "%Y-%m-%d",
                ).date()
            except Exception:
                due = None
            if due and due < today:
                overdue_days = (today - due).days
                next_status = "Inactive" if overdue_days > 7 else "Past Due"
                normalized_member["status"] = next_status
                member_id = normalized_member.get("id")
                if member_id not in {None, ""}:
                    status_updates.append((int(member_id), next_status))
        normalized_members.append(normalized_member)

    for member_id, next_status in status_updates:
        try:
            pg.from_("members").update({"status": next_status}).eq("id", member_id).execute()
        except Exception:
            pass

    return normalized_members


def update_member_gift_card_number(
    pg: SyncPostgrestClient,
    member_id,
    gift_card_number: str,
) -> tuple[bool, str]:
    if member_id in {None, ""}:
        return False, "Member is required."
    number_text = str(gift_card_number or "").strip()
    try:
        pg.from_("members").update({"gift_card_number": number_text}).eq("id", member_id).execute()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def member_locker_label(member: dict) -> str:
    full_name = f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", ")
    locker = str(member.get("locker") or "").strip() or "—"
    if full_name:
        return f"Locker {locker} | {full_name}"
    return f"Locker {locker}"


def _enroll_member_in_loyalty(
    pg: SyncPostgrestClient,
    member_id: str,
    first_name: str,
    last_name: str,
    phone: str = "",
    email: str = "",
):
    """Create or link a loyalty contact for a newly created member.

    Safe to call multiple times — merges by phone/name so no duplicate is created.
    Must be called after the settings helpers are defined (load/save_pos_loyalty_*).
    """
    try:
        existing = load_pos_loyalty_customers(pg)
        points = load_pos_loyalty_points(pg)

        cid = "lc_m" + str(member_id)
        incoming = [
            {
                "id": cid,
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": email,
                "member_id": str(member_id),
                "external_id": "",
                "source": "member",
            }
        ]
        merged, _added, _updated, _imported_points = merge_loyalty_contacts(existing, incoming)
        # Ensure every contact that phone/name-matches the new member ID gets linked.
        for contact in merged:
            if contact.get("member_id") in {None, ""} and str(contact.get("id")) == cid:
                contact["member_id"] = str(member_id)
        # Initialize points to 0 for this member if not already set.
        mid_key = str(member_id)
        if mid_key not in points:
            points[mid_key] = 0
        save_pos_loyalty_customers(pg, merged)
        save_pos_loyalty_points(pg, points)
    except Exception:
        # Never block member creation if loyalty enrolment fails.
        pass


def add_member(pg: SyncPostgrestClient, first_name, last_name, email, phone, tier, locker, months):
    today = datetime.date.today().strftime("%Y-%m-%d")
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "tier": tier,
        "status": "Active",
        "locker": locker or "—",
        "join_date": today,
        "next_billing_date": advance_billing(today, tier, months),
        "last_reminder": "None",
    }
    inserted_id = None
    try:
        resp = pg.from_("members").insert(payload).execute()
        data = getattr(resp, "data", None) or []
        if data and isinstance(data[0], dict):
            inserted_id = data[0].get("id")
    except Exception:
        payload.pop("phone", None)
        resp = pg.from_("members").insert(payload).execute()
        data = getattr(resp, "data", None) or []
        if data and isinstance(data[0], dict):
            inserted_id = data[0].get("id")

    if inserted_id is None:
        try:
            rows = (
                pg.from_("members")
                .select("id")
                .eq("first_name", str(first_name).strip())
                .eq("last_name", str(last_name).strip())
                .order("id", desc=True)
                .limit(1)
                .execute()
                .data or []
            )
            if rows:
                inserted_id = rows[0]["id"]
        except Exception:
            pass

    if inserted_id is not None:
        _enroll_member_in_loyalty(pg, str(inserted_id), str(first_name).strip(), str(last_name).strip(), str(phone or "").strip(), str(email or "").strip())

    return payload


def update_member(pg: SyncPostgrestClient, member_id, first_name, last_name, email, phone, locker, status):
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "locker": locker,
        "status": status,
    }
    try:
        pg.from_("members").update(payload).eq("id", member_id).execute()
    except Exception:
        payload.pop("phone", None)
        pg.from_("members").update(payload).eq("id", member_id).execute()


def delete_member(pg: SyncPostgrestClient, member_id):
    pg.from_("members").delete().eq("id", member_id).execute()


def process_payment(pg: SyncPostgrestClient, member_id, tier, current_due, months):
    pg.from_("members").update(
        {
            "status": "Active",
            "next_billing_date": advance_billing(current_due, tier, months),
            "last_reminder": "None",
        }
    ).eq("id", member_id).execute()


def fetch_member_monthly_drinks(
    pg: SyncPostgrestClient,
    member_id=None,
    month_start: str | None = None,
) -> list[dict]:
    try:
        query = (
            pg.from_("member_monthly_drinks")
            .select("member_id, month_start, alcoholic_drinks, non_alcoholic_drinks, total_drinks")
            .order("month_start", desc=True)
        )
        if member_id is not None:
            query = query.eq("member_id", member_id)
        if month_start:
            query = query.eq("month_start", month_start)
        return query.execute().data or []
    except Exception:
        return []


def increment_member_monthly_drinks(
    pg: SyncPostgrestClient,
    member_id,
    drink_type: str,
    amount: int = 1,
):
    if member_id in {None, ""}:
        return
    month_start = month_start_for()
    amount = max(0, int(amount or 0))
    if amount == 0:
        return

    try:
        rows = (
            pg.from_("member_monthly_drinks")
            .select("member_id, month_start, alcoholic_drinks, non_alcoholic_drinks")
            .eq("member_id", member_id)
            .eq("month_start", month_start)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            row = rows[0]
            alc = int(row.get("alcoholic_drinks") or 0)
            non_alc = int(row.get("non_alcoholic_drinks") or 0)
            if drink_type == "alcoholic":
                alc += amount
            else:
                non_alc += amount
            pg.from_("member_monthly_drinks").update(
                {
                    "alcoholic_drinks": alc,
                    "non_alcoholic_drinks": non_alc,
                }
            ).eq("member_id", member_id).eq("month_start", month_start).execute()
            return

        payload = {
            "member_id": member_id,
            "month_start": month_start,
            "alcoholic_drinks": amount if drink_type == "alcoholic" else 0,
            "non_alcoholic_drinks": amount if drink_type != "alcoholic" else 0,
        }
        pg.from_("member_monthly_drinks").insert(payload).execute()
    except Exception:
        # Do not block seat operations if monthly analytics storage is unavailable.
        return


def fetch_member_monthly_refills(
    pg: SyncPostgrestClient,
    member_id=None,
    month_start: str | None = None,
) -> list[dict]:
    try:
        query = (
            pg.from_("member_monthly_refills")
            .select("member_id, month_start, amount, refilled_at, notes")
            .order("month_start", desc=True)
            .order("member_id")
        )
        if member_id is not None:
            query = query.eq("member_id", member_id)
        if month_start:
            query = query.eq("month_start", month_start)
        return query.execute().data or []
    except Exception:
        return []


def mark_member_monthly_refill(
    pg: SyncPostgrestClient,
    member_id,
    month_start: str | None = None,
    amount: float = 25.0,
    notes: str = "",
) -> tuple[bool, str]:
    if member_id in {None, ""}:
        return False, "Member is required."
    month_start = month_start or month_start_for()
    amount = round(max(0.0, float(amount or 0.0)), 2)
    note_text = str(notes or "").strip()
    now_iso = datetime.datetime.utcnow().isoformat()

    try:
        rows = (
            pg.from_("member_monthly_refills")
            .select("member_id")
            .eq("member_id", member_id)
            .eq("month_start", month_start)
            .limit(1)
            .execute()
            .data
            or []
        )
        payload = {
            "amount": amount,
            "notes": note_text,
            "refilled_at": now_iso,
        }
        if rows:
            (
                pg.from_("member_monthly_refills")
                .update(payload)
                .eq("member_id", member_id)
                .eq("month_start", month_start)
                .execute()
            )
        else:
            payload.update(
                {
                    "member_id": member_id,
                    "month_start": month_start,
                }
            )
            pg.from_("member_monthly_refills").insert(payload).execute()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def unmark_member_monthly_refill(
    pg: SyncPostgrestClient,
    member_id,
    month_start: str | None = None,
) -> tuple[bool, str]:
    if member_id in {None, ""}:
        return False, "Member is required."
    month_start = month_start or month_start_for()
    try:
        (
            pg.from_("member_monthly_refills")
            .delete()
            .eq("member_id", member_id)
            .eq("month_start", month_start)
            .execute()
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def fetch_daily_sales_ledger(
    pg: SyncPostgrestClient,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    try:
        query = (
            pg.from_("daily_sales_ledger")
            .select(
                "sale_date, cash_sales, credit_sales, cash_taken, cash_deposit, notes, updated_at"
            )
            .order("sale_date", desc=True)
        )
        if start_date:
            query = query.gte("sale_date", start_date)
        if end_date:
            query = query.lte("sale_date", end_date)
        return query.execute().data or []
    except Exception:
        return []


def save_daily_sales_ledger_entry(
    pg: SyncPostgrestClient,
    sale_date: str,
    cash_sales: float,
    credit_sales: float,
    cash_taken: float = 0.0,
    cash_deposit: float = 0.0,
    notes: str = "",
) -> tuple[bool, str]:
    sale_date = str(sale_date or "").strip()
    if not sale_date:
        return False, "Ledger date is required."

    payload = {
        "cash_sales": round(max(0.0, float(cash_sales or 0.0)), 2),
        "credit_sales": round(max(0.0, float(credit_sales or 0.0)), 2),
        "cash_taken": round(max(0.0, float(cash_taken or 0.0)), 2),
        "cash_deposit": round(max(0.0, float(cash_deposit or 0.0)), 2),
        "notes": str(notes or "").strip(),
    }

    try:
        existing = (
            pg.from_("daily_sales_ledger")
            .select("sale_date, cash_sales, credit_sales, cash_taken, cash_deposit, notes")
            .eq("sale_date", sale_date)
            .limit(1)
            .execute()
            .data
            or []
        )
        previous = existing[0] if existing else None
        if existing:
            pg.from_("daily_sales_ledger").update(payload).eq("sale_date", sale_date).execute()
        else:
            payload["sale_date"] = sale_date
            pg.from_("daily_sales_ledger").insert(payload).execute()
        current = {"sale_date": sale_date, **payload}
        record_daily_sales_ledger_audit(
            pg,
            sale_date,
            "ledger_updated" if previous else "ledger_created",
            "ledger_day",
            {
                "previous": previous or {},
                "current": current,
            },
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def delete_daily_sales_ledger_entry(
    pg: SyncPostgrestClient,
    sale_date: str,
) -> tuple[bool, str]:
    sale_date = str(sale_date or "").strip()
    if not sale_date:
        return False, "Ledger date is required."
    try:
        current_rows = (
            pg.from_("daily_sales_ledger")
            .select("sale_date, cash_sales, credit_sales, cash_taken, cash_deposit, notes")
            .eq("sale_date", sale_date)
            .limit(1)
            .execute()
            .data
            or []
        )
        deduction_rows = fetch_daily_sales_cash_deductions(pg, sale_date=sale_date)
        if deduction_rows:
            pg.from_("daily_sales_cash_deductions").delete().eq("sale_date", sale_date).execute()
        pg.from_("daily_sales_ledger").delete().eq("sale_date", sale_date).execute()
        record_daily_sales_ledger_audit(
            pg,
            sale_date,
            "ledger_deleted",
            "ledger_day",
            {
                "deleted": current_rows[0] if current_rows else {"sale_date": sale_date},
                "deleted_deductions": deduction_rows,
            },
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def ledger_month_start(sale_date: str) -> str:
    raw = str(sale_date or "").strip()
    try:
        dt = datetime.datetime.strptime(raw, "%Y-%m-%d").date()
        return month_start_for(dt)
    except Exception:
        return month_start_for()


def build_daily_ledger_display_row(row: dict) -> dict:
    cash_sales = round(float(row.get("cash_sales") or 0.0), 2)
    credit_sales = round(float(row.get("credit_sales") or 0.0), 2)
    cash_taken = round(float(row.get("cash_taken") or 0.0), 2)
    cash_deposit = round(float(row.get("cash_deposit") or 0.0), 2)
    closed_register_withdrawn = round(float(row.get("closed_register_withdrawn") or 0.0), 2)
    remaining_closed_cash = round(cash_deposit - closed_register_withdrawn, 2)
    total_sales = round(cash_sales + credit_sales, 2)
    expected_deposit = round(cash_sales - cash_taken, 2)
    deposit_variance = round(cash_deposit - expected_deposit, 2)
    return {
        "Date": str(row.get("sale_date") or ""),
        "Cash Sales": cash_sales,
        "Credit Sales": credit_sales,
        "Total Sales": total_sales,
        "Cash Taken": cash_taken,
        "Deduction Count": int(row.get("deduction_count") or 0),
        "Expected Deposit": expected_deposit,
        "Cash Deposited": cash_deposit,
        "Closed Register Withdrawn": closed_register_withdrawn,
        "Remaining Closed Cash": remaining_closed_cash,
        "Deposit Variance": deposit_variance,
        "Notes": str(row.get("notes") or ""),
        "Updated": str(row.get("updated_at") or ""),
    }


def summarize_closed_register_withdrawals(deduction_rows: list[dict]) -> dict[str, dict]:
    summary = {}
    for row in deduction_rows or []:
        source_day = str(row.get("source_sale_date") or row.get("sale_date") or "").strip()
        withdrawal_day = str(row.get("withdrawal_date") or "").strip()
        if not source_day:
            continue
        if source_day == withdrawal_day:
            continue
        bucket = summary.setdefault(
            source_day,
            {
                "closed_register_withdrawn": 0.0,
                "closed_register_withdrawal_count": 0,
            },
        )
        bucket["closed_register_withdrawn"] += round(float(row.get("amount") or 0.0), 2)
        bucket["closed_register_withdrawal_count"] += 1

    for bucket in summary.values():
        bucket["closed_register_withdrawn"] = round(
            float(bucket.get("closed_register_withdrawn") or 0.0),
            2,
        )
    return summary


def fetch_daily_sales_cash_deductions(
    pg: SyncPostgrestClient,
    sale_date: str | None = None,
    source_sale_date: str | None = None,
    withdrawal_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    try:
        query = (
            pg.from_("daily_sales_cash_deductions")
            .select("id, sale_date, source_sale_date, withdrawal_date, amount, note, created_at")
            .order("sale_date", desc=True)
            .order("created_at", desc=True)
        )
        if sale_date:
            query = query.eq("sale_date", sale_date)
        if source_sale_date:
            query = query.eq("source_sale_date", source_sale_date)
        if withdrawal_date:
            query = query.eq("withdrawal_date", withdrawal_date)
        if start_date:
            query = query.gte("sale_date", start_date)
        if end_date:
            query = query.lte("sale_date", end_date)
        return query.execute().data or []
    except Exception:
        return []


def fetch_daily_sales_ledger_audit(
    pg: SyncPostgrestClient,
    sale_date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    try:
        query = (
            pg.from_("daily_sales_ledger_audit")
            .select("id, sale_date, action, entity_type, snapshot, created_at")
            .order("created_at", desc=True)
            .limit(max(1, int(limit or 100)))
        )
        if sale_date:
            query = query.eq("sale_date", sale_date)
        return query.execute().data or []
    except Exception:
        return []


def record_daily_sales_ledger_audit(
    pg: SyncPostgrestClient,
    sale_date: str,
    action: str,
    entity_type: str,
    snapshot: dict,
):
    try:
        pg.from_("daily_sales_ledger_audit").insert(
            {
                "sale_date": str(sale_date or "").strip(),
                "action": str(action or "").strip(),
                "entity_type": str(entity_type or "").strip(),
                "snapshot": snapshot or {},
            }
        ).execute()
    except Exception:
        return


def ensure_daily_sales_ledger_row(
    pg: SyncPostgrestClient,
    sale_date: str,
):
    sale_date = str(sale_date or "").strip()
    if not sale_date:
        return
    try:
        existing = (
            pg.from_("daily_sales_ledger")
            .select("sale_date")
            .eq("sale_date", sale_date)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            return
        pg.from_("daily_sales_ledger").insert(
            {
                "sale_date": sale_date,
                "cash_sales": 0.0,
                "credit_sales": 0.0,
                "cash_taken": 0.0,
                "cash_deposit": 0.0,
                "notes": "",
            }
        ).execute()
    except Exception:
        return


def ensure_daily_sales_cash_deduction_columns(pg: SyncPostgrestClient):
    """Backfill missing fields so older rows remain compatible."""
    try:
        rows = (
            pg.from_("daily_sales_cash_deductions")
            .select("id, sale_date, source_sale_date, withdrawal_date")
            .limit(3000)
            .execute()
            .data
            or []
        )
    except Exception:
        return

    for row in rows:
        row_id = row.get("id")
        sale_date = str(row.get("sale_date") or "").strip()
        source_sale_date = str(row.get("source_sale_date") or "").strip()
        withdrawal_date = str(row.get("withdrawal_date") or "").strip()
        patch = {}
        if not source_sale_date and sale_date:
            patch["source_sale_date"] = sale_date
        if not withdrawal_date:
            patch["withdrawal_date"] = str(row.get("created_at") or "")[:10] or sale_date
        if patch and row_id not in {None, ""}:
            try:
                pg.from_("daily_sales_cash_deductions").update(patch).eq("id", row_id).execute()
            except Exception:
                continue


def sync_daily_sales_ledger_cash_taken(
    pg: SyncPostgrestClient,
    sale_date: str,
) -> float:
    sale_date = str(sale_date or "").strip()
    if not sale_date:
        return 0.0

    deductions = fetch_daily_sales_cash_deductions(pg, sale_date=sale_date)
    total = round(sum(float(row.get("amount") or 0.0) for row in deductions), 2)
    ensure_daily_sales_ledger_row(pg, sale_date)
    try:
        (
            pg.from_("daily_sales_ledger")
            .update({"cash_taken": total})
            .eq("sale_date", sale_date)
            .execute()
        )
    except Exception:
        return total
    return total


def add_daily_sales_cash_deduction(
    pg: SyncPostgrestClient,
    sale_date: str,
    amount: float,
    source_sale_date: str | None = None,
    withdrawal_date: str | None = None,
    note: str = "",
) -> tuple[bool, str]:
    sale_date = str(sale_date or "").strip()
    source_sale_date = str(source_sale_date or sale_date).strip()
    withdrawal_date = str(withdrawal_date or datetime.date.today().strftime("%Y-%m-%d")).strip()
    amount = round(max(0.0, float(amount or 0.0)), 2)
    if not sale_date or not source_sale_date:
        return False, "Source register day is required."
    if amount <= 0:
        return False, "Deduction amount must be greater than zero."

    try:
        ensure_daily_sales_ledger_row(pg, source_sale_date)
        ensure_daily_sales_cash_deduction_columns(pg)
        result = (
            pg.from_("daily_sales_cash_deductions")
            .insert(
                {
                    "sale_date": source_sale_date,
                    "source_sale_date": source_sale_date,
                    "withdrawal_date": withdrawal_date,
                    "amount": amount,
                    "note": str(note or "").strip(),
                }
            )
            .execute()
        )
        inserted = (result.data or [{}])[0]
        new_total = sync_daily_sales_ledger_cash_taken(pg, source_sale_date)
        record_daily_sales_ledger_audit(
            pg,
            source_sale_date,
            "cash_deduction_added",
            "cash_deduction",
            {
                "deduction": inserted,
                "entry_day": sale_date,
                "source_day": source_sale_date,
                "withdrawal_day": withdrawal_date,
                "cash_taken_total": new_total,
            },
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def delete_daily_sales_cash_deduction(
    pg: SyncPostgrestClient,
    deduction_id,
) -> tuple[bool, str]:
    if deduction_id in {None, ""}:
        return False, "Deduction id is required."
    try:
        rows = (
            pg.from_("daily_sales_cash_deductions")
            .select("id, sale_date, source_sale_date, withdrawal_date, amount, note, created_at")
            .eq("id", deduction_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return False, "Deduction not found."
        deduction = rows[0]
        source_sale_date = str(
            deduction.get("source_sale_date") or deduction.get("sale_date") or ""
        ).strip()
        pg.from_("daily_sales_cash_deductions").delete().eq("id", deduction_id).execute()
        new_total = sync_daily_sales_ledger_cash_taken(pg, source_sale_date)
        record_daily_sales_ledger_audit(
            pg,
            source_sale_date,
            "cash_deduction_deleted",
            "cash_deduction",
            {
                "deduction": deduction,
                "source_day": source_sale_date,
                "withdrawal_day": str(deduction.get("withdrawal_date") or ""),
                "cash_taken_total": new_total,
            },
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def update_daily_sales_cash_deduction(
    pg: SyncPostgrestClient,
    deduction_id,
    source_sale_date: str,
    withdrawal_date: str,
    amount: float,
    note: str = "",
) -> tuple[bool, str]:
    if deduction_id in {None, ""}:
        return False, "Deduction id is required."

    source_sale_date = str(source_sale_date or "").strip()
    withdrawal_date = str(withdrawal_date or "").strip()
    amount = round(max(0.0, float(amount or 0.0)), 2)
    if not source_sale_date:
        return False, "Source register day is required."
    if not withdrawal_date:
        return False, "Withdrawal date is required."
    if amount <= 0:
        return False, "Deduction amount must be greater than zero."

    try:
        rows = (
            pg.from_("daily_sales_cash_deductions")
            .select("id, sale_date, source_sale_date, withdrawal_date, amount, note, created_at")
            .eq("id", deduction_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return False, "Deduction not found."

        existing = rows[0]
        previous_source_day = str(existing.get("source_sale_date") or existing.get("sale_date") or "").strip()
        ensure_daily_sales_ledger_row(pg, source_sale_date)
        (
            pg.from_("daily_sales_cash_deductions")
            .update(
                {
                    "sale_date": source_sale_date,
                    "source_sale_date": source_sale_date,
                    "withdrawal_date": withdrawal_date,
                    "amount": amount,
                    "note": str(note or "").strip(),
                }
            )
            .eq("id", deduction_id)
            .execute()
        )

        if previous_source_day and previous_source_day != source_sale_date:
            sync_daily_sales_ledger_cash_taken(pg, previous_source_day)
        new_total = sync_daily_sales_ledger_cash_taken(pg, source_sale_date)
        record_daily_sales_ledger_audit(
            pg,
            source_sale_date,
            "cash_deduction_updated",
            "cash_deduction",
            {
                "previous": existing,
                "deduction": {
                    "id": deduction_id,
                    "source_sale_date": source_sale_date,
                    "withdrawal_date": withdrawal_date,
                    "amount": amount,
                    "note": str(note or "").strip(),
                },
                "cash_taken_total": new_total,
            },
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


def summarize_daily_sales_cash_deductions(deduction_rows: list[dict]) -> dict[str, dict]:
    summary = {}
    for row in deduction_rows or []:
        sale_date = str(row.get("source_sale_date") or row.get("sale_date") or "").strip()
        if not sale_date:
            continue
        bucket = summary.setdefault(
            sale_date,
            {
                "cash_taken": 0.0,
                "deduction_count": 0,
                "latest_created_at": "",
            },
        )
        bucket["cash_taken"] += round(float(row.get("amount") or 0.0), 2)
        bucket["deduction_count"] += 1
        created_at = str(row.get("created_at") or "")
        if created_at and created_at > str(bucket.get("latest_created_at") or ""):
            bucket["latest_created_at"] = created_at
    for bucket in summary.values():
        bucket["cash_taken"] = round(float(bucket.get("cash_taken") or 0.0), 2)
    return summary


def merge_daily_sales_ledger_rows(
    ledger_rows: list[dict],
    deduction_rows: list[dict],
) -> list[dict]:
    deduction_summary = summarize_daily_sales_cash_deductions(deduction_rows)
    closed_register_summary = summarize_closed_register_withdrawals(deduction_rows)
    merged = {}
    for row in ledger_rows or []:
        sale_date = str(row.get("sale_date") or "").strip()
        if not sale_date:
            continue
        combined = dict(row)
        deduction_info = deduction_summary.get(sale_date, {})
        closed_register_info = closed_register_summary.get(sale_date, {})
        if deduction_info.get("deduction_count", 0) > 0:
            combined["cash_taken"] = deduction_info.get("cash_taken", 0.0)
        combined["deduction_count"] = deduction_info.get("deduction_count", 0)
        combined["closed_register_withdrawn"] = closed_register_info.get("closed_register_withdrawn", 0.0)
        combined["closed_register_withdrawal_count"] = closed_register_info.get(
            "closed_register_withdrawal_count", 0
        )
        merged[sale_date] = combined

    for sale_date, deduction_info in deduction_summary.items():
        if sale_date in merged:
            continue
        merged[sale_date] = {
            "sale_date": sale_date,
            "cash_sales": 0.0,
            "credit_sales": 0.0,
            "cash_taken": deduction_info.get("cash_taken", 0.0),
            "cash_deposit": 0.0,
            "notes": "",
            "updated_at": deduction_info.get("latest_created_at", ""),
            "deduction_count": deduction_info.get("deduction_count", 0),
            "closed_register_withdrawn": 0.0,
            "closed_register_withdrawal_count": 0,
        }

    for sale_date, closed_register_info in closed_register_summary.items():
        if sale_date not in merged:
            merged[sale_date] = {
                "sale_date": sale_date,
                "cash_sales": 0.0,
                "credit_sales": 0.0,
                "cash_taken": 0.0,
                "cash_deposit": 0.0,
                "notes": "",
                "updated_at": "",
                "deduction_count": 0,
                "closed_register_withdrawn": closed_register_info.get("closed_register_withdrawn", 0.0),
                "closed_register_withdrawal_count": closed_register_info.get(
                    "closed_register_withdrawal_count", 0
                ),
            }
        else:
            merged[sale_date]["closed_register_withdrawn"] = closed_register_info.get(
                "closed_register_withdrawn", 0.0
            )
            merged[sale_date]["closed_register_withdrawal_count"] = closed_register_info.get(
                "closed_register_withdrawal_count", 0
            )

    return sorted(
        merged.values(),
        key=lambda row: str(row.get("sale_date") or ""),
        reverse=True,
    )


def format_daily_sales_audit_summary(row: dict) -> str:
    action = str(row.get("action") or "")
    entity_type = str(row.get("entity_type") or "")
    snapshot = row.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        return ""
    if entity_type == "cash_deduction":
        deduction = snapshot.get("deduction") or {}
        amount = round(float(deduction.get("amount") or 0.0), 2)
        note = str(deduction.get("note") or "").strip()
        source_day = str(
            snapshot.get("source_day") or deduction.get("source_sale_date") or deduction.get("sale_date") or ""
        )
        withdrawal_day = str(snapshot.get("withdrawal_day") or deduction.get("withdrawal_date") or "")
        if action == "cash_deduction_added":
            context = ""
            if source_day and withdrawal_day and source_day != withdrawal_day:
                context = f" from {source_day} (entered {withdrawal_day})"
            return f"Added ${amount:,.2f}{context}" + (f" ({note})" if note else "")
        if action == "cash_deduction_deleted":
            context = ""
            if source_day and withdrawal_day and source_day != withdrawal_day:
                context = f" from {source_day} (entered {withdrawal_day})"
            return f"Deleted ${amount:,.2f}{context}" + (f" ({note})" if note else "")
    if entity_type == "ledger_day":
        current = snapshot.get("current") or {}
        cash_sales = round(float(current.get("cash_sales") or 0.0), 2)
        credit_sales = round(float(current.get("credit_sales") or 0.0), 2)
        cash_deposit = round(float(current.get("cash_deposit") or 0.0), 2)
        cash_taken = round(float(current.get("cash_taken") or 0.0), 2)
        if action == "ledger_deleted":
            deleted = snapshot.get("deleted") or current
            return (
                f"Deleted day with cash ${float(deleted.get('cash_sales') or 0.0):,.2f}, "
                f"credit ${float(deleted.get('credit_sales') or 0.0):,.2f}"
            )
        return (
            f"Cash ${cash_sales:,.2f}, credit ${credit_sales:,.2f}, "
            f"taken ${cash_taken:,.2f}, deposit ${cash_deposit:,.2f}"
        )
    return ""


def build_monthly_ledger_print_html(
        month_name: str,
        rows: list[dict],
        totals: dict[str, float],
) -> str:
        def esc(text) -> str:
                txt = str(text or "")
                return (
                        txt.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace('"', "&quot;")
                        .replace("'", "&#39;")
                )

        def money(value) -> str:
                return f"${float(value or 0.0):,.2f}"

        table_rows = []
        for row in sorted(rows or [], key=lambda x: str(x.get("Date") or ""), reverse=True):
                table_rows.append(
                        "<tr>"
                        f"<td>{esc(row.get('Date'))}</td>"
                        f"<td>{money(row.get('Cash Sales'))}</td>"
                        f"<td>{money(row.get('Credit Sales'))}</td>"
                        f"<td>{money(row.get('Total Sales'))}</td>"
                        f"<td>{money(row.get('Cash Taken'))}</td>"
                        f"<td>{money(row.get('Cash Deposited'))}</td>"
                        f"<td>{money(row.get('Deposit Variance'))}</td>"
                        f"<td>{esc(row.get('Notes'))}</td>"
                        "</tr>"
                )

        rows_html = "".join(table_rows)
        generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"""
<!doctype html>
<html>
<head>
    <meta charset=\"utf-8\" />
    <title>Daily Sales Ledger - {esc(month_name)}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 24px; color: #111; }}
        h1 {{ margin: 0 0 8px 0; font-size: 24px; }}
        .meta {{ margin-bottom: 14px; color: #444; font-size: 13px; }}
        .summary {{ display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 8px; margin: 14px 0 16px 0; }}
        .summary div {{ border: 1px solid #ddd; padding: 8px; border-radius: 6px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 12px; }}
        th {{ background: #f3f3f3; }}
        @media print {{
            body {{ margin: 8mm; }}
            .no-print {{ display: none; }}
        }}
    </style>
</head>
<body>
    <div class=\"no-print\" style=\"margin-bottom: 10px;\">
        <button onclick=\"window.print()\">Print</button>
    </div>
    <h1>Daily Sales Ledger - {esc(month_name)}</h1>
    <div class=\"meta\">Generated: {esc(generated_at)}</div>
    <div class=\"summary\">
        <div><strong>Cash Sales</strong><br/>{money(totals.get('cash_sales', 0.0))}</div>
        <div><strong>Credit Sales</strong><br/>{money(totals.get('credit_sales', 0.0))}</div>
        <div><strong>Total Sales</strong><br/>{money(totals.get('total_sales', 0.0))}</div>
        <div><strong>Cash Taken</strong><br/>{money(totals.get('cash_taken', 0.0))}</div>
        <div><strong>Cash Deposited</strong><br/>{money(totals.get('cash_deposited', 0.0))}</div>
        <div><strong>Deposit Variance</strong><br/>{money(totals.get('deposit_variance', 0.0))}</div>
    </div>
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Cash Sales</th>
                <th>Credit Sales</th>
                <th>Total Sales</th>
                <th>Cash Taken</th>
                <th>Cash Deposited</th>
                <th>Deposit Variance</th>
                <th>Notes</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
</body>
</html>
"""


def _norm(value) -> str:
    return str(value or "").strip()


def _pick(record: dict, *keys: str) -> str:
    for k in keys:
        if k in record and _norm(record[k]):
            return _norm(record[k])
    return ""


def import_members_csv(pg: SyncPostgrestClient, csv_text: str) -> tuple[int, int]:
    reader = csv.DictReader(io.StringIO(csv_text))
    inserted = 0
    skipped = 0
    today = datetime.date.today().strftime("%Y-%m-%d")

    for row in reader:
        first_name = _pick(row, "first_name", "First Name", "first")
        last_name = _pick(row, "last_name", "Last Name", "last")

        if not first_name and not last_name:
            full_name = _pick(row, "name", "Member Name", "full_name")
            if "," in full_name:
                left, right = [x.strip() for x in full_name.split(",", 1)]
                last_name, first_name = left, right
            elif full_name:
                parts = full_name.split()
                first_name = parts[0]
                last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

        if not first_name or not last_name:
            skipped += 1
            continue

        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "email": _pick(row, "email", "Email"),
            "phone": _pick(row, "phone", "Phone", "phone_number", "Phone Number"),
            "tier": _pick(row, "tier", "Tier") or "Monthly",
            "status": _pick(row, "status", "Status") or "Active",
            "locker": _pick(row, "locker", "Locker") or "—",
            "join_date": _pick(row, "join_date", "Join Date", "join") or today,
            "next_billing_date": _pick(
                row,
                "next_billing_date",
                "Next Bill Due",
                "next_bill",
                "Next Bill",
            )
            or today,
            "last_reminder": _pick(row, "last_reminder") or "None",
        }

        inserted_id = None
        try:
            resp = pg.from_("members").insert(payload).execute()
            data = getattr(resp, "data", None) or []
            if data and isinstance(data[0], dict):
                inserted_id = data[0].get("id")
        except Exception:
            payload.pop("phone", None)
            resp = pg.from_("members").insert(payload).execute()
            data = getattr(resp, "data", None) or []
            if data and isinstance(data[0], dict):
                inserted_id = data[0].get("id")
        if inserted_id is not None:
            _enroll_member_in_loyalty(
                pg,
                str(inserted_id),
                payload["first_name"],
                payload["last_name"],
                payload.get("phone", ""),
                payload.get("email", ""),
            )
        inserted += 1

    return inserted, skipped


# ── Settings helpers ───────────────────────────────────────────────────────────

def get_setting(pg: SyncPostgrestClient, key: str) -> str:
    try:
        rows = pg.from_("settings").select("value").eq("key", key).execute().data
    except Exception as exc:
        if "getaddrinfo failed" in str(exc).lower():
            st.error(
                "Failed to connect to Supabase (DNS resolution error). "
                "Verify SUPABASE_URL in .streamlit/secrets.toml and your network DNS settings."
            )
            st.stop()
        raise
    return rows[0]["value"] if rows else ""


def save_setting(pg: SyncPostgrestClient, key: str, value: str):
    existing = pg.from_("settings").select("key").eq("key", key).execute().data
    if existing:
        pg.from_("settings").update({"value": value}).eq("key", key).execute()
    else:
        pg.from_("settings").insert({"key": key, "value": value}).execute()


def clear_setting(pg: SyncPostgrestClient, key: str):
    pg.from_("settings").delete().eq("key", key).execute()


MEMBER_EDIT_UNDO_KEY = "member_edit_undo_v1"
MAX_MEMBER_EDIT_UNDO = 50


def load_member_edit_undo(pg: SyncPostgrestClient) -> list[dict]:
    raw = get_setting(pg, MEMBER_EDIT_UNDO_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("id") is not None:
            out.append(item)
    return out[-MAX_MEMBER_EDIT_UNDO:]


def save_member_edit_undo(pg: SyncPostgrestClient, undo_stack: list[dict]):
    save_setting(
        pg,
        MEMBER_EDIT_UNDO_KEY,
        json.dumps(undo_stack[-MAX_MEMBER_EDIT_UNDO:]),
    )


def clear_member_edit_widget_state(member_id):
    for prefix in (
        "e_fn_",
        "e_ln_",
        "e_em_",
        "e_ph_",
        "e_lk_",
        "e_st_",
        "save_member_",
        "undo_member_",
        "restore_pick_",
        "restore_snap_",
    ):
        st.session_state.pop(f"{prefix}{member_id}", None)


POS_INVENTORY_KEY = "pos_inventory_v1"
POS_PROMOTIONS_KEY = "pos_promotions_v1"
POS_SALES_KEY = "pos_sales_v1"
POS_LAST_SYNC_KEY = "pos_last_sync_v1"
POS_LAST_SYNC_ERROR_KEY = "pos_last_sync_error_v1"
POS_SCAN_CHANNEL_KEY = "pos_scan_channel"
POS_CUSTOMER_GROUPS_KEY = "pos_customer_groups_v1"
POS_LOYALTY_SETTINGS_KEY = "pos_loyalty_settings_v1"
POS_LOYALTY_POINTS_KEY = "pos_loyalty_points_v1"
POS_LOYALTY_CUSTOMERS_KEY = "pos_loyalty_customers_v1"
NAV_SHOW_POS_KEY = "nav_show_pos_v1"
NAV_SHOW_SCANNER_KEY = "nav_show_scanner_v1"
MEMBER_MARGIN_SECTION_KEY = "members_show_margin_section_v1"
DRINK_CATALOG_KEY = "drink_catalog_v1"

CIGARPOS_BASE_URL_KEY = "cigarpos_base_url"
CIGARPOS_USERNAME_KEY = "cigarpos_username"
CIGARPOS_PASSWORD_KEY = "cigarpos_password_enc"
CIGARPOS_AUTO_SYNC_KEY = "cigarpos_auto_sync_enabled"
CIGARPOS_AUTO_SYNC_MIN_KEY = "cigarpos_auto_sync_min"
CIGARPOS_SALES_LAST_SYNC_KEY = "cigarpos_sales_last_sync_v1"
EMAIL_REMINDERS_AUTO_ENABLED_KEY = "email_reminders_auto_enabled_v1"
EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY = "email_reminders_auto_interval_min_v1"
EMAIL_REMINDERS_AUTO_LAST_RUN_KEY = "email_reminders_auto_last_run_v1"
EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY = "email_reminders_auto_last_result_v1"
SCHEDULE_MONTHLY_REMINDERS_KEY = "schedule_monthly_reminders_v1"
SCHEDULE_STORE_EVENTS_KEY = "schedule_store_events_v1"
SCHEDULE_EMAIL_TO_KEY = "schedule_email_to_v1"
SCHEDULE_EMAIL_AUTO_ENABLED_KEY = "schedule_email_auto_enabled_v1"
SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY = "schedule_email_auto_interval_min_v1"
SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY = "schedule_email_auto_last_run_v1"
SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY = "schedule_email_auto_last_result_v1"
ORDERING_REPS_KEY = "ordering_sales_reps_v1"
ORDERING_COMPANIES_KEY = "ordering_companies_v1"
SALES_LEDGER_WEEKEND_START_KEY = "sales_ledger_weekend_start_day_v1"
SALES_LEDGER_WEEKEND_END_KEY = "sales_ledger_weekend_end_day_v1"


def _load_json_list_setting(pg: SyncPostgrestClient, key: str) -> list[dict]:
    raw = get_setting(pg, key)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _save_json_list_setting(pg: SyncPostgrestClient, key: str, rows: list[dict]):
    save_setting(pg, key, json.dumps(rows))


def _setting_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _next_monthly_due_date(day_of_month: int, today: datetime.date | None = None) -> datetime.date:
    today = today or datetime.date.today()
    year = today.year
    month = today.month

    current_month_day = min(int(day_of_month), calendar.monthrange(year, month)[1])
    due_date = datetime.date(year, month, current_month_day)
    if due_date >= today:
        return due_date

    if month == 12:
        month = 1
        year += 1
    else:
        month += 1
    next_month_day = min(int(day_of_month), calendar.monthrange(year, month)[1])
    return datetime.date(year, month, next_month_day)


def _format_time_12h(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        hour_txt, minute_txt = raw.split(":", 1)
        hour = int(hour_txt)
        minute = int(minute_txt)
    except Exception:
        return raw
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return raw
    period = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{hour_12}:{minute:02d} {period}"


def _time_24h_to_parts(
    value: str,
    default_hour: int = 6,
    default_minute: int = 0,
    default_period: str = "PM",
) -> tuple[int, int, str]:
    raw = str(value or "").strip()
    if raw:
        try:
            hour_txt, minute_txt = raw.split(":", 1)
            hour = int(hour_txt)
            minute = int(minute_txt)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                period = "AM" if hour < 12 else "PM"
                hour_12 = hour % 12
                if hour_12 == 0:
                    hour_12 = 12
                return hour_12, minute, period
        except Exception:
            pass
    if default_period not in {"AM", "PM"}:
        default_period = "PM"
    return max(1, min(12, int(default_hour))), max(0, min(59, int(default_minute))), default_period


def _time_parts_to_24h(hour_12: int, minute: int, period: str) -> str:
    hour = max(1, min(12, int(hour_12)))
    minute = max(0, min(59, int(minute)))
    marker = str(period or "PM").strip().upper()
    if marker not in {"AM", "PM"}:
        marker = "PM"
    if marker == "AM":
        hour_24 = 0 if hour == 12 else hour
    else:
        hour_24 = 12 if hour == 12 else hour + 12
    return f"{hour_24:02d}:{minute:02d}"


def _format_datetime_12h(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("T", " ").replace("Z", "").strip()
    if "." in normalized:
        normalized = normalized.split(".", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(normalized, fmt)
            return dt.strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            continue
    return raw


def load_monthly_schedule_reminders(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, SCHEDULE_MONTHLY_REMINDERS_KEY)
    out = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        try:
            day_of_month = int(row.get("day_of_month") or 1)
        except Exception:
            day_of_month = 1
        day_of_month = max(1, min(31, day_of_month))
        notes = str(row.get("notes") or "").strip()
        reminder_id = str(row.get("id") or hashlib.sha1(f"{title.lower()}|{day_of_month}".encode("utf-8")).hexdigest()[:12])
        out.append(
            {
                "id": reminder_id,
                "title": title,
                "day_of_month": day_of_month,
                "notes": notes,
                "enabled": _setting_bool(row.get("enabled"), True),
            }
        )
    return sorted(out, key=lambda item: (int(item.get("day_of_month") or 1), str(item.get("title") or "").lower()))


def save_monthly_schedule_reminders(pg: SyncPostgrestClient, reminders: list[dict]):
    clean = []
    for row in reminders or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        try:
            day_of_month = int(row.get("day_of_month") or 1)
        except Exception:
            day_of_month = 1
        day_of_month = max(1, min(31, day_of_month))
        notes = str(row.get("notes") or "").strip()
        reminder_id = str(row.get("id") or hashlib.sha1(f"{title.lower()}|{day_of_month}".encode("utf-8")).hexdigest()[:12])
        clean.append(
            {
                "id": reminder_id,
                "title": title,
                "day_of_month": day_of_month,
                "notes": notes,
                "enabled": _setting_bool(row.get("enabled"), True),
            }
        )
    _save_json_list_setting(pg, SCHEDULE_MONTHLY_REMINDERS_KEY, clean)


def load_store_events_schedule(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, SCHEDULE_STORE_EVENTS_KEY)
    out = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        event_date = str(row.get("event_date") or "").strip()
        if not title or not event_date:
            continue
        try:
            datetime.datetime.strptime(event_date, "%Y-%m-%d")
        except Exception:
            continue
        all_day = _setting_bool(row.get("all_day"), True)
        start_time = str(row.get("start_time") or "").strip()
        if start_time:
            try:
                start_time = datetime.datetime.strptime(start_time, "%H:%M").strftime("%H:%M")
            except Exception:
                start_time = ""
        location = str(row.get("location") or "").strip()
        notes = str(row.get("notes") or "").strip()
        event_id = str(
            row.get("id")
            or hashlib.sha1(f"{title.lower()}|{event_date}|{start_time}|{location.lower()}".encode("utf-8")).hexdigest()[:12]
        )
        out.append(
            {
                "id": event_id,
                "title": title,
                "event_date": event_date,
                "all_day": all_day,
                "start_time": "" if all_day else start_time,
                "location": location,
                "notes": notes,
            }
        )
    return sorted(
        out,
        key=lambda item: (
            str(item.get("event_date") or ""),
            str(item.get("start_time") or "99:99"),
            str(item.get("title") or "").lower(),
        ),
    )


def save_store_events_schedule(pg: SyncPostgrestClient, events: list[dict]):
    clean = []
    for row in events or []:
        title = str(row.get("title") or "").strip()
        event_date = str(row.get("event_date") or "").strip()
        if not title or not event_date:
            continue
        try:
            datetime.datetime.strptime(event_date, "%Y-%m-%d")
        except Exception:
            continue
        all_day = _setting_bool(row.get("all_day"), True)
        start_time = str(row.get("start_time") or "").strip()
        if start_time:
            try:
                start_time = datetime.datetime.strptime(start_time, "%H:%M").strftime("%H:%M")
            except Exception:
                start_time = ""
        location = str(row.get("location") or "").strip()
        notes = str(row.get("notes") or "").strip()
        event_id = str(
            row.get("id")
            or hashlib.sha1(f"{title.lower()}|{event_date}|{start_time}|{location.lower()}".encode("utf-8")).hexdigest()[:12]
        )
        clean.append(
            {
                "id": event_id,
                "title": title,
                "event_date": event_date,
                "all_day": all_day,
                "start_time": "" if all_day else start_time,
                "location": location,
                "notes": notes,
            }
        )
    _save_json_list_setting(pg, SCHEDULE_STORE_EVENTS_KEY, clean)


def load_ordering_sales_reps(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, ORDERING_REPS_KEY)
    out = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        email = parseaddr(str(row.get("email") or "").strip())[1].strip()
        if not name:
            continue
        rep_id = str(row.get("id") or hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:12])
        out.append(
            {
                "id": rep_id,
                "name": name,
                "email": email,
                "active": _setting_bool(row.get("active"), True),
            }
        )
    return sorted(out, key=lambda item: str(item.get("name") or "").lower())


def save_ordering_sales_reps(pg: SyncPostgrestClient, reps: list[dict]):
    clean = []
    for row in reps or []:
        name = str(row.get("name") or "").strip()
        email = parseaddr(str(row.get("email") or "").strip())[1].strip()
        if not name:
            continue
        rep_id = str(row.get("id") or hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:12])
        clean.append(
            {
                "id": rep_id,
                "name": name,
                "email": email,
                "active": _setting_bool(row.get("active"), True),
            }
        )
    _save_json_list_setting(pg, ORDERING_REPS_KEY, clean)


def upsert_company_reps_into_sales_reps(companies: list[dict], existing_reps: list[dict]) -> list[dict]:
    updated = [dict(rep) for rep in (existing_reps or []) if isinstance(rep, dict)]

    for company in companies or []:
        rep_name = str((company or {}).get("rep_name") or "").strip()
        rep_email = parseaddr(str((company or {}).get("rep_email") or "").strip())[1].strip()
        if not rep_name or not rep_email:
            continue

        match_idx = -1
        target_name = rep_name.lower()
        target_email = rep_email.lower()
        for idx, rep in enumerate(updated):
            existing_name = str((rep or {}).get("name") or "").strip().lower()
            existing_email = parseaddr(str((rep or {}).get("email") or "").strip())[1].strip().lower()
            if (existing_email and existing_email == target_email) or (existing_name and existing_name == target_name):
                match_idx = idx
                break

        if match_idx >= 0:
            row = dict(updated[match_idx])
            row["name"] = rep_name
            row["email"] = rep_email
            row["active"] = True
            row["id"] = str(row.get("id") or hashlib.sha1(rep_name.lower().encode("utf-8")).hexdigest()[:12])
            updated[match_idx] = row
        else:
            updated.append(
                {
                    "id": hashlib.sha1(rep_name.lower().encode("utf-8")).hexdigest()[:12],
                    "name": rep_name,
                    "email": rep_email,
                    "active": True,
                }
            )

    return updated


def _clean_ordering_item_row(row: dict) -> dict:
    import re

    raw_name = str((row or {}).get("name") or "").strip()
    source_price_type = str((row or {}).get("source_price_type") or "box").strip() or "box"
    box_price = round(float((row or {}).get("box_price") or (row or {}).get("unit_cost") or 0.0), 2)
    stick_price = round(float((row or {}).get("stick_price") or 0.0), 2)
    cigars_per_box = max(0, int((row or {}).get("cigars_per_box") or 0))

    # Recover wholesale when old/dirty rows include both wholesale and stick prices in the name.
    embedded = []
    for hit in re.findall(r"\$\s*([\d,]+(?:\.\d{1,2})?)", raw_name):
        try:
            embedded.append(float(hit.replace(",", "")))
        except Exception:
            pass
    if len(embedded) >= 2:
        inferred_box = round(max(embedded), 2)
        inferred_stick = round(min(embedded), 2)
        if inferred_box > box_price:
            box_price = inferred_box
            source_price_type = "embedded_wholesale"
        if stick_price <= 0:
            stick_price = inferred_stick

    # If stored box price looks like per-stick, convert to box-level using count.
    if cigars_per_box > 1 and box_price > 0 and box_price <= 60:
        computed = round(box_price * cigars_per_box, 2)
        if computed > box_price:
            box_price = computed
            source_price_type = "computed_from_stick"

    # If we have both prices and box is not larger than stick, treat stick as per-cigar and compute box.
    if cigars_per_box > 1 and box_price > 0 and stick_price > 0 and box_price <= stick_price * 1.05:
        box_price = round(stick_price * cigars_per_box, 2)
        source_price_type = "computed_from_stick"

    # Clean visual noise from product names.
    clean_name = raw_name
    clean_name = re.sub(r"\$\s*[\d,]+(?:\.\d{1,2})?", "", clean_name)
    clean_name = re.sub(r"\s+", " ", clean_name).strip(" -,:;")

    return {
        "sku": str((row or {}).get("sku") or "").strip(),
        "name": clean_name,
        "box_price": box_price,
        "unit_cost": box_price,
        "boxes": max(0, int((row or {}).get("boxes") or (row or {}).get("quantity") or 0)),
        "quantity": max(0, int((row or {}).get("boxes") or (row or {}).get("quantity") or 0)),
        "stick_price": stick_price,
        "cigars_per_box": cigars_per_box,
        "source_price_type": source_price_type,
        "rep_name": str((row or {}).get("rep_name") or "").strip(),
        "rep_email": parseaddr(str((row or {}).get("rep_email") or "").strip())[1].strip(),
        "notes": str((row or {}).get("notes") or "").strip(),
    }


def load_ordering_companies(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, ORDERING_COMPANIES_KEY)
    out = []
    for row in rows:
        company = str(row.get("company") or row.get("name") or "").strip()
        if not company:
            continue
        company_id = str(row.get("id") or hashlib.sha1(company.lower().encode("utf-8")).hexdigest()[:12])
        rep_name = str(row.get("rep_name") or "").strip()
        rep_email = parseaddr(str(row.get("rep_email") or row.get("email") or "").strip())[1].strip()
        source_file = str(row.get("source_file") or "").strip()
        order_note = str(row.get("order_note") or "").strip()
        raw_rows = row.get("order_rows") if isinstance(row.get("order_rows"), list) else []
        clean_rows = []
        for item in raw_rows:
            if isinstance(item, dict):
                clean_item = _clean_ordering_item_row(item)
                if clean_item.get("name"):
                    clean_rows.append(clean_item)
        out.append(
            {
                "id": company_id,
                "company": company,
                "rep_name": rep_name,
                "rep_email": rep_email,
                "active": _setting_bool(row.get("active"), True),
                "source_file": source_file,
                "order_note": order_note,
                "order_rows": clean_rows,
            }
        )
    return sorted(out, key=lambda item: str(item.get("company") or "").lower())


def save_ordering_companies(pg: SyncPostgrestClient, companies: list[dict]):
    clean = []
    for row in companies or []:
        company = str((row or {}).get("company") or (row or {}).get("name") or "").strip()
        if not company:
            continue
        company_id = str((row or {}).get("id") or hashlib.sha1(company.lower().encode("utf-8")).hexdigest()[:12])
        rep_name = str((row or {}).get("rep_name") or "").strip()
        rep_email = parseaddr(str((row or {}).get("rep_email") or (row or {}).get("email") or "").strip())[1].strip()
        source_file = str((row or {}).get("source_file") or "").strip()
        order_note = str((row or {}).get("order_note") or "").strip()
        clean_rows = []
        for item in (row or {}).get("order_rows") or []:
            if isinstance(item, dict):
                cleaned = _clean_ordering_item_row(item)
                if cleaned.get("name"):
                    clean_rows.append(cleaned)
        clean.append(
            {
                "id": company_id,
                "company": company,
                "rep_name": rep_name,
                "rep_email": rep_email,
                "active": _setting_bool((row or {}).get("active"), True),
                "source_file": source_file,
                "order_note": order_note,
                "order_rows": clean_rows,
            }
        )
    _save_json_list_setting(pg, ORDERING_COMPANIES_KEY, clean)


def save_ordering_company_draft(
    pg: SyncPostgrestClient,
    company_id: str,
    order_rows: list[dict],
    source_file: str,
    order_note: str,
):
    target_id = str(company_id or "").strip()
    if not target_id:
        return
    companies = load_ordering_companies(pg)
    updated = []
    for company in companies:
        row = dict(company)
        if str(row.get("id") or "").strip() == target_id:
            row["order_rows"] = [
                _clean_ordering_item_row(item)
                for item in (order_rows or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            row["source_file"] = str(source_file or "").strip()
            row["order_note"] = str(order_note or "").strip()
        updated.append(row)
    save_ordering_companies(pg, updated)


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


def page_ordering(pg: SyncPostgrestClient):
    st.header("Ordering")
    st.caption("Select a company, load or build its order form, and send to the linked sales rep email.")

    reps = load_ordering_sales_reps(pg)
    companies = load_ordering_companies(pg)

    with st.expander("Sales Reps", expanded=False):
        st.caption("Maintain rep emails here so orders always route to the right person. Click ➕ at the bottom of the table to add a row.")
        editor_rows = [
            {
                "Name": str(rep.get("name") or ""),
                "Email": str(rep.get("email") or ""),
                "Active": bool(rep.get("active", True)),
            }
            for rep in reps
        ]
        edited_rep_rows = st.data_editor(
            editor_rows,
            num_rows="dynamic",
            width="stretch",
            key="ordering_reps_editor",
        )
        if st.button("Save Rep Directory", key="ordering_save_reps"):
            updated = []
            for row in edited_rep_rows or []:
                name = str((row or {}).get("Name") or "").strip()
                email = parseaddr(str((row or {}).get("Email") or "").strip())[1].strip()
                active = bool((row or {}).get("Active", True))
                if not name:
                    continue
                updated.append(
                    {
                        "id": hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:12],
                        "name": name,
                        "email": email,
                        "active": active,
                    }
                )
            save_ordering_sales_reps(pg, updated)
            st.success("Sales rep directory saved.")
            st.rerun()

    with st.expander("Companies", expanded=True):
        st.caption("Each company stores a default sales rep email and a saved order form draft. Click ➕ at the bottom of the table to add a row.")
        auto_sync_company_reps = st.checkbox(
            "Auto-sync company reps into Sales Reps when saving",
            value=bool(st.session_state.get("ordering_auto_sync_company_reps", True)),
            key="ordering_auto_sync_company_reps",
            help="When enabled, company rep name/email entries are upserted into the Sales Reps directory.",
        )
        company_editor_rows = [
            {
                "Company": str(company.get("company") or ""),
                "Sales Rep": str(company.get("rep_name") or ""),
                "Rep Email": str(company.get("rep_email") or ""),
                "Active": bool(company.get("active", True)),
            }
            for company in companies
        ]
        edited_company_rows = st.data_editor(
            company_editor_rows,
            num_rows="dynamic",
            width="stretch",
            key="ordering_company_editor",
        )
        if st.button("Save Company Directory", key="ordering_save_companies"):
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
                        "rep_name": str((row or {}).get("Sales Rep") or "").strip(),
                        "rep_email": parseaddr(str((row or {}).get("Rep Email") or "").strip())[1].strip(),
                        "active": bool((row or {}).get("Active", True)),
                        "source_file": str(existing.get("source_file") or ""),
                        "order_note": str(existing.get("order_note") or ""),
                        "order_rows": list(existing.get("order_rows") or []),
                    }
                )
            save_ordering_companies(pg, updated_companies)
            if auto_sync_company_reps:
                current_reps = load_ordering_sales_reps(pg)
                merged_reps = upsert_company_reps_into_sales_reps(updated_companies, current_reps)
                save_ordering_sales_reps(pg, merged_reps)
            st.success("Company directory saved.")
            st.rerun()

    active_companies = [company for company in load_ordering_companies(pg) if company.get("active")]

    _NO_COMPANY = "— None (manual entry) —"
    company_options = {str(company.get("company") or "Unknown Company"): company for company in active_companies}
    company_select_options = [_NO_COMPANY] + list(company_options.keys())
    company_choice = st.selectbox("Company (optional)", company_select_options, key="ordering_company_pick")
    selected_company = company_options.get(company_choice) or {}
    selected_company_id = str(selected_company.get("id") or "").strip()

    if selected_company_id:
        active_reps_for_sync = [rep for rep in load_ordering_sales_reps(pg) if rep.get("active")]
        if active_reps_for_sync:
            rep_pick_options = {
                f"{str(rep.get('name') or '').strip()} ({str(rep.get('email') or '').strip() or 'no email'})": rep
                for rep in active_reps_for_sync
            }
            sync_c1, sync_c2 = st.columns(2)
            rep_pick = sync_c1.selectbox(
                "Pick rep to copy into this company",
                list(rep_pick_options.keys()),
                key=f"ordering_company_rep_pick_{selected_company_id}",
            )
            if sync_c2.button("Copy Sales Rep → Company", key="ordering_copy_rep_to_company"):
                selected_rep = rep_pick_options[rep_pick]
                rep_name_val = str(selected_rep.get("name") or "").strip()
                rep_email_val = parseaddr(str(selected_rep.get("email") or "").strip())[1].strip()
                if not rep_name_val or not rep_email_val:
                    st.warning("Selected sales rep is missing a valid name or email.")
                else:
                    companies_now = load_ordering_companies(pg)
                    updated_companies = []
                    for _c in companies_now:
                        row = dict(_c)
                        if str(row.get("id") or "").strip() == selected_company_id:
                            row["rep_name"] = rep_name_val
                            row["rep_email"] = rep_email_val
                        updated_companies.append(row)
                    save_ordering_companies(pg, updated_companies)
                    st.session_state[f"ordering_rep_name_{selected_company_id}"] = rep_name_val
                    st.session_state[f"ordering_rep_email_{selected_company_id}"] = rep_email_val
                    st.success(f"Copied {rep_name_val} into {selected_company.get('company', '')}.")
                    st.rerun()

        cp_col, _ = st.columns([1, 3])
        if cp_col.button("Copy Company Rep → Sales Reps", key="ordering_copy_company_rep"):
            rep_name_val = str(selected_company.get("rep_name") or "").strip()
            rep_email_val = parseaddr(str(selected_company.get("rep_email") or "").strip())[1].strip()
            if not rep_name_val:
                st.warning("Selected company does not have a rep name yet.")
            elif not rep_email_val:
                st.warning("Selected company does not have a valid rep email yet.")
            else:
                current_reps = load_ordering_sales_reps(pg)
                rep_updated = False
                updated_reps = []
                for rep in current_reps:
                    row = dict(rep)
                    existing_name = str(row.get("name") or "").strip().lower()
                    existing_email = parseaddr(str(row.get("email") or "").strip())[1].strip().lower()
                    if (existing_email and existing_email == rep_email_val.lower()) or (existing_name and existing_name == rep_name_val.lower()):
                        row["name"] = rep_name_val
                        row["email"] = rep_email_val
                        row["active"] = True
                        rep_updated = True
                    updated_reps.append(row)
                if not rep_updated:
                    updated_reps.append(
                        {
                            "id": hashlib.sha1(rep_name_val.lower().encode("utf-8")).hexdigest()[:12],
                            "name": rep_name_val,
                            "email": rep_email_val,
                            "active": True,
                        }
                    )
                save_ordering_sales_reps(pg, updated_reps)
                st.success(f"Copied {rep_name_val} to Sales Reps.")
                st.rerun()

        previous_company_id = str(st.session_state.get("ordering_active_company_id") or "").strip()
        if previous_company_id != selected_company_id:
            st.session_state["ordering_active_company_id"] = selected_company_id
            st.session_state["ordering_rows"] = list(selected_company.get("order_rows") or [])
            st.session_state["ordering_source_file"] = str(selected_company.get("source_file") or "")
            st.session_state["ordering_note"] = str(selected_company.get("order_note") or "")
            st.rerun()
    else:
        st.session_state.pop("ordering_active_company_id", None)

    st.divider()
    st.subheader("Price Sheet Import")
    upload = st.file_uploader(
        "Upload vendor price sheet",
        type=["csv", "xlsx", "xls", "pdf"],
        key="ordering_price_sheet_upload",
        help="Supported: CSV, Excel, PDF",
    )

    if st.button("Parse Price Sheet", key="ordering_parse_sheet"):
        if upload is None:
            st.warning("Upload a file first.")
        else:
            rows, sheet_meta, err = parse_price_sheet_upload_with_meta(upload.name, upload.getvalue())
            if err:
                st.error(err)
            elif not rows:
                st.warning("No orderable rows found in the uploaded file.")
            else:
                st.session_state["ordering_rows"] = rows
                st.session_state["ordering_source_file"] = upload.name

                # Derive company name: prefer sheet column, fall back to filename
                detected_company = str(sheet_meta.get("company") or "").strip()
                if not detected_company:
                    detected_company = _company_name_from_filename(upload.name)

                # Prefer sheet rep info; fall back to whatever is currently on selected company
                detected_rep_name = str(sheet_meta.get("rep_name") or selected_company.get("rep_name") or "").strip()
                detected_rep_email = parseaddr(
                    str(sheet_meta.get("rep_email") or selected_company.get("rep_email") or "").strip()
                )[1].strip()

                if detected_company:
                    saved_company = upsert_ordering_company_from_sheet(
                        pg,
                        detected_company,
                        detected_rep_name,
                        detected_rep_email,
                        upload.name,
                        rows,
                    )
                    new_id = str(saved_company.get("id") or "").strip()
                    st.session_state["ordering_active_company_id"] = new_id
                    st.success(
                        f"Loaded {len(rows)} item(s) and saved to company **{detected_company}**."
                    )
                elif selected_company_id:
                    save_ordering_company_draft(
                        pg, selected_company_id, rows, upload.name,
                        str(st.session_state.get("ordering_note") or ""),
                    )
                    st.success(f"Loaded {len(rows)} orderable item(s) from {upload.name}.")
                else:
                    st.success(f"Loaded {len(rows)} orderable item(s) from {upload.name}. (No company linked — select one above to save the form.)")
                st.rerun()

    order_rows = st.session_state.get("ordering_rows", [])
    source_file = str(st.session_state.get("ordering_source_file") or "")
    if not order_rows:
        st.info("Upload and parse a price sheet to start an order.")
        return

    st.caption(f"Source file: {source_file or 'unknown'}")

    editable_rows = []
    for row in order_rows:
        boxes = int(row.get("boxes") or row.get("quantity") or 0)
        box_price = float(row.get("box_price") or row.get("unit_cost") or 0.0)
        editable_rows.append(
            {
                "SKU": str(row.get("sku") or ""),
                "Product": str(row.get("name") or ""),
                "Box Price": round(box_price, 2),
                "Boxes": boxes,
                "Line Total": round(box_price * boxes, 2),
                "Cigars/Box": int(row.get("cigars_per_box") or 0),
                "Price Source": str(row.get("source_price_type") or "box"),
                "Rep (from sheet)": str(row.get("rep_name") or ""),
                "Rep Email (from sheet)": str(row.get("rep_email") or ""),
            }
        )

    edited_order_rows = st.data_editor(
        editable_rows,
        width="stretch",
        key="ordering_items_editor",
        hide_index=True,
        disabled=["SKU", "Product", "Box Price", "Line Total", "Cigars/Box", "Price Source", "Rep (from sheet)", "Rep Email (from sheet)"],
        column_config={
            "Boxes": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "Box Price": st.column_config.NumberColumn(format="$%.2f"),
            "Line Total": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

    updated_rows = []
    for idx, row in enumerate(order_rows):
        boxes = int((edited_order_rows[idx] or {}).get("Boxes") or 0) if idx < len(edited_order_rows) else int(row.get("boxes") or row.get("quantity") or 0)
        updated = dict(row)
        updated["boxes"] = max(0, boxes)
        updated["quantity"] = max(0, boxes)
        updated_rows.append(updated)
    st.session_state["ordering_rows"] = updated_rows

    selected_lines = [row for row in updated_rows if int(row.get("boxes") or row.get("quantity") or 0) > 0]
    order_total = round(
        sum(float(row.get("box_price") or row.get("unit_cost") or 0.0) * int(row.get("boxes") or row.get("quantity") or 0) for row in selected_lines),
        2,
    )

    m1, m2 = st.columns(2)
    m1.metric("Items selected", len(selected_lines))
    m2.metric("Estimated order total", f"${order_total:,.2f}")

    st.subheader("Route Order")
    _rep_name_default = str(selected_company.get("rep_name") or "").strip()
    _rep_email_default = parseaddr(str(selected_company.get("rep_email") or "").strip())[1].strip()
    rep_key_suffix = selected_company_id or "manual"
    rep_name = st.text_input("Sales rep name", value=_rep_name_default, key=f"ordering_rep_name_{rep_key_suffix}")
    rep_email_input = st.text_input(
        "Sales rep email",
        value=_rep_email_default,
        key=f"ordering_rep_email_{rep_key_suffix}",
        help="Enter the rep's email, or select a company above to auto-fill.",
    )

    member_smtp = load_smtp_settings(pg)
    ordering_smtp = load_ordering_smtp_settings(pg)
    sender_choice = st.radio(
        "Send using",
        ["Member SMTP (current default)", "Sales Rep SMTP profile"],
        key="ordering_sender_choice",
        horizontal=True,
    )
    smtp = ordering_smtp if sender_choice == "Sales Rep SMTP profile" else member_smtp
    smtp_ready = bool(smtp.get("host")) and bool(smtp.get("port")) and bool(smtp.get("from_addr")) and bool(smtp.get("password"))
    if not smtp_ready:
        if sender_choice == "Sales Rep SMTP profile":
            st.info("Sales Rep SMTP profile is not configured. Set it in Settings -> Sales Rep Email Config (Optional).")
        else:
            st.info("Configure SMTP in Settings before sending orders.")

    order_note = st.text_area("Order note (optional)", key="ordering_note", height=90)
    if selected_company_id and st.button("Save Order Form To Company", key="ordering_save_company_form"):
        try:
            save_ordering_company_draft(pg, selected_company_id, updated_rows, source_file, order_note)
            st.success(f"Saved order form for {selected_company.get('company', '')}.")
        except Exception as exc:
            st.error(f"Failed to save company order form: {exc}")

    if st.button("Send Order", key="ordering_send_order", type="primary"):
        rep_email = parseaddr(str(rep_email_input or "").strip())[1].strip()
        if not smtp_ready:
            st.warning("SMTP is not configured.")
        elif not rep_email:
            st.warning("Sales rep email is missing or invalid.")
        elif not selected_lines:
            st.warning("Set boxes greater than 0 for at least one product.")
        else:
            subject = f"Liberty Smokes Order - {datetime.date.today().strftime('%Y-%m-%d')}"
            body_lines = [
                f"Company: {selected_company.get('company', '')}",
                f"Sales Rep: {rep_name}",
                f"Source Price Sheet: {source_file or 'N/A'}",
                "",
                "Order items:",
            ]
            for row in selected_lines:
                boxes = int(row.get("boxes") or row.get("quantity") or 0)
                box_price = float(row.get("box_price") or row.get("unit_cost") or 0.0)
                line_total = boxes * box_price
                sku = str(row.get("sku") or "").strip()
                name = str(row.get("name") or "").strip()
                label = f"{sku} - {name}" if sku else name
                body_lines.append(f"- {label} | Boxes {boxes} | Box ${box_price:,.2f} | Line ${line_total:,.2f}")
            body_lines.extend(["", f"Estimated total: ${order_total:,.2f}"])
            if order_note.strip():
                body_lines.extend(["", "Notes:", order_note.strip()])

            try:
                send_email(
                    smtp["host"],
                    int(smtp["port"]),
                    smtp.get("username", ""),
                    smtp.get("password", ""),
                    rep_email,
                    subject,
                    "\n".join(body_lines),
                    security=smtp.get("security", "SSL"),
                    from_addr=smtp.get("from_addr", ""),
                )
                save_ordering_company_draft(pg, selected_company_id, updated_rows, source_file, order_note)
                st.success(f"Order sent to {rep_name or selected_company.get('company', '')} ({rep_email}).")
            except Exception as exc:
                st.error(f"Failed to send order: {exc}")

    export_buffer = io.StringIO()
    writer = csv.DictWriter(export_buffer, fieldnames=["sku", "name", "box_price", "boxes", "line_total"])
    writer.writeheader()
    for row in selected_lines:
        boxes = int(row.get("boxes") or row.get("quantity") or 0)
        box_price = float(row.get("box_price") or row.get("unit_cost") or 0.0)
        writer.writerow(
            {
                "sku": str(row.get("sku") or ""),
                "name": str(row.get("name") or ""),
                "box_price": round(box_price, 2),
                "boxes": boxes,
                "line_total": round(boxes * box_price, 2),
            }
        )
    st.download_button(
        "Download Order CSV",
        data=export_buffer.getvalue(),
        file_name=f"order_{datetime.date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        width="stretch",
        disabled=not bool(selected_lines),
    )


def load_drink_catalog(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, DRINK_CATALOG_KEY)
    out = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        category = str(row.get("category") or "").strip().lower()
        if not name or category not in {"alcoholic", "non_alcoholic"}:
            continue
        try:
            cost = round(float(row.get("cost") or 0), 2)
        except Exception:
            cost = 0.0
        out.append(
            {
                "id": str(row.get("id") or hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:12]),
                "name": name,
                "category": category,
                "cost": max(0.0, cost),
            }
        )
    return out


def save_drink_catalog(pg: SyncPostgrestClient, rows: list[dict]):
    safe = []
    for row in rows or []:
        name = str(row.get("name") or "").strip()
        category = str(row.get("category") or "").strip().lower()
        if not name or category not in {"alcoholic", "non_alcoholic"}:
            continue
        try:
            cost = round(float(row.get("cost") or 0), 2)
        except Exception:
            cost = 0.0
        safe.append(
            {
                "id": str(row.get("id") or hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:12]),
                "name": name,
                "category": category,
                "cost": max(0.0, cost),
            }
        )
    _save_json_list_setting(pg, DRINK_CATALOG_KEY, safe)


def _parse_drink_breakdown(value) -> dict:
    if isinstance(value, dict):
        src = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            src = parsed if isinstance(parsed, dict) else {}
        except Exception:
            src = {}
    else:
        src = {}
    out = {}
    for k, v in src.items():
        key = str(k or "").strip()
        if not key:
            continue
        try:
            out[key] = max(0, int(v))
        except Exception:
            out[key] = 0
    return out


def load_pos_inventory(pg: SyncPostgrestClient) -> list[dict]:
    return _load_json_list_setting(pg, POS_INVENTORY_KEY)


def save_pos_inventory(pg: SyncPostgrestClient, rows: list[dict]):
    _save_json_list_setting(pg, POS_INVENTORY_KEY, rows)


def load_pos_promotions(pg: SyncPostgrestClient) -> list[dict]:
    return _load_json_list_setting(pg, POS_PROMOTIONS_KEY)


def save_pos_promotions(pg: SyncPostgrestClient, rows: list[dict]):
    _save_json_list_setting(pg, POS_PROMOTIONS_KEY, rows)


def load_pos_sales(pg: SyncPostgrestClient) -> list[dict]:
    return _load_json_list_setting(pg, POS_SALES_KEY)


def save_pos_sales(pg: SyncPostgrestClient, rows: list[dict]):
    _save_json_list_setting(pg, POS_SALES_KEY, rows)


def resolve_sale_member_id(sale: dict, loyalty_customers: list[dict] | None = None):
    member_id = sale.get("member_id")
    if member_id not in {None, ""}:
        return str(member_id)

    loyalty_contact_id = str(sale.get("loyalty_contact_id") or "").strip()
    if not loyalty_contact_id:
        return None

    for contact in loyalty_customers or []:
        if str(contact.get("id") or "").strip() != loyalty_contact_id:
            continue
        linked_member_id = contact.get("member_id")
        if linked_member_id in {None, ""}:
            return None
        return str(linked_member_id)

    return None


def backfill_pos_sales_member_ids(
    sales: list[dict],
    loyalty_customers: list[dict] | None = None,
) -> tuple[list[dict], int]:
    loyalty_member_by_contact_id = {}
    for contact in loyalty_customers or []:
        contact_id = str(contact.get("id") or "").strip()
        linked_member_id = str(contact.get("member_id") or "").strip()
        if contact_id and linked_member_id:
            loyalty_member_by_contact_id[contact_id] = linked_member_id

    updated = []
    changed = 0
    for sale in sales or []:
        row = dict(sale)
        if row.get("member_id") in {None, ""}:
            loyalty_contact_id = str(row.get("loyalty_contact_id") or "").strip()
            resolved_member_id = loyalty_member_by_contact_id.get(loyalty_contact_id, "")
            if resolved_member_id:
                row["member_id"] = resolved_member_id
                changed += 1
        updated.append(row)

    return updated, changed


def load_pos_customer_groups(pg: SyncPostgrestClient) -> list[dict]:
    return _load_json_list_setting(pg, POS_CUSTOMER_GROUPS_KEY)


def save_pos_customer_groups(pg: SyncPostgrestClient, rows: list[dict]):
    _save_json_list_setting(pg, POS_CUSTOMER_GROUPS_KEY, rows)


def load_pos_loyalty_settings(pg: SyncPostgrestClient) -> dict:
    raw = get_setting(pg, POS_LOYALTY_SETTINGS_KEY)
    if not raw:
        return {
            "enabled": True,
            "earn_points_per_dollar": 1.0,
            "redeem_dollars_per_point": 0.01,
        }
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "enabled": bool(data.get("enabled", True)),
                "earn_points_per_dollar": float(data.get("earn_points_per_dollar", 1.0) or 1.0),
                "redeem_dollars_per_point": float(data.get("redeem_dollars_per_point", 0.01) or 0.01),
            }
    except Exception:
        pass
    return {
        "enabled": True,
        "earn_points_per_dollar": 1.0,
        "redeem_dollars_per_point": 0.01,
    }


def save_pos_loyalty_settings(pg: SyncPostgrestClient, cfg: dict):
    save_setting(
        pg,
        POS_LOYALTY_SETTINGS_KEY,
        json.dumps(
            {
                "enabled": bool(cfg.get("enabled", True)),
                "earn_points_per_dollar": float(cfg.get("earn_points_per_dollar", 1.0) or 1.0),
                "redeem_dollars_per_point": float(cfg.get("redeem_dollars_per_point", 0.01) or 0.01),
            }
        ),
    )


def load_pos_loyalty_points(pg: SyncPostgrestClient) -> dict:
    raw = get_setting(pg, POS_LOYALTY_POINTS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                try:
                    out[str(k)] = int(v)
                except Exception:
                    out[str(k)] = 0
            return out
    except Exception:
        pass
    return {}


def save_pos_loyalty_points(pg: SyncPostgrestClient, points_by_member_id: dict):
    safe = {}
    for k, v in (points_by_member_id or {}).items():
        try:
            safe[str(k)] = max(0, int(v))
        except Exception:
            safe[str(k)] = 0
    save_setting(pg, POS_LOYALTY_POINTS_KEY, json.dumps(safe))


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _normalized_name(first_name: str, last_name: str) -> str:
    return f"{str(first_name or '').strip().lower()}|{str(last_name or '').strip().lower()}"


def _parse_int_safe(value, default=0) -> int:
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned == "":
                return default
            return int(float(cleaned))
        return int(float(value))
    except Exception:
        return default


def _split_name_parts(full_name: str) -> tuple[str, str]:
    clean = str(full_name or "").strip()
    if not clean:
        return "", ""
    if "," in clean:
        last, first = [x.strip() for x in clean.split(",", 1)]
        return first, last
    parts = clean.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def load_pos_loyalty_customers(pg: SyncPostgrestClient) -> list[dict]:
    rows = _load_json_list_setting(pg, POS_LOYALTY_CUSTOMERS_KEY)
    out = []
    for row in rows:
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        out.append(
            {
                "id": cid,
                "first_name": str(row.get("first_name") or "").strip(),
                "last_name": str(row.get("last_name") or "").strip(),
                "phone": str(row.get("phone") or "").strip(),
                "email": str(row.get("email") or "").strip(),
                "member_id": row.get("member_id"),
                "external_id": str(row.get("external_id") or "").strip(),
                "source": str(row.get("source") or "manual").strip(),
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
        )
    return out


def save_pos_loyalty_customers(pg: SyncPostgrestClient, rows: list[dict]):
    now_txt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe = []
    for row in rows or []:
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        safe.append(
            {
                "id": cid,
                "first_name": str(row.get("first_name") or "").strip(),
                "last_name": str(row.get("last_name") or "").strip(),
                "phone": str(row.get("phone") or "").strip(),
                "email": str(row.get("email") or "").strip(),
                "member_id": row.get("member_id"),
                "external_id": str(row.get("external_id") or "").strip(),
                "source": str(row.get("source") or "manual").strip(),
                "created_at": str(row.get("created_at") or now_txt),
                "updated_at": now_txt,
            }
        )
    _save_json_list_setting(pg, POS_LOYALTY_CUSTOMERS_KEY, safe)


def _loyalty_contact_label(contact: dict) -> str:
    full_name = f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip() or "Unnamed"
    phone = str(contact.get("phone") or "").strip() or "No phone"
    member_id = contact.get("member_id")
    suffix = f" | Member ID {member_id}" if member_id is not None and str(member_id) != "" else ""
    return f"{full_name} | {phone}{suffix}"


def reconcile_loyalty_contacts_with_members(
    loyalty_customers: list[dict],
    members: list[dict],
    loyalty_points: dict,
) -> tuple[list[dict], dict, int]:
    by_phone = {}
    by_name = {}
    for member in members or []:
        mid = member.get("id")
        if mid is None:
            continue
        mid_txt = str(mid)
        m_phone = _normalize_phone(member.get("phone", ""))
        if m_phone and m_phone not in by_phone:
            by_phone[m_phone] = mid_txt
        m_name_key = _normalized_name(member.get("first_name", ""), member.get("last_name", ""))
        if m_name_key != "|" and m_name_key not in by_name:
            by_name[m_name_key] = mid_txt

    safe_points = {str(k): max(0, int(v)) for k, v in (loyalty_points or {}).items()}
    linked = 0
    updated = []
    for contact in loyalty_customers or []:
        current = dict(contact)
        cid = str(current.get("id") or "").strip()
        if not cid:
            continue
        existing_mid = current.get("member_id")
        if existing_mid is not None and str(existing_mid).strip() != "":
            current["member_id"] = str(existing_mid)
            updated.append(current)
            continue

        match_mid = None
        c_phone = _normalize_phone(current.get("phone", ""))
        if c_phone and c_phone in by_phone:
            match_mid = by_phone[c_phone]

        if match_mid:
            current["member_id"] = str(match_mid)
            linked += 1
            contact_points = int(safe_points.get(cid, 0))
            if contact_points > 0:
                safe_points[str(match_mid)] = int(safe_points.get(str(match_mid), 0)) + contact_points
                safe_points.pop(cid, None)
        updated.append(current)

    return updated, safe_points, linked


def _map_remote_customer_to_loyalty_contact(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None

    first_name = str(item.get("first_name") or item.get("firstname") or "").strip()
    last_name = str(item.get("last_name") or item.get("lastname") or "").strip()
    if not first_name and not last_name:
        first_name, last_name = _split_name_parts(
            item.get("name")
            or item.get("customer_name")
            or item.get("fullname")
            or ""
        )

    phone = str(
        item.get("phone")
        or item.get("mobile")
        or item.get("cell")
        or item.get("phonenumber")
        or item.get("phone_number")
        or ""
    ).strip()
    email = str(item.get("email") or item.get("email_address") or "").strip()
    external_id = str(
        item.get("id")
        or item.get("customerid")
        or item.get("customer_id")
        or item.get("memberid")
        or ""
    ).strip()
    imported_points = max(
        0,
        _parse_int_safe(
            item.get("points")
            or item.get("loyalty_points")
            or item.get("reward_points")
            or item.get("point_balance")
            or item.get("points_balance")
            or 0
        ),
    )

    if not (first_name or last_name) and not phone:
        return None

    unique_seed = (external_id or "") + "|" + _normalize_phone(phone) + "|" + _normalized_name(first_name, last_name)
    cid = "lc_" + hashlib.sha1(unique_seed.encode("utf-8")).hexdigest()[:18]
    return {
        "id": cid,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "email": email,
        "member_id": None,
        "external_id": external_id,
        "source": "cigarpos",
        "import_points": int(imported_points),
    }


def fetch_cigarpos_loyalty_customers(base_url: str, username: str, password: str) -> list[dict]:
    session = _cigarpos_login_session(base_url, username, password)
    base = _normalize_base_url(base_url)
    endpoints = [
        ("/api/customers/get", "post"),
        ("/api/customer/get", "post"),
        ("/api/clients/get", "post"),
        ("/api/customers/get", "get"),
        ("/api/customers", "get"),
    ]

    last_error = "No valid customer endpoint response"
    for path, method in endpoints:
        url = f"{base}{path}"
        try:
            if method == "post":
                resp = session.post(url, data={"data": json.dumps({})}, timeout=30)
            else:
                resp = session.get(url, timeout=30)
            if resp.status_code in {404, 405}:
                last_error = f"{path} returned {resp.status_code}"
                continue
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("error") not in {None, "OK"}:
                last_error = str(payload.get("error"))
                continue

            raw_rows = payload.get("data") if isinstance(payload, dict) else payload
            rows = _extract_remote_items_payload(raw_rows)
            mapped = []
            for row in rows:
                mapped_row = _map_remote_customer_to_loyalty_contact(row)
                if mapped_row is not None:
                    mapped.append(mapped_row)
            if mapped:
                return mapped
            last_error = f"{path} returned no usable customer rows"
        except Exception as exc:
            last_error = str(exc)
            continue

    raise ValueError(last_error)


def _extract_remote_sales_payload(raw_data) -> list[dict]:
    rows = _extract_remote_items_payload(raw_data)
    return [row for row in rows if isinstance(row, dict)]


def _extract_sale_customer_phone(item: dict) -> str:
    direct_phone = str(
        item.get("phone")
        or item.get("mobile")
        or item.get("cell")
        or item.get("customer_phone")
        or item.get("customerphone")
        or item.get("phonenumber")
        or ""
    ).strip()
    if direct_phone:
        return direct_phone

    customer = item.get("customer")
    if isinstance(customer, dict):
        nested = str(
            customer.get("phone")
            or customer.get("mobile")
            or customer.get("cell")
            or customer.get("phonenumber")
            or ""
        ).strip()
        if nested:
            return nested
    return ""


def _extract_sale_timestamp(item: dict) -> str:
    for key in (
        "created_at",
        "createdon",
        "created",
        "date",
        "sale_date",
        "saledate",
        "invoice_date",
        "datetime",
        "updated_at",
    ):
        value = item.get(key)
        if value not in {None, ""}:
            return str(value)
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_sale_lines(item: dict) -> list[dict]:
    for key in ("items", "line_items", "lines", "products", "details"):
        value = item.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _map_remote_sale_to_pos_sale(
    item: dict,
    member_id: str,
    loyalty_contact_id: str,
    inventory_by_sku: dict,
) -> dict:
    lines = _extract_sale_lines(item)
    sale_items = []
    subtotal_from_lines = 0.0
    cost_total = 0.0

    for line in lines:
        sku = str(
            line.get("sku")
            or line.get("code")
            or line.get("stock_code")
            or line.get("stockcode")
            or ""
        ).strip()
        name = str(line.get("name") or line.get("item") or line.get("description") or "").strip()
        qty = max(0, _to_int(line.get("qty") or line.get("quantity") or line.get("count") or 0))
        unit_price = round(
            max(0.0, _to_float(line.get("price") or line.get("unit_price") or line.get("rate") or 0.0)),
            2,
        )
        unit_cost = round(max(0.0, _to_float(line.get("cost") or line.get("unit_cost") or 0.0)), 2)

        inv_item = inventory_by_sku.get(sku.lower(), {}) if sku else {}
        if unit_cost <= 0:
            unit_cost = round(max(0.0, _to_float(inv_item.get("cost") or 0.0)), 2)
        if not name:
            name = str(inv_item.get("name") or sku or "Item").strip()

        line_total = round(unit_price * qty, 2)
        line_cost = round(unit_cost * qty, 2)
        subtotal_from_lines += line_total
        cost_total += line_cost

        sale_items.append(
            {
                "sku": sku,
                "name": name,
                "category": str(line.get("category") or inv_item.get("category") or "").strip(),
                "qty": qty,
                "price": unit_price,
                "unit_cost": unit_cost,
                "regular_line_total": line_total,
                "allocated_discount": 0.0,
                "discounted_line_total": line_total,
                "regular_margin": round(line_total - line_cost, 2),
                "discounted_margin": round(line_total - line_cost, 2),
            }
        )

    subtotal = round(
        max(
            0.0,
            _to_float(item.get("subtotal") or item.get("sub_total") or item.get("regular_total") or subtotal_from_lines),
        ),
        2,
    )
    total = round(
        max(
            0.0,
            _to_float(item.get("total") or item.get("grand_total") or item.get("discounted_total") or subtotal),
        ),
        2,
    )
    discount = round(max(0.0, _to_float(item.get("discount") or (subtotal - total))), 2)
    if total > subtotal:
        total = subtotal
        discount = 0.0

    if cost_total <= 0 and sale_items:
        cost_total = compute_sale_cost_from_items(sale_items)
    cost_total = round(max(0.0, cost_total), 2)

    remote_id = str(
        item.get("id")
        or item.get("sale_id")
        or item.get("invoice_id")
        or item.get("invoiceid")
        or item.get("transaction_id")
        or ""
    ).strip()
    if not remote_id:
        fingerprint = json.dumps(item, sort_keys=True, default=str)
        remote_id = "auto_" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]

    customer_name = str(
        item.get("customer_name")
        or item.get("customer")
        or item.get("name")
        or "Member"
    ).strip()
    if not customer_name:
        customer_name = "Member"

    created_at = _extract_sale_timestamp(item)
    sale_id = f"cpos_{remote_id}"

    return {
        "id": sale_id,
        "created_at": created_at,
        "customer": customer_name,
        "member_id": member_id,
        "loyalty_contact_id": loyalty_contact_id or None,
        "subtotal": subtotal,
        "discount": discount,
        "total": total,
        "regular_total": subtotal,
        "discounted_total": total,
        "cost_total": cost_total,
        "member_discount_total": discount,
        "gross_margin_regular": round(subtotal - cost_total, 2),
        "gross_margin_discounted": round(total - cost_total, 2),
        "promotion": "",
        "discount_source": "Imported",
        "payment_method": str(item.get("payment_method") or item.get("payment") or "Imported"),
        "loyalty_points_redeemed": 0,
        "items": sale_items,
        "remote_source": "cigarpos",
        "remote_source_id": remote_id,
    }


def fetch_cigarpos_sales_rows(base_url: str, username: str, password: str) -> tuple[list[dict], str]:
    session = _cigarpos_login_session(base_url, username, password)
    base = _normalize_base_url(base_url)

    endpoints = [
        ("/api/sales/get", "post"),
        ("/api/sale/get", "post"),
        ("/api/invoices/get", "post"),
        ("/api/transactions/get", "post"),
        ("/api/sales", "get"),
        ("/api/invoices", "get"),
    ]
    payload_candidates = [
        {},
        {"limit": 1000},
        {"page": 1, "limit": 1000},
        {"start": 0, "limit": 1000},
    ]

    last_error = "No CigarPOS sales endpoint responded with usable data."
    for path, method in endpoints:
        url = f"{base}{path}"
        for payload in payload_candidates:
            try:
                if method == "post":
                    resp = session.post(url, data={"data": json.dumps(payload)}, timeout=30)
                else:
                    resp = session.get(url, timeout=30)
                if resp.status_code in {404, 405}:
                    last_error = f"{path} returned {resp.status_code}"
                    continue
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("error") not in {None, "OK"}:
                    last_error = str(data.get("error"))
                    continue

                raw_rows = data.get("data") if isinstance(data, dict) else data
                rows = _extract_remote_sales_payload(raw_rows)
                if rows:
                    return rows, f"{method.upper()} {path}"
                last_error = f"{method.upper()} {path} returned no sales rows"
            except Exception as exc:
                last_error = str(exc)
                continue

    raise ValueError(last_error)


def sync_cigarpos_member_sales(pg: SyncPostgrestClient) -> dict:
    cfg = load_cigarpos_settings(pg)
    if not cfg.get("base_url") or not cfg.get("username") or not cfg.get("password"):
        raise ValueError("Configure CigarPOS URL, username, and password first.")

    members = fetch_members(pg)
    loyalty_customers = load_pos_loyalty_customers(pg)
    existing_sales = load_pos_sales(pg)
    inventory = load_pos_inventory(pg)

    member_id_by_phone = {}
    for member in members or []:
        mid = str(member.get("id") or "").strip()
        phone = _normalize_phone(member.get("phone", ""))
        if mid and phone and phone not in member_id_by_phone:
            member_id_by_phone[phone] = mid

    loyalty_id_by_phone = {}
    for contact in loyalty_customers or []:
        phone = _normalize_phone(contact.get("phone", ""))
        cid = str(contact.get("id") or "").strip()
        if phone and cid and phone not in loyalty_id_by_phone:
            loyalty_id_by_phone[phone] = cid

    inventory_by_sku = {
        str(item.get("sku", "")).strip().lower(): item
        for item in inventory
        if str(item.get("sku", "")).strip()
    }

    rows, endpoint_used = fetch_cigarpos_sales_rows(cfg["base_url"], cfg["username"], cfg["password"])

    existing_by_remote = {}
    for sale in existing_sales:
        if str(sale.get("remote_source") or "") != "cigarpos":
            continue
        remote_id = str(sale.get("remote_source_id") or "").strip()
        if remote_id:
            existing_by_remote[remote_id] = sale

    imported = 0
    updated = 0
    matched = 0
    for row in rows:
        phone = _normalize_phone(_extract_sale_customer_phone(row))
        member_id = member_id_by_phone.get(phone)
        if not member_id:
            continue
        matched += 1
        loyalty_id = loyalty_id_by_phone.get(phone, "")
        mapped = _map_remote_sale_to_pos_sale(row, member_id, loyalty_id, inventory_by_sku)
        remote_id = str(mapped.get("remote_source_id") or "").strip()
        if remote_id in existing_by_remote:
            existing_by_remote[remote_id].update(mapped)
            updated += 1
        else:
            existing_sales.append(mapped)
            if remote_id:
                existing_by_remote[remote_id] = mapped
            imported += 1

    existing_sales = existing_sales[-2000:]
    save_pos_sales(pg, existing_sales)
    now_txt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_setting(pg, CIGARPOS_SALES_LAST_SYNC_KEY, now_txt)

    return {
        "endpoint": endpoint_used,
        "remote_rows": len(rows),
        "matched_member_rows": matched,
        "imported": imported,
        "updated": updated,
        "when": now_txt,
    }


def merge_loyalty_contacts(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int, dict]:
    index_by_phone = {}
    index_by_name = {}
    merged = []

    for row in existing or []:
        row_copy = dict(row)
        merged.append(row_copy)
    for idx, row in enumerate(merged):
        phone_key = _normalize_phone(row.get("phone", ""))
        if phone_key:
            index_by_phone[phone_key] = idx
        name_key = _normalized_name(row.get("first_name", ""), row.get("last_name", ""))
        if name_key != "|":
            index_by_name[name_key] = idx

    added = 0
    updated = 0
    imported_points_by_key = {}
    for row in incoming or []:
        phone_key = _normalize_phone(row.get("phone", ""))
        name_key = _normalized_name(row.get("first_name", ""), row.get("last_name", ""))
        idx = None
        if phone_key and phone_key in index_by_phone:
            idx = index_by_phone[phone_key]
        elif name_key != "|" and name_key in index_by_name:
            idx = index_by_name[name_key]

        if idx is None:
            merged.append(dict(row))
            idx = len(merged) - 1
            if phone_key:
                index_by_phone[phone_key] = idx
            if name_key != "|":
                index_by_name[name_key] = idx
            added += 1
            continue

        current = merged[idx]
        for field in ("first_name", "last_name", "phone", "email", "external_id", "source"):
            incoming_val = str(row.get(field) or "").strip()
            if incoming_val and not str(current.get(field) or "").strip():
                current[field] = incoming_val
        if current.get("member_id") in {None, ""} and row.get("member_id") not in {None, ""}:
            current["member_id"] = row.get("member_id")

        imported_points = max(0, _parse_int_safe(row.get("import_points"), default=0))
        if imported_points > 0:
            points_key = str(current.get("member_id") or current.get("id") or "").strip()
            if points_key:
                imported_points_by_key[points_key] = max(
                    int(imported_points_by_key.get(points_key, 0)),
                    int(imported_points),
                )
        updated += 1

    return merged, added, updated, imported_points_by_key


def create_member_from_loyalty_contact(
    pg: SyncPostgrestClient,
    contact: dict,
    tier: str,
    locker: str,
    months: int,
) -> str:
    first_name = str(contact.get("first_name") or "").strip()
    last_name = str(contact.get("last_name") or "").strip()
    if not first_name:
        raise ValueError("First name is required to create a member.")
    if not last_name:
        last_name = "Loyalty"

    today = datetime.date.today().strftime("%Y-%m-%d")
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": str(contact.get("email") or "").strip(),
        "phone": str(contact.get("phone") or "").strip(),
        "tier": tier,
        "status": "Active",
        "locker": locker or "—",
        "join_date": today,
        "next_billing_date": advance_billing(today, tier, int(months)),
        "last_reminder": "None",
    }

    inserted_id = None
    try:
        resp = pg.from_("members").insert(payload).execute()
        data = getattr(resp, "data", None) or []
        if data and isinstance(data[0], dict) and data[0].get("id") is not None:
            inserted_id = str(data[0].get("id"))
    except Exception:
        payload.pop("phone", None)
        resp = pg.from_("members").insert(payload).execute()
        data = getattr(resp, "data", None) or []
        if data and isinstance(data[0], dict) and data[0].get("id") is not None:
            inserted_id = str(data[0].get("id"))

    if inserted_id:
        return inserted_id

    all_members = fetch_members(pg)
    name_matches = [
        m for m in all_members
        if str(m.get("first_name", "")).strip().lower() == first_name.lower()
        and str(m.get("last_name", "")).strip().lower() == last_name.lower()
    ]
    if not name_matches:
        raise ValueError("Member was created but could not be reloaded.")
    name_matches.sort(key=lambda m: int(m.get("id", 0)), reverse=True)
    return str(name_matches[0].get("id"))


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "t"}


def parse_inventory_csv(csv_text: str) -> tuple[list[dict], int]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    skipped = 0

    def pick(norm_row: dict, *keys: str) -> str:
        for key in keys:
            val = norm_row.get(key)
            if val not in (None, ""):
                return val
        return ""

    for raw in reader:
        norm = {str(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        sku = pick(norm, "sku", "stock code", "stockcode", "item_code", "item code", "code", "barcode", "upc")
        name = pick(norm, "name", "item", "item name", "product", "description")
        category = pick(norm, "category", "dept", "department", "group")
        barcode = pick(norm, "barcode", "upc", "ean", "gtin")
        price_txt = pick(norm, "price", "unit_price", "unit price", "retail", "sell_price", "selling price", "selling_price") or "0"
        stock_txt = pick(
            norm,
            "total stock",
            "total_stock",
            "seprate total stock",
            "seprate_total_stock",
            "stock",
            "qty",
            "quantity",
            "qty on hand",
            "on hand",
            "stock on hand",
        ) or "0"
        cost_txt = pick(norm, "cost", "unit cost", "purchase_cost", "buy price") or "0"
        taxable_txt = pick(norm, "taxable", "tax", "is_taxable")
        if not sku or not name:
            skipped += 1
            continue
        price = round(_to_float(price_txt, default=-1), 2)
        stock = _to_int(stock_txt, default=-1)
        cost = round(_to_float(cost_txt, default=-1), 2)
        if price < 0 or stock < 0 or cost < 0:
            skipped += 1
            continue
        rows.append(
            {
                "sku": sku,
                "name": name,
                "category": category,
                "barcode": barcode,
                "price": max(0.0, price),
                "cost": max(0.0, cost),
                "stock": max(0, stock),
                "taxable": _truthy(taxable_txt),
            }
        )
    return rows, skipped


def _promotion_applies(
    promo: dict,
    cart: list[dict],
    is_member: bool,
    member_tier: str,
    selected_member_id,
    customer_groups: list[dict],
) -> bool:
    if not promo.get("active", True):
        return False
    apply_to = (promo.get("apply_to") or "All").strip()
    target = (promo.get("target") or "").strip().lower()
    if apply_to == "Members only" and not is_member:
        return False
    if apply_to == "Non-members only" and is_member:
        return False
    if apply_to == "Tier" and (member_tier or "").strip().lower() != target:
        return False
    if apply_to == "SKU":
        skus = {(line.get("sku") or "").strip().lower() for line in cart}
        if target not in skus:
            return False
    if apply_to == "Category":
        cats = {(line.get("category") or "").strip().lower() for line in cart}
        if target not in cats:
            return False
    if apply_to == "Customer Group":
        group_match = None
        for group in (customer_groups or []):
            gid = str(group.get("id", "")).strip().lower()
            gname = str(group.get("name", "")).strip().lower()
            if target in {gid, gname}:
                group_match = group
                break
        if not group_match or selected_member_id is None:
            return False
        member_ids = {str(x) for x in (group_match.get("member_ids") or [])}
        if str(selected_member_id) not in member_ids:
            return False
    return True


def calculate_discount(subtotal: float, promo: dict) -> float:
    if not promo:
        return 0.0
    kind = (promo.get("kind") or "Percent").strip()
    value = float(promo.get("value") or 0)
    if subtotal <= 0 or value <= 0:
        return 0.0
    if kind == "Percent":
        return min(subtotal, round(subtotal * (value / 100.0), 2))
    return min(subtotal, round(value, 2))


def _sanitize_scan_channel(channel: str) -> str:
    raw = (channel or "").strip().lower()
    clean = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return clean or "main"


def _scan_queue_key(channel: str) -> str:
    return f"pos_scan_queue_{_sanitize_scan_channel(channel)}"


def load_scan_queue(pg: SyncPostgrestClient, channel: str) -> list[dict]:
    return _load_json_list_setting(pg, _scan_queue_key(channel))


def save_scan_queue(pg: SyncPostgrestClient, channel: str, queue_rows: list[dict]):
    _save_json_list_setting(pg, _scan_queue_key(channel), queue_rows[-300:])


def enqueue_scan(pg: SyncPostgrestClient, channel: str, code: str, source: str = "phone"):
    code_clean = (code or "").strip()
    if not code_clean:
        return
    queue_rows = load_scan_queue(pg, channel)
    queue_rows.append(
        {
            "code": code_clean,
            "source": (source or "phone").strip()[:60],
            "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_scan_queue(pg, channel, queue_rows)


def drain_scan_queue(pg: SyncPostgrestClient, channel: str, limit: int = 60) -> list[dict]:
    queue_rows = load_scan_queue(pg, channel)
    if not queue_rows:
        return []
    take = queue_rows[: max(1, int(limit))]
    remain = queue_rows[len(take):]
    save_scan_queue(pg, channel, remain)
    return take


def find_inventory_item_by_code(inventory: list[dict], code: str):
    needle = (code or "").strip().lower()
    if not needle:
        return None
    for item in inventory:
        sku = str(item.get("sku") or "").strip().lower()
        barcode = str(item.get("barcode") or "").strip().lower()
        if needle == sku or needle == barcode:
            return item
    return None


def import_scans_to_cart(
    pg: SyncPostgrestClient,
    channel: str,
    inventory: list[dict],
    cart: list[dict],
    limit: int = 80,
) -> tuple[int, list[str]]:
    incoming = drain_scan_queue(pg, channel, limit=limit)
    if not incoming:
        return 0, []

    added = 0
    misses = []
    for ev in incoming:
        item = find_inventory_item_by_code(inventory, ev.get("code", ""))
        if not item:
            misses.append(ev.get("code", ""))
            continue

        code_sku = item.get("sku", "")
        existing = None
        for line in cart:
            if line.get("sku") == code_sku:
                existing = line
                break
        if existing:
            existing["qty"] = int(existing.get("qty", 0)) + 1
        else:
            cart.append(
                {
                    "sku": code_sku,
                    "name": item.get("name", ""),
                    "category": item.get("category", ""),
                    "price": float(item.get("price", 0.0)),
                    "cost": float(item.get("cost", 0.0)),
                    "qty": 1,
                }
            )
        added += 1
    return added, misses


def _bool_setting(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_setting(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _fernet_from_app_secret():
    try:
        fernet_mod = importlib.import_module("cryptography.fernet")
        fernet_cls = getattr(fernet_mod, "Fernet", None)
    except Exception:
        return None
    if fernet_cls is None:
        return None
    secret_seed = st.secrets.get("SUPABASE_KEY") or "liberty-smokes-local"
    digest = hashlib.sha256((str(secret_seed) + "|cigarpos-sync").encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    try:
        return fernet_cls(key)
    except Exception:
        return None


def has_fernet_support() -> bool:
    try:
        fernet_mod = importlib.import_module("cryptography.fernet")
        return getattr(fernet_mod, "Fernet", None) is not None
    except Exception:
        return False


def trigger_optional_autorefresh(interval_ms: int, key: str) -> bool:
    try:
        module = importlib.import_module("streamlit_autorefresh")
        func = getattr(module, "st_autorefresh", None)
        if callable(func):
            func(interval=int(interval_ms), key=key)
            return True
        return False
    except Exception:
        return False


def encrypt_secret(plain_text: str) -> str:
    if not plain_text:
        return ""
    f = _fernet_from_app_secret()
    if f is None:
        # Compatibility fallback when cryptography is unavailable.
        return "plain:" + plain_text
    token = f.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    return "fernet:" + token


def decrypt_secret(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    if cipher_text.startswith("plain:"):
        return cipher_text[len("plain:"):]
    if cipher_text.startswith("fernet:"):
        token = cipher_text[len("fernet:"):]
        f = _fernet_from_app_secret()
        if f is None:
            return ""
        try:
            return f.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return ""
    return ""


def load_cigarpos_settings(pg: SyncPostgrestClient) -> dict:
    return {
        "base_url": get_setting(pg, CIGARPOS_BASE_URL_KEY).strip(),
        "username": get_setting(pg, CIGARPOS_USERNAME_KEY).strip(),
        "password": decrypt_secret(get_setting(pg, CIGARPOS_PASSWORD_KEY)),
        "auto_sync": _bool_setting(get_setting(pg, CIGARPOS_AUTO_SYNC_KEY), False),
        "auto_sync_min": max(5, _int_setting(get_setting(pg, CIGARPOS_AUTO_SYNC_MIN_KEY), 60)),
    }


def save_cigarpos_settings(
    pg: SyncPostgrestClient,
    base_url: str,
    username: str,
    password: str,
    auto_sync: bool,
    auto_sync_min: int,
):
    save_setting(pg, CIGARPOS_BASE_URL_KEY, (base_url or "").strip())
    save_setting(pg, CIGARPOS_USERNAME_KEY, (username or "").strip())
    if password:
        save_setting(pg, CIGARPOS_PASSWORD_KEY, encrypt_secret(password))
    save_setting(pg, CIGARPOS_AUTO_SYNC_KEY, "true" if auto_sync else "false")
    save_setting(pg, CIGARPOS_AUTO_SYNC_MIN_KEY, str(max(5, int(auto_sync_min))))


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _cigarpos_login_session(base_url: str, username: str, password: str) -> requests.Session:
    base = _normalize_base_url(base_url)
    if not base or not username or not password:
        raise ValueError("Base URL, username, and password are required.")

    session = requests.Session()
    payload = {
        "username": username,
        "password": hashlib.sha256(password.encode("utf-8")).hexdigest(),
        "getsessiontokens": True,
        "start_date": int(datetime.datetime.now().timestamp() * 1000),
        "uuid": f"liberty-{hashlib.sha1(username.encode('utf-8')).hexdigest()[:16]}",
    }
    res = session.post(
        f"{base}/api/auth",
        data={"data": json.dumps(payload)},
        timeout=20,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("error") != "OK":
        raise ValueError(data.get("error") or "Authentication failed")
    return session


def test_cigarpos_connection(base_url: str, username: str, password: str) -> tuple[bool, str]:
    try:
        session = _cigarpos_login_session(base_url, username, password)
        base = _normalize_base_url(base_url)
        check = session.get(f"{base}/api/hello", timeout=15)
        if check.status_code == 200:
            return True, "Connected and authenticated."
        return True, "Authenticated, but /api/hello did not return 200."
    except Exception as exc:
        return False, str(exc)


def _extract_remote_items_payload(raw_data) -> list:
    if isinstance(raw_data, dict):
        return list(raw_data.values())
    if isinstance(raw_data, list):
        return raw_data
    return []


def _to_float(value, default=0.0) -> float:
    try:
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "").replace("$", "")
            if cleaned == "":
                return default
            return float(cleaned)
        return float(value)
    except Exception:
        return default


def _to_int(value, default=0) -> int:
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned == "":
                return default
            if cleaned.lower() in {"true", "false", "yes", "no", "y", "n", "t", "f"}:
                return default
            return int(float(cleaned))
        return int(float(value))
    except Exception:
        return default


def _map_remote_item_to_inventory(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    norm_item = {str(k or "").strip().lower(): v for k, v in item.items()}

    def pick(*keys: str):
        for key in keys:
            if key in norm_item and norm_item.get(key) not in (None, ""):
                return norm_item.get(key)
        return None

    def pick_stock(*keys: str):
        for key in keys:
            if key not in norm_item:
                continue
            val = norm_item.get(key)
            if val in (None, ""):
                continue
            if isinstance(val, bool):
                continue
            if isinstance(val, str) and val.strip().lower() in {"true", "false", "yes", "no", "y", "n", "t", "f"}:
                continue
            return val
        return None

    sku = str(pick("code", "sku", "stockcode", "stock code", "itemcode", "item_code") or "").strip()
    name = str(pick("name", "item", "description", "item name", "product") or "").strip()
    if not sku or not name:
        return None
    price = round(max(0.0, _to_float(pick("price", "retail", "retailprice", "saleprice", "selling price") or 0.0)), 2)
    cost = round(max(0.0, _to_float(pick("cost", "itemcost", "costprice", "unit cost") or 0.0)), 2)
    stock = max(0, _to_int(
        pick_stock(
            "total_stock",
            "total stock",
            "seprate_total_stock",
            "seprate total stock",
            "qty",
            "quantity",
            "stock",
            "onhand",
            "on_hand",
            "qoh",
            "qtyonhand",
            "qty_on_hand",
            "stockqty",
            "stock_qty",
            "quantityonhand",
            "quantity_on_hand",
            "qty on hand",
            "quantity on hand",
            "qtyinstock",
            "currentstock",
            "available_qty",
        )
        or 0
    ))
    category = str(pick("category", "categoryid", "dept", "deptname", "department", "departmentname") or "").strip()
    taxable = _truthy(pick("taxable", "tax", "is_taxable"))
    return {
        "sku": sku,
        "name": name,
        "category": category,
        "price": price,
        "cost": cost,
        "stock": stock,
        "taxable": taxable,
    }


def fetch_cigarpos_inventory(base_url: str, username: str, password: str) -> list[dict]:
    session = _cigarpos_login_session(base_url, username, password)
    base = _normalize_base_url(base_url)

    resp = session.get(f"{base}/api/items/get", timeout=30)
    if resp.status_code == 405:
        resp = session.post(f"{base}/api/items/get", data={"data": json.dumps({})}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error") != "OK":
        raise ValueError(payload.get("error") or "Failed to fetch inventory")

    remote_items = _extract_remote_items_payload(payload.get("data"))
    mapped = []
    for item in remote_items:
        mapped_item = _map_remote_item_to_inventory(item)
        if mapped_item is not None:
            mapped.append(mapped_item)
    return mapped


def run_cigarpos_inventory_sync(pg: SyncPostgrestClient, merge_mode: bool = True) -> tuple[int, str]:
    cfg = load_cigarpos_settings(pg)
    items = fetch_cigarpos_inventory(cfg["base_url"], cfg["username"], cfg["password"])
    existing = load_pos_inventory(pg)
    if merge_mode:
        merged = {str(i.get("sku", "")).strip().lower(): i for i in existing if i.get("sku")}
        for row in items:
            merged[str(row.get("sku", "")).strip().lower()] = row
        final_rows = list(merged.values())
    else:
        final_rows = items
    save_pos_inventory(pg, final_rows)
    now_txt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_setting(pg, POS_LAST_SYNC_KEY, now_txt)
    clear_setting(pg, POS_LAST_SYNC_ERROR_KEY)
    return len(items), now_txt


def maybe_auto_sync_cigarpos(pg: SyncPostgrestClient) -> tuple[bool, str]:
    cfg = load_cigarpos_settings(pg)
    if not cfg.get("auto_sync"):
        return False, "Auto-sync disabled"
    if not cfg.get("base_url") or not cfg.get("username") or not cfg.get("password"):
        return False, "CigarPOS credentials not configured"

    last_sync = get_setting(pg, POS_LAST_SYNC_KEY)
    if last_sync:
        try:
            last_dt = datetime.datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            minutes_since = (datetime.datetime.now() - last_dt).total_seconds() / 60.0
            if minutes_since < int(cfg.get("auto_sync_min", 60)):
                return False, "Auto-sync interval not reached"
        except Exception:
            pass

    try:
        run_cigarpos_inventory_sync(pg, merge_mode=True)
        return True, "CigarPOS inventory auto-sync complete"
    except Exception as exc:
        save_setting(pg, POS_LAST_SYNC_ERROR_KEY, str(exc))
        return False, f"Auto-sync failed: {exc}"


EMAIL_TEMPLATE_DEFAULTS = {
    "welcome_subject": "Welcome to Liberty Smokes, {first_name}!",
    "welcome_body": (
        "Hi {first_name},\n\n"
        "Welcome to Liberty Smokes. You're enrolled in our {tier} membership plan.\n"
        "Your next payment due date is {next_billing_date}.\n\n"
        "We look forward to seeing you in the shop."
    ),
    "renewal_subject": "Liberty Smokes Renewal Reminder",
    "renewal_body": (
        "Hi {first_name}, your {tier} membership renews on {next_billing_date}. "
        "If payment is missed, you have a 7-day grace period before your membership is marked inactive."
    ),
    "past_due_subject": "Payment Past Due",
    "past_due_body": (
        "Hi {first_name}, your membership payment is now past due. "
        "You have a 7-day grace period before your membership is marked inactive."
    ),
}


class SafeTemplateDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def member_template_context(member: dict) -> dict:
    return {
        "first_name": (member.get("first_name") or "").strip(),
        "last_name": (member.get("last_name") or "").strip(),
        "full_name": (
            f"{member.get('first_name', '').strip()} {member.get('last_name', '').strip()}"
        ).strip(),
        "email": (member.get("email") or "").strip(),
        "phone": (member.get("phone") or "").strip(),
        "tier": (member.get("tier") or "").strip(),
        "status": (member.get("status") or "").strip(),
        "locker": (member.get("locker") or "").strip(),
        "join_date": (member.get("join_date") or "").strip(),
        "next_billing_date": (member.get("next_billing_date") or "").strip(),
    }


def format_email_template(template: str, member: dict) -> str:
    try:
        return (template or "").format_map(SafeTemplateDict(member_template_context(member)))
    except Exception:
        return template or ""


def load_email_templates(pg: SyncPostgrestClient) -> dict:
    mapping = {
        "welcome_subject": "email_tpl_welcome_subject",
        "welcome_body": "email_tpl_welcome_body",
        "renewal_subject": "email_tpl_renewal_subject",
        "renewal_body": "email_tpl_renewal_body",
        "past_due_subject": "email_tpl_past_due_subject",
        "past_due_body": "email_tpl_past_due_body",
    }
    result = {}
    for key, db_key in mapping.items():
        saved = get_setting(pg, db_key)
        result[key] = saved if saved else EMAIL_TEMPLATE_DEFAULTS[key]
    return result


# ── Email helpers ──────────────────────────────────────────────────────────────

def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    to_addr: str,
    subject: str,
    body: str,
    security: str = "SSL",
    from_addr: str = "",
):
    normalized_to = parseaddr(str(to_addr or "").strip())[1].strip()
    if not normalized_to or "@" not in normalized_to:
        raise ValueError("Recipient email is missing or invalid.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr or smtp_user
    msg["To"] = normalized_to
    msg.set_content(body + "\n\nBest,\nLiberty Smokes Management")

    security_mode = (security or "SSL").upper()
    if security_mode == "SSL":
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if security_mode == "STARTTLS":
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def load_smtp_settings(pg: SyncPostgrestClient) -> dict:
    """Load SMTP settings with fallback to legacy Gmail keys."""
    host = get_setting(pg, "smtp_host") or "smtp.gmail.com"
    port_raw = get_setting(pg, "smtp_port") or "465"
    security = (get_setting(pg, "smtp_security") or "SSL").upper()
    username = get_setting(pg, "smtp_username") or get_setting(pg, "smtp_email")
    password = get_setting(pg, "smtp_password")
    from_addr = get_setting(pg, "smtp_from") or username
    try:
        port = int(port_raw)
    except Exception:
        port = 465
    return {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "password": password,
        "from_addr": from_addr,
    }


def load_ordering_smtp_settings(pg: SyncPostgrestClient) -> dict:
    host = get_setting(pg, "ordering_smtp_host") or ""
    port_raw = get_setting(pg, "ordering_smtp_port") or "465"
    security = (get_setting(pg, "ordering_smtp_security") or "SSL").upper()
    username = get_setting(pg, "ordering_smtp_username") or ""
    password = get_setting(pg, "ordering_smtp_password") or ""
    from_addr = get_setting(pg, "ordering_smtp_from") or username
    try:
        port = int(port_raw)
    except Exception:
        port = 465
    return {
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "password": password,
        "from_addr": from_addr,
    }


def _phone_to_e164(phone: str, default_country_code: str = "+1") -> str:
    raw = str(phone or "").strip()
    if not raw:
        return ""

    digits = _normalize_phone(raw)
    if not digits:
        return ""

    country = str(default_country_code or "+1").strip()
    if not country.startswith("+"):
        country = "+" + _normalize_phone(country)
    if country == "+":
        country = "+1"

    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return country + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return ""


def load_sms_settings(pg: SyncPostgrestClient) -> dict:
    return {
        "provider": (get_setting(pg, "sms_provider") or "twilio").strip().lower(),
        "account_sid": (get_setting(pg, "twilio_account_sid") or "").strip(),
        "auth_token": decrypt_secret(get_setting(pg, "twilio_auth_token")),
        "from_number": (get_setting(pg, "twilio_from_number") or "").strip(),
        "default_country_code": (get_setting(pg, "sms_default_country_code") or "+1").strip() or "+1",
    }


def send_sms_twilio(account_sid: str, auth_token: str, from_number: str, to_number: str, body: str) -> str:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = {
        "To": to_number,
        "From": from_number,
        "Body": body,
    }
    resp = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=30)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("message") or "")
        except Exception:
            detail = (resp.text or "").strip()
        if detail:
            raise ValueError(detail)
        raise ValueError(f"Twilio request failed ({resp.status_code}).")
    try:
        return str(resp.json().get("sid") or "")
    except Exception:
        return ""


def member_text_label(member: dict, to_number: str = "") -> str:
    full_name = f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", ")
    phone = str(member.get("phone") or "").strip()
    normalized = to_number or phone
    return f"{full_name} | {normalized}"


def send_mass_member_text(
    sms_cfg: dict,
    recipients: list[dict],
    body_template: str,
) -> tuple[int, list[tuple[str, str]]]:
    sent_count = 0
    failures: list[tuple[str, str]] = []
    for member in recipients:
        to_number = str(member.get("_sms_to") or "").strip()
        if not to_number:
            continue
        try:
            body = format_email_template(body_template, member)
            send_sms_twilio(
                sms_cfg["account_sid"],
                sms_cfg["auth_token"],
                sms_cfg["from_number"],
                to_number,
                body,
            )
            sent_count += 1
        except Exception as exc:
            failures.append((member_text_label(member, to_number=to_number), str(exc)))
    return sent_count, failures


def member_email_label(member: dict) -> str:
    full_name = f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", ")
    locker = str(member.get("locker") or "—").strip() or "—"
    email = str(member.get("email") or "").strip()
    return f"{full_name} | Locker {locker} | {email}"


def send_mass_member_email(
    smtp: dict,
    recipients: list[dict],
    subject_template: str,
    body_template: str,
) -> tuple[int, list[tuple[str, str]]]:
    sent_count = 0
    failures: list[tuple[str, str]] = []
    for member in recipients:
        email = str(member.get("email") or "").strip()
        if not email:
            continue
        try:
            send_email(
                smtp["host"],
                int(smtp["port"]),
                smtp["username"],
                smtp["password"],
                email,
                format_email_template(subject_template, member),
                format_email_template(body_template, member),
                security=smtp["security"],
                from_addr=smtp["from_addr"],
            )
            sent_count += 1
        except Exception as exc:
            failures.append((member_email_label(member), str(exc)))
    return sent_count, failures


def get_pending_reminders(members: list, templates: dict) -> list:
    today = datetime.date.today()
    pending = []
    for m in members:
        if m.get("status") == "Canceled":
            continue
        try:
            due = datetime.datetime.strptime(m["next_billing_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        diff = (due - today).days
        last_reminder = str(m.get("last_reminder") or "").strip()
        if 0 <= diff <= 7 and last_reminder != "7_pre":
            pending.append(
                {
                    "id": m["id"],
                    "email": m["email"],
                    "target": "7_pre",
                    "subject": format_email_template(templates["renewal_subject"], m),
                    "body": format_email_template(templates["renewal_body"], m),
                }
            )
        elif diff < 0 and last_reminder != "7_post":
            pending.append(
                {
                    "id": m["id"],
                    "email": m["email"],
                    "target": "7_post",
                    "subject": format_email_template(templates["past_due_subject"], m),
                    "body": format_email_template(templates["past_due_body"], m),
                }
            )
    return pending


def run_pending_member_reminders(
    pg: SyncPostgrestClient,
    smtp: dict,
    templates: dict,
    members: list[dict],
) -> dict:
    pending = get_pending_reminders(members, templates)
    sent_count = 0
    skipped_no_email = 0
    failures = []

    for row in pending:
        email = str(row.get("email") or "").strip()
        if not email:
            skipped_no_email += 1
            continue
        try:
            send_email(
                smtp["host"],
                int(smtp["port"]),
                smtp["username"],
                smtp["password"],
                email,
                row.get("subject", ""),
                row.get("body", ""),
                security=smtp.get("security", "SSL"),
                from_addr=smtp.get("from_addr", ""),
            )
            pg.from_("members").update({"last_reminder": row.get("target", "")}).eq(
                "id", row.get("id")
            ).execute()
            sent_count += 1
        except Exception as exc:
            failures.append(
                {
                    "id": row.get("id"),
                    "email": email,
                    "error": str(exc),
                }
            )

    return {
        "pending": len(pending),
        "sent": sent_count,
        "skipped_no_email": skipped_no_email,
        "failed": len(failures),
        "failures": failures,
    }


def maybe_run_automated_member_reminders(
    pg: SyncPostgrestClient,
    smtp: dict,
    templates: dict,
    members: list[dict],
) -> tuple[bool, str, dict]:
    enabled = _bool_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_ENABLED_KEY), False)
    if not enabled:
        return False, "disabled", {}

    interval_minutes = max(5, _int_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY), 60))
    now = datetime.datetime.now()
    last_run_raw = str(get_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY) or "").strip()
    if last_run_raw:
        try:
            last_run = datetime.datetime.fromisoformat(last_run_raw)
            elapsed_sec = (now - last_run).total_seconds()
            if elapsed_sec < interval_minutes * 60:
                return False, "throttled", {"minutes_remaining": int(((interval_minutes * 60) - elapsed_sec + 59) // 60)}
        except Exception:
            pass

    stats = run_pending_member_reminders(pg, smtp, templates, members)
    save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY, now.isoformat(timespec="seconds"))
    result_summary = (
        f"sent={stats.get('sent', 0)}; pending={stats.get('pending', 0)}; "
        f"skipped_no_email={stats.get('skipped_no_email', 0)}; failed={stats.get('failed', 0)}"
    )
    save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY, result_summary)
    return True, "ran", stats


def _build_schedule_digest_rows(
    monthly_reminders: list[dict],
    store_events: list[dict],
    lookahead_days: int = 31,
) -> tuple[list[dict], list[dict]]:
    today = datetime.date.today()
    due_rows: list[dict] = []
    for reminder in monthly_reminders or []:
        if not bool(reminder.get("enabled")):
            continue
        due_date = _next_monthly_due_date(int(reminder.get("day_of_month") or 1), today)
        days_left = (due_date - today).days
        if 0 <= days_left <= lookahead_days:
            due_rows.append(
                {
                    "title": str(reminder.get("title") or "").strip(),
                    "due_date": due_date,
                    "days_left": days_left,
                    "notes": str(reminder.get("notes") or "").strip(),
                }
            )
    due_rows.sort(key=lambda row: (row.get("due_date"), str(row.get("title") or "").lower()))

    event_rows: list[dict] = []
    for event in store_events or []:
        event_date_text = str(event.get("event_date") or "").strip()
        try:
            event_date = datetime.datetime.strptime(event_date_text, "%Y-%m-%d").date()
        except Exception:
            continue
        days_left = (event_date - today).days
        if 0 <= days_left <= lookahead_days:
            event_rows.append(
                {
                    "title": str(event.get("title") or "").strip(),
                    "event_date": event_date,
                    "days_left": days_left,
                    "all_day": bool(event.get("all_day")),
                    "start_time": str(event.get("start_time") or "").strip(),
                    "location": str(event.get("location") or "").strip(),
                    "notes": str(event.get("notes") or "").strip(),
                }
            )
    event_rows.sort(
        key=lambda row: (
            row.get("event_date"),
            str(row.get("start_time") or "99:99"),
            str(row.get("title") or "").lower(),
        )
    )
    return due_rows, event_rows


def send_schedule_digest_email(
    smtp: dict,
    recipient_email: str,
    monthly_reminders: list[dict],
    store_events: list[dict],
    lookahead_days: int = 31,
) -> dict:
    recipient = parseaddr(str(recipient_email or "").strip())[1].strip()
    if not recipient:
        raise ValueError("Schedule digest recipient email is required.")

    due_rows, event_rows = _build_schedule_digest_rows(
        monthly_reminders,
        store_events,
        lookahead_days=lookahead_days,
    )

    today_txt = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"Liberty Smokes Schedule Digest ({today_txt})"
    body_lines = [
        f"Schedule digest generated on {today_txt}.",
        f"Window: next {lookahead_days} day(s).",
        "",
        "Monthly reminders due soon:",
    ]
    if due_rows:
        for row in due_rows:
            line = (
                f"- {row['title']} | due {row['due_date'].strftime('%Y-%m-%d')} "
                f"({int(row['days_left'])} day(s))"
            )
            if row.get("notes"):
                line += f" | notes: {row['notes']}"
            body_lines.append(line)
    else:
        body_lines.append("- None")

    body_lines.extend(["", "Upcoming store events:"])
    if event_rows:
        for row in event_rows:
            when_txt = row["event_date"].strftime("%Y-%m-%d")
            if not row.get("all_day") and row.get("start_time"):
                when_txt = f"{when_txt} at {_format_time_12h(str(row['start_time']))}"
            line = (
                f"- {row['title']} | {when_txt} "
                f"({int(row['days_left'])} day(s))"
            )
            if row.get("location"):
                line += f" | location: {row['location']}"
            if row.get("notes"):
                line += f" | notes: {row['notes']}"
            body_lines.append(line)
    else:
        body_lines.append("- None")

    send_email(
        smtp["host"],
        int(smtp["port"]),
        smtp.get("username", ""),
        smtp.get("password", ""),
        recipient,
        subject,
        "\n".join(body_lines),
        security=smtp.get("security", "SSL"),
        from_addr=smtp.get("from_addr", ""),
    )
    return {
        "sent": 1,
        "recipient": recipient,
        "due_count": len(due_rows),
        "events_count": len(event_rows),
    }


def maybe_run_automated_schedule_digest(
    pg: SyncPostgrestClient,
    smtp: dict,
    monthly_reminders: list[dict],
    store_events: list[dict],
) -> tuple[bool, str, dict]:
    enabled = _bool_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_ENABLED_KEY), False)
    if not enabled:
        return False, "disabled", {}

    recipient = parseaddr(str(get_setting(pg, SCHEDULE_EMAIL_TO_KEY) or "").strip())[1].strip()
    if not recipient:
        return False, "missing_recipient", {}

    interval_minutes = max(15, _int_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY), 1440))
    now = datetime.datetime.now()
    last_run_raw = str(get_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY) or "").strip()
    if last_run_raw:
        try:
            last_run = datetime.datetime.fromisoformat(last_run_raw)
            elapsed_sec = (now - last_run).total_seconds()
            if elapsed_sec < interval_minutes * 60:
                return False, "throttled", {"minutes_remaining": int(((interval_minutes * 60) - elapsed_sec + 59) // 60)}
        except Exception:
            pass

    stats = send_schedule_digest_email(
        smtp,
        recipient,
        monthly_reminders,
        store_events,
        lookahead_days=31,
    )
    save_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY, now.isoformat(timespec="seconds"))
    result_summary = (
        f"sent={stats.get('sent', 0)}; recipient={stats.get('recipient', '')}; "
        f"due={stats.get('due_count', 0)}; events={stats.get('events_count', 0)}"
    )
    save_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY, result_summary)
    return True, "ran", stats


# ── Page: Seats ────────────────────────────────────────────────────────────────

def render_seat_card(
    pg: SyncPostgrestClient,
    seat: dict,
    member_drink_limit: int = 3,
    non_member_limit: int = 1,
    members: list = None,
    monthly_drinks_by_member: dict | None = None,
    drink_catalog: list[dict] | None = None,
):
    seat_number = int(seat.get("seat_number", 0))
    customer_name = seat.get("customer_name") or ""
    drinks_consumed = int(seat.get("drinks_consumed") or 0)
    alcoholic_drinks = int(seat.get("alcoholic_drinks") or 0)
    non_alcoholic_drinks = int(seat.get("non_alcoholic_drinks") or 0)
    if (alcoholic_drinks + non_alcoholic_drinks) == 0 and drinks_consumed > 0:
        non_alcoholic_drinks = drinks_consumed
    drink_breakdown = _parse_drink_breakdown(seat.get("drink_breakdown"))

    total_drinks = alcoholic_drinks + non_alcoholic_drinks
    is_occupied = bool(seat.get("is_occupied"))

    with st.container(border=True):
        st.subheader(f"Seat {seat_number}")

        if not is_occupied:
            name_key = f"name_input_{seat_number}"
            if st.session_state.pop(f"clear_input_{seat_number}", False):
                st.session_state.pop(name_key, None)
                st.session_state.pop(f"seat_limit_{seat_number}", None)
            st.text_input("Customer name", key=name_key)
            can_check_in = bool(st.session_state.get(name_key, "").strip())
            if st.button("Check In", key=f"check_in_{seat_number}", disabled=not can_check_in):
                check_in(pg, seat_number, st.session_state[name_key])
                st.session_state[f"clear_input_{seat_number}"] = True
                # Pre-set limit for the incoming customer based on membership.
                incoming_name = st.session_state[name_key]
                st.session_state[f"seat_limit_{seat_number}"] = (
                    member_drink_limit
                    if is_member(incoming_name, members or [])
                    else non_member_limit
                )
                st.rerun()
        else:
            seat_member = find_member_by_customer_name(customer_name, members or [])
            seat_is_member = seat_member is not None
            default_limit = member_drink_limit if seat_is_member else non_member_limit
            limit_key = f"seat_limit_{seat_number}"
            if limit_key not in st.session_state:
                st.session_state[limit_key] = default_limit

            badge = "Member" if seat_is_member else "Non-member"
            st.write(f"Name: **{customer_name}** ({badge})")
            st.write(f"Alcoholic: **{alcoholic_drinks}**")
            st.write(f"Non-alcoholic: **{non_alcoholic_drinks}**")
            st.write(f"Total drinks: **{total_drinks}**")

            catalog = drink_catalog or []
            cost_by_name = {
                str(item.get("name")): float(item.get("cost") or 0.0)
                for item in catalog
                if str(item.get("name") or "").strip()
            }
            seat_total_cost = 0.0
            for drink_name, qty in drink_breakdown.items():
                seat_total_cost += float(cost_by_name.get(drink_name, 0.0)) * int(qty)

            if drink_breakdown:
                breakdown_rows = []
                for name, qty in sorted(drink_breakdown.items(), key=lambda x: x[1], reverse=True):
                    unit_cost = float(cost_by_name.get(name, 0.0))
                    breakdown_rows.append(
                        {
                            "Drink": name,
                            "Qty": int(qty),
                            "Unit Cost": f"${unit_cost:,.2f}" if unit_cost > 0 else "",
                            "Total Cost": f"${(unit_cost * int(qty)):,.2f}" if unit_cost > 0 else "",
                        }
                    )
                st.dataframe(breakdown_rows, width="stretch", hide_index=True)
                st.caption(f"Estimated seat drink cost: ${seat_total_cost:,.2f}")

            if seat_member is not None:
                stats = (monthly_drinks_by_member or {}).get(str(seat_member.get("id")), {})
                m_alc = int(stats.get("alcoholic_drinks") or 0)
                m_non_alc = int(stats.get("non_alcoholic_drinks") or 0)
                m_total = int(stats.get("total_drinks") or (m_alc + m_non_alc))
                st.caption(
                    f"Member this month: {m_alc} alcoholic, {m_non_alc} non-alcoholic, {m_total} total"
                )

            effective_limit = st.number_input(
                "Seat limit",
                min_value=0,
                max_value=20,
                step=1,
                key=limit_key,
                help=f"Default for {badge}: {default_limit}. Adjust here to override.",
            )

            plus_disabled = total_drinks >= effective_limit

            if catalog:
                option_labels = [
                    f"{item.get('name', '')} ({'Alcoholic' if item.get('category') == 'alcoholic' else 'Non-Alcoholic'}) - ${float(item.get('cost') or 0.0):,.2f}"
                    for item in catalog
                ]
                option_to_item = {
                    option_labels[idx]: catalog[idx] for idx in range(len(option_labels))
                }
                selected_drink_label = st.selectbox(
                    "Drink",
                    option_labels,
                    key=f"seat_drink_pick_{seat_number}",
                )
                if st.button("Add Selected Drink", key=f"add_selected_drink_{seat_number}", disabled=plus_disabled):
                    selected_drink = option_to_item[selected_drink_label]
                    selected_name = str(selected_drink.get("name") or "").strip()
                    selected_type = str(selected_drink.get("category") or "non_alcoholic").strip().lower()
                    add_named_drink(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "alcoholic" if selected_type == "alcoholic" else "non_alcoholic",
                        selected_name,
                        drink_breakdown,
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(
                            pg,
                            seat_member.get("id"),
                            "alcoholic" if selected_type == "alcoholic" else "non_alcoholic",
                            1,
                        )
                    st.rerun()
            else:
                st.caption("No drinks configured yet. Add drinks and costs in Settings -> Drink Catalog.")
                c1, c2 = st.columns(2)
                if c1.button("+ Alcoholic", key=f"plus_alc_{seat_number}", disabled=plus_disabled):
                    add_drink_type(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "alcoholic",
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(pg, seat_member.get("id"), "alcoholic", 1)
                    st.rerun()

                if c2.button("+ Non-Alcoholic", key=f"plus_nonalc_{seat_number}", disabled=plus_disabled):
                    add_drink_type(
                        pg,
                        seat_number,
                        alcoholic_drinks,
                        non_alcoholic_drinks,
                        "non_alcoholic",
                    )
                    if seat_member is not None:
                        increment_member_monthly_drinks(pg, seat_member.get("id"), "non_alcoholic", 1)
                    st.rerun()

            if plus_disabled:
                st.caption(f"Limit reached ({effective_limit} total).")
            if st.button("Clear", key=f"clear_{seat_number}"):
                clear_seat(pg, seat_number)
                st.rerun()


def page_seats(pg: SyncPostgrestClient):
    st.header("Seat Check-In")
    member_drink_limit = st.session_state.get("drink_limit", 3)
    non_member_limit = st.session_state.get("non_member_limit", 1)

    try:
        seats = fetch_seats(pg)
    except Exception as exc:
        st.error(f"Failed to load seats: {exc}")
        return

    try:
        members = fetch_members(pg)
    except Exception:
        members = []

    try:
        drink_catalog = load_drink_catalog(pg)
    except Exception:
        drink_catalog = []

    current_month_map = {}
    month_rows = fetch_member_monthly_drinks(pg, month_start=month_start_for())
    for row in month_rows:
        current_month_map[str(row.get("member_id"))] = row

    next_seat = (max(int(s["seat_number"]) for s in seats) + 1) if seats else 1
    if st.button(f"+ Add Seat {next_seat}"):
        try:
            add_seat(pg, next_seat)
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to add seat: {exc}")

    if not seats:
        st.info("No seat rows found in table 'seats'.")
        return

    columns = st.columns(4)
    for idx, seat in enumerate(seats):
        with columns[idx % 4]:
            render_seat_card(
                pg,
                seat,
                member_drink_limit,
                non_member_limit,
                members,
                current_month_map,
                drink_catalog,
            )


def render_member_purchase_margins(
    sales: list[dict],
    loyalty_customers: list[dict],
    by_id: dict,
    current_month: str,
):
    st.subheader("Member Purchase Margins")
    member_sales = [s for s in sales if resolve_sale_member_id(s, loyalty_customers) not in {None, ""}]
    margin_month_options = []
    for sale in member_sales:
        mstart = sale_month_start(sale.get("created_at", ""))
        if mstart not in margin_month_options:
            margin_month_options.append(mstart)
    if current_month not in margin_month_options:
        margin_month_options.insert(0, current_month)

    selected_margin_month = st.selectbox(
        "Purchase history month",
        margin_month_options,
        format_func=lambda x: month_label(x),
        key="member_margin_overview_month",
    )

    margin_by_member = {}
    for sale in member_sales:
        sale_month = sale_month_start(sale.get("created_at", ""))
        if sale_month != selected_margin_month:
            continue

        mid = resolve_sale_member_id(sale, loyalty_customers)
        if mid in {None, ""}:
            continue
        regular_total = round(float(sale.get("regular_total", sale.get("subtotal", 0.0)) or 0.0), 2)
        discounted_total = round(float(sale.get("discounted_total", sale.get("total", 0.0)) or 0.0), 2)

        if sale.get("cost_total") is not None:
            cost_total = round(float(sale.get("cost_total") or 0.0), 2)
        else:
            cost_total = compute_sale_cost_from_items(sale.get("items", []))

        member_discount_total = round(
            float(sale.get("member_discount_total", sale.get("discount", 0.0)) or 0.0),
            2,
        )

        bucket = margin_by_member.setdefault(
            mid,
            {
                "regular_total": 0.0,
                "discounted_total": 0.0,
                "cost_total": 0.0,
                "discount_total": 0.0,
                "sales_count": 0,
            },
        )
        bucket["regular_total"] += regular_total
        bucket["discounted_total"] += discounted_total
        bucket["cost_total"] += cost_total
        bucket["discount_total"] += member_discount_total
        bucket["sales_count"] += 1

    margin_rows = []
    export_margin_rows = []
    total_regular = 0.0
    total_discounted = 0.0
    total_cost = 0.0
    total_member_discount = 0.0

    for mid, agg in margin_by_member.items():
        member = by_id.get(mid, {})
        member_name = f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", ") or f"Member ID {mid}"
        regular_total = round(float(agg.get("regular_total", 0.0)), 2)
        discounted_total = round(float(agg.get("discounted_total", 0.0)), 2)
        cost_total = round(float(agg.get("cost_total", 0.0)), 2)
        discount_total = round(float(agg.get("discount_total", 0.0)), 2)
        regular_margin = round(regular_total - cost_total, 2)
        discounted_margin = round(discounted_total - cost_total, 2)
        margin_delta = round(discounted_margin - regular_margin, 2)

        total_regular += regular_total
        total_discounted += discounted_total
        total_cost += cost_total
        total_member_discount += discount_total

        margin_rows.append(
            {
                "Member": member_name,
                "Sales": int(agg.get("sales_count", 0)),
                "Regular Price Sales": regular_total,
                "Member Discount Price Sales": discounted_total,
                "Member Discounts Given": discount_total,
                "Cost": cost_total,
                "Regular Margin": regular_margin,
                "Member Margin": discounted_margin,
                "Margin Delta": margin_delta,
            }
        )
        export_margin_rows.append(
            {
                "month_start": selected_margin_month,
                "month_label": month_label(selected_margin_month),
                "member_id": mid,
                "member_name": member_name,
                "sales_count": int(agg.get("sales_count", 0)),
                "regular_price_sales": regular_total,
                "member_discount_price_sales": discounted_total,
                "member_discounts_given": discount_total,
                "cost_total": cost_total,
                "regular_margin": regular_margin,
                "member_margin": discounted_margin,
                "margin_delta": margin_delta,
            }
        )

    mm1, mm2, mm3, mm4 = st.columns(4)
    mm1.metric("Regular Price Sales", f"${total_regular:,.2f}")
    mm2.metric("Member Discount Sales", f"${total_discounted:,.2f}")
    mm3.metric("Member Discounts", f"${total_member_discount:,.2f}")
    mm4.metric("Member Margin", f"${(total_discounted - total_cost):,.2f}")

    margin_export_text = ""
    if export_margin_rows:
        margin_buf = io.StringIO()
        margin_writer = csv.DictWriter(
            margin_buf,
            fieldnames=[
                "month_start",
                "month_label",
                "member_id",
                "member_name",
                "sales_count",
                "regular_price_sales",
                "member_discount_price_sales",
                "member_discounts_given",
                "cost_total",
                "regular_margin",
                "member_margin",
                "margin_delta",
            ],
        )
        margin_writer.writeheader()
        for row in sorted(export_margin_rows, key=lambda x: x["member_discount_price_sales"], reverse=True):
            margin_writer.writerow(row)
        margin_export_text = margin_buf.getvalue()

    st.download_button(
        "Download Member Margin CSV",
        data=margin_export_text,
        file_name=f"member_margins_{selected_margin_month}_{datetime.date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        disabled=not bool(margin_export_text),
        width="stretch",
    )

    if margin_rows:
        st.dataframe(
            sorted(margin_rows, key=lambda x: x["Member Discount Price Sales"], reverse=True),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No member purchase history for this month yet.")

    st.caption("All-month member margin trends")
    trend_mode = st.radio(
        "Trend metric mode",
        ["Dollars", "Percent"],
        horizontal=True,
        key="member_margin_trend_mode",
    )
    overall_by_month = {}
    per_member_by_month = {}
    for sale in member_sales:
        mstart = sale_month_start(sale.get("created_at", ""))
        regular_total = round(float(sale.get("regular_total", sale.get("subtotal", 0.0)) or 0.0), 2)
        discounted_total = round(float(sale.get("discounted_total", sale.get("total", 0.0)) or 0.0), 2)
        if sale.get("cost_total") is not None:
            cost_total = round(float(sale.get("cost_total") or 0.0), 2)
        else:
            cost_total = compute_sale_cost_from_items(sale.get("items", []))
        discount_total = round(float(sale.get("member_discount_total", sale.get("discount", 0.0)) or 0.0), 2)

        o_bucket = overall_by_month.setdefault(
            mstart,
            {
                "regular_total": 0.0,
                "discounted_total": 0.0,
                "cost_total": 0.0,
                "discount_total": 0.0,
            },
        )
        o_bucket["regular_total"] += regular_total
        o_bucket["discounted_total"] += discounted_total
        o_bucket["cost_total"] += cost_total
        o_bucket["discount_total"] += discount_total

        mid = resolve_sale_member_id(sale, loyalty_customers)
        if mid in {None, ""}:
            continue
        member_bucket = per_member_by_month.setdefault(mid, {})
        m_bucket = member_bucket.setdefault(
            mstart,
            {
                "regular_total": 0.0,
                "discounted_total": 0.0,
                "cost_total": 0.0,
                "discount_total": 0.0,
            },
        )
        m_bucket["regular_total"] += regular_total
        m_bucket["discounted_total"] += discounted_total
        m_bucket["cost_total"] += cost_total
        m_bucket["discount_total"] += discount_total

    if overall_by_month:
        sorted_months = sorted(overall_by_month.keys())
        overall_chart_rows = []
        for mstart in sorted_months:
            b = overall_by_month[mstart]
            regular_margin = round(float(b["regular_total"] - b["cost_total"]), 2)
            member_margin = round(float(b["discounted_total"] - b["cost_total"]), 2)
            discount_total = round(float(b["discount_total"]), 2)
            regular_margin_pct = round((regular_margin / float(b["regular_total"]) * 100.0), 2) if float(b["regular_total"]) > 0 else 0.0
            member_margin_pct = round((member_margin / float(b["discounted_total"]) * 100.0), 2) if float(b["discounted_total"]) > 0 else 0.0
            discounts_pct = round((discount_total / float(b["regular_total"]) * 100.0), 2) if float(b["regular_total"]) > 0 else 0.0
            overall_chart_rows.append(
                {
                    "Month": month_label(mstart),
                    "Regular Margin": regular_margin,
                    "Member Margin": member_margin,
                    "Member Discounts": discount_total,
                    "Regular Margin %": regular_margin_pct,
                    "Member Margin %": member_margin_pct,
                    "Member Discounts %": discounts_pct,
                }
            )
        st.write("Overall monthly trend")
        overall_y_cols = ["Regular Margin", "Member Margin", "Member Discounts"]
        if trend_mode == "Percent":
            overall_y_cols = ["Regular Margin %", "Member Margin %", "Member Discounts %"]
        st.line_chart(
            overall_chart_rows,
            x="Month",
            y=overall_y_cols,
            width="stretch",
        )

        member_label_to_id = {}
        for mid in per_member_by_month.keys():
            m = by_id.get(str(mid), {})
            label = f"{m.get('last_name', '')}, {m.get('first_name', '')}".strip(", ")
            if not label:
                label = f"Member ID {mid}"
            member_label_to_id[label] = str(mid)

        selected_member_trend_label = st.selectbox(
            "Per-member trend",
            sorted(member_label_to_id.keys()),
            key="member_margin_trend_pick",
        )
        selected_member_trend_id = member_label_to_id[selected_member_trend_label]
        selected_member_months = per_member_by_month.get(selected_member_trend_id, {})
        member_chart_rows = []
        for mstart in sorted(selected_member_months.keys()):
            b = selected_member_months[mstart]
            regular_margin = round(float(b["regular_total"] - b["cost_total"]), 2)
            member_margin = round(float(b["discounted_total"] - b["cost_total"]), 2)
            discount_total = round(float(b["discount_total"]), 2)
            regular_margin_pct = round((regular_margin / float(b["regular_total"]) * 100.0), 2) if float(b["regular_total"]) > 0 else 0.0
            member_margin_pct = round((member_margin / float(b["discounted_total"]) * 100.0), 2) if float(b["discounted_total"]) > 0 else 0.0
            discounts_pct = round((discount_total / float(b["regular_total"]) * 100.0), 2) if float(b["regular_total"]) > 0 else 0.0
            member_chart_rows.append(
                {
                    "Month": month_label(mstart),
                    "Regular Margin": regular_margin,
                    "Member Margin": member_margin,
                    "Member Discounts": discount_total,
                    "Regular Margin %": regular_margin_pct,
                    "Member Margin %": member_margin_pct,
                    "Member Discounts %": discounts_pct,
                }
            )
        if member_chart_rows:
            st.write(f"Trend for {selected_member_trend_label}")
            member_y_cols = ["Regular Margin", "Member Margin", "Member Discounts"]
            if trend_mode == "Percent":
                member_y_cols = ["Regular Margin %", "Member Margin %", "Member Discounts %"]
            st.line_chart(
                member_chart_rows,
                x="Month",
                y=member_y_cols,
                width="stretch",
            )
    else:
        st.info("No historical member sales yet for trend charts.")


# ── Page: Members ──────────────────────────────────────────────────────────────

def page_members(pg: SyncPostgrestClient):
    st.header("Member Management")

    pending_members_reset = st.session_state.pop("pending_members_widget_reset", None)
    if isinstance(pending_members_reset, dict):
        reset_widget_state(pending_members_reset)

    with st.expander("Import Members (CSV)", expanded=False):
        st.caption(
            "Upload a CSV export from your old app. Supported columns include "
            "first_name/last_name or name, plus email, phone, tier, status, locker, "
            "join_date, and next_billing_date."
        )
        upload = st.file_uploader("Members CSV", type=["csv"], key="members_csv_upload")
        if upload is not None and st.button("Import CSV"):
            try:
                csv_text = upload.getvalue().decode("utf-8")
                inserted, skipped = import_members_csv(pg, csv_text)
                st.success(f"Imported {inserted} member(s). Skipped {skipped} row(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Import failed: {exc}")

    with st.expander("Add New Member", expanded=False):
        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        fn = c1.text_input("First Name", key="m_fn")
        ln = c2.text_input("Last Name", key="m_ln")
        em = c3.text_input("Email", key="m_em")
        phone = c4.text_input("Phone", key="m_phone")
        tier = c5.selectbox("Tier", ["Monthly", "Annual"], key="m_tier")
        locker = c6.text_input("Locker", key="m_locker")
        months = c7.number_input("Pay Months", min_value=1, max_value=12, value=1, step=1, key="m_months")
        if st.button("Add Member"):
            if fn and ln:
                try:
                    added_member = add_member(pg, fn, ln, em, phone, tier, locker, int(months))
                    if em.strip():
                        try:
                            templates = load_email_templates(pg)
                            smtp = load_smtp_settings(pg)
                            if (
                                smtp.get("host")
                                and smtp.get("port")
                                and smtp.get("from_addr")
                                and smtp.get("password")
                            ):
                                send_email(
                                    smtp["host"],
                                    int(smtp["port"]),
                                    smtp.get("username", ""),
                                    smtp["password"],
                                    em.strip(),
                                    format_email_template(templates["welcome_subject"], added_member),
                                    format_email_template(templates["welcome_body"], added_member),
                                    security=smtp.get("security", "SSL"),
                                    from_addr=smtp.get("from_addr", ""),
                                )
                                st.info("Welcome email sent.")
                            else:
                                st.info("Member added. SMTP is not fully configured, so welcome email was skipped.")
                        except Exception as exc:
                            st.warning(f"Member added, but welcome email failed: {exc}")
                    st.success(f"Added {fn} {ln}.")
                    queue_widget_reset(
                        {
                            "m_fn": "",
                            "m_ln": "",
                            "m_em": "",
                            "m_phone": "",
                            "m_locker": "",
                            "m_tier": "Monthly",
                            "m_months": 1,
                        },
                        "pending_members_widget_reset",
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed: {exc}")
            else:
                st.warning("First and last name are required.")

    try:
        members = fetch_members(pg)
    except Exception as exc:
        st.error(f"Failed to load members: {exc}")
        return

    try:
        loyalty_customers = load_pos_loyalty_customers(pg)
    except Exception:
        loyalty_customers = []

    try:
        sales = load_pos_sales(pg)
    except Exception:
        sales = []

    sales, backfilled_count = backfill_pos_sales_member_ids(sales, loyalty_customers)
    if backfilled_count:
        try:
            save_pos_sales(pg, sales)
        except Exception:
            pass

    try:
        cigarpos_cfg = load_cigarpos_settings(pg)
    except Exception:
        cigarpos_cfg = {"base_url": ""}

    if not members:
        st.info("No members yet. Use 'Add New Member' above to create your first member.")
        if st.button("Create Sample Member"):
            try:
                add_member(
                    pg,
                    first_name="Sample",
                    last_name="Member",
                    email="",
                    phone="",
                    tier="Monthly",
                    locker="—",
                    months=1,
                )
                st.success("Sample member created.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to create sample member: {exc}")
        return

    today = datetime.date.today()
    rows = []
    for m in members:
        try:
            due = datetime.datetime.strptime(m["next_billing_date"], "%Y-%m-%d").date()
            days_left = (due - today).days
            due_label = f"{m['next_billing_date']} ({days_left:+d}d)"
        except Exception:
            due_label = m.get("next_billing_date", "")
        rows.append(
            {
                "ID": m["id"],
                "Name": f"{m['last_name']}, {m['first_name']}",
                "Email": m.get("email", ""),
                "Phone": m.get("phone", ""),
                "Tier": m.get("tier", ""),
                "Status": m.get("status", ""),
                "Locker": m.get("locker", ""),
                "Join Date": m.get("join_date", ""),
                "Next Bill": due_label,
            }
        )

    st.dataframe(rows, width="stretch", hide_index=True)

    st.divider()
    st.subheader("Monthly Gift Card Refill Tracker")
    st.caption("Track who received their monthly gift card so each member is only refilled once per month.")

    refill_month_options = []
    refill_rows_all = fetch_member_monthly_refills(pg)
    for row in refill_rows_all:
        mstart = str(row.get("month_start") or "").strip()
        if mstart and mstart not in refill_month_options:
            refill_month_options.append(mstart)
    current_refill_month = month_start_for()
    if current_refill_month not in refill_month_options:
        refill_month_options.insert(0, current_refill_month)

    refill_month = st.selectbox(
        "Refill month",
        refill_month_options,
        format_func=lambda x: month_label(x),
        key="gift_refill_month",
    )
    refill_amount = st.number_input(
        "Gift card amount",
        min_value=0.0,
        value=25.0,
        step=1.0,
        key="gift_refill_amount",
    )
    refill_note = st.text_input("Refill note (optional)", key="gift_refill_note")

    refill_rows = fetch_member_monthly_refills(pg, month_start=refill_month)
    refill_map = {str(r.get("member_id")): r for r in refill_rows}
    active_members = [m for m in members if str(m.get("status") or "").strip().lower() == "active"]

    st.caption("Gift card number manager")
    card_member_options = {
        member_locker_label(m): m
        for m in active_members
    }
    if card_member_options:
        selected_card_member_label = st.selectbox(
            "Member card profile",
            list(card_member_options.keys()),
            key="gift_card_member_pick",
        )
        selected_card_member = card_member_options[selected_card_member_label]
        selected_card_member_id = str(selected_card_member.get("id"))
        selected_card_number = str(selected_card_member.get("gift_card_number") or "")

        with st.form(key=f"gift_card_profile_form_{selected_card_member_id}"):
            card_number_value = st.text_input(
                "Gift card number",
                value=selected_card_number,
                key=f"gift_card_number_input_{selected_card_member_id}",
                help="Save or update the member's card number. Leave blank to clear it.",
            )
            save_card_profile = st.form_submit_button("Save Gift Card Number")

        if save_card_profile:
            ok, err = update_member_gift_card_number(
                pg,
                selected_card_member_id,
                card_number_value,
            )
            if ok:
                st.success("Gift card number saved.")
                st.rerun()
            else:
                st.error(
                    "Could not save gift card number. If this is the first time using this feature, "
                    "run supabase/create_members_table.sql in Supabase SQL editor. "
                    f"Details: {err}"
                )
    else:
        st.info("No active members available for gift card number management yet.")

    refilled_ids = [str(m.get("id")) for m in active_members if str(m.get("id")) in refill_map]
    pending_ids = [str(m.get("id")) for m in active_members if str(m.get("id")) not in refill_map]

    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("Active members", len(active_members))
    metric_2.metric("Refilled", len(refilled_ids))
    metric_3.metric("Pending", len(pending_ids))

    active_lookup = {str(m.get("id")): m for m in active_members}
    c_refill, c_unfill = st.columns(2)

    with c_refill:
        mark_ids = st.multiselect(
            "Mark as refilled",
            options=pending_ids,
            format_func=lambda mid: member_locker_label(active_lookup[mid]),
            key="gift_refill_mark_ids",
        )
        if st.button("Mark Selected Refilled", key="gift_refill_mark_btn"):
            if not mark_ids:
                st.warning("Select at least one member to mark as refilled.")
            else:
                marked = 0
                error_text = ""
                for mid in mark_ids:
                    ok, err = mark_member_monthly_refill(
                        pg,
                        mid,
                        month_start=refill_month,
                        amount=float(refill_amount),
                        notes=refill_note,
                    )
                    if ok:
                        marked += 1
                    else:
                        error_text = err or "Unknown error"
                        break
                if error_text:
                    st.error(
                        "Could not save refill tracker data. If this is the first time using this feature, "
                        "run supabase/create_member_monthly_refills_table.sql in Supabase SQL editor. "
                        f"Details: {error_text}"
                    )
                else:
                    st.success(f"Marked {marked} member(s) as refilled for {month_label(refill_month)}.")
                    queue_widget_reset(
                        {
                            "gift_refill_mark_ids": [],
                            "gift_refill_note": "",
                        },
                        "pending_members_widget_reset",
                    )
                    st.rerun()

        if st.button("Mark All Pending", key="gift_refill_mark_all_btn", disabled=not pending_ids):
            marked = 0
            error_text = ""
            for mid in pending_ids:
                ok, err = mark_member_monthly_refill(
                    pg,
                    mid,
                    month_start=refill_month,
                    amount=float(refill_amount),
                    notes=refill_note,
                )
                if ok:
                    marked += 1
                else:
                    error_text = err or "Unknown error"
                    break
            if error_text:
                st.error(
                    "Could not save refill tracker data. If this is the first time using this feature, "
                    "run supabase/create_member_monthly_refills_table.sql in Supabase SQL editor. "
                    f"Details: {error_text}"
                )
            else:
                st.success(f"Marked all pending members ({marked}) as refilled for {month_label(refill_month)}.")
                queue_widget_reset(
                    {
                        "gift_refill_note": "",
                    },
                    "pending_members_widget_reset",
                )
                st.rerun()

    with c_unfill:
        unmark_ids = st.multiselect(
            "Undo refill status",
            options=refilled_ids,
            format_func=lambda mid: member_locker_label(active_lookup[mid]),
            key="gift_refill_unmark_ids",
        )
        if st.button("Undo Selected", key="gift_refill_unmark_btn"):
            if not unmark_ids:
                st.warning("Select at least one member to undo.")
            else:
                undone = 0
                error_text = ""
                for mid in unmark_ids:
                    ok, err = unmark_member_monthly_refill(pg, mid, month_start=refill_month)
                    if ok:
                        undone += 1
                    else:
                        error_text = err or "Unknown error"
                        break
                if error_text:
                    st.error(f"Failed to undo refill status: {error_text}")
                else:
                    st.success(f"Undid refill status for {undone} member(s).")
                    queue_widget_reset(
                        {
                            "gift_refill_unmark_ids": [],
                        },
                        "pending_members_widget_reset",
                    )
                    st.rerun()

    tracker_rows = []
    for member in sorted(active_members, key=_locker_sort_key):
        member_id = str(member.get("id"))
        refill = refill_map.get(member_id)
        tracker_rows.append(
            {
                "Member": f"{member.get('last_name', '')}, {member.get('first_name', '')}",
                "Locker": str(member.get("locker") or "").strip() or "—",
                "Gift Card #": str(member.get("gift_card_number") or ""),
                "Refilled": "Yes" if refill else "No",
                "Amount": float(refill.get("amount") or 0.0) if refill else 0.0,
                "Refilled At": str(refill.get("refilled_at") or "") if refill else "",
                "Note": str(refill.get("notes") or "") if refill else "",
            }
        )

    st.dataframe(tracker_rows, width="stretch", hide_index=True)

    st.divider()
    st.subheader("Mass Email Members")

    members_with_email = [m for m in members if str(m.get("email") or "").strip()]
    if not members_with_email:
        st.info("No members with email addresses are available yet.")
    else:
        try:
            smtp = load_smtp_settings(pg)
        except Exception:
            smtp = {
                "host": "",
                "port": 0,
                "security": "SSL",
                "username": "",
                "password": "",
                "from_addr": "",
            }

        if not smtp["host"] or not smtp["port"] or not smtp["from_addr"] or not smtp["password"]:
            st.info("Configure SMTP settings in Settings before sending mass email.")
        else:
            st.caption(
                "Placeholders available: {first_name}, {last_name}, {full_name}, {tier}, "
                "{next_billing_date}, {join_date}, {status}, {locker}, {email}, {phone}"
            )

            recipient_mode = st.radio(
                "Recipients",
                ["All members with email", "Select members"],
                horizontal=True,
                key="mass_email_recipient_mode",
            )

            recipient_pool = members_with_email
            if recipient_mode == "Select members":
                recipient_lookup = {str(m["id"]): m for m in members_with_email}
                selected_ids = st.multiselect(
                    "Choose members",
                    options=list(recipient_lookup.keys()),
                    format_func=lambda mid: member_email_label(recipient_lookup[mid]),
                    key="mass_email_selected_ids",
                )
                recipient_pool = [recipient_lookup[mid] for mid in selected_ids]

            subject = st.text_input("Subject", key="mass_email_subject")
            body = st.text_area("Message", height=180, key="mass_email_body")

            send_clicked = st.button("Send Mass Email", type="primary")
            if send_clicked:
                if not subject.strip() or not body.strip():
                    st.warning("Subject and message are required.")
                elif not recipient_pool:
                    st.warning("Select at least one recipient.")
                else:
                    sent_count, failures = send_mass_member_email(
                        smtp,
                        recipient_pool,
                        subject,
                        body,
                    )
                    if sent_count:
                        st.success(f"Sent {sent_count} email(s).")
                    if failures:
                        failure_text = ", ".join(f"{label}: {error}" for label, error in failures[:3])
                        if len(failures) > 3:
                            failure_text += f" (+{len(failures) - 3} more)"
                        st.error(f"Some emails failed: {failure_text}")
                    if sent_count and not failures:
                        queue_widget_reset(
                            {
                                "mass_email_subject": "",
                                "mass_email_body": "",
                                "mass_email_selected_ids": [],
                            },
                            "pending_members_widget_reset",
                        )
                        st.rerun()

    st.divider()
    st.subheader("Mass Text Members")

    try:
        sms_cfg = load_sms_settings(pg)
    except Exception:
        sms_cfg = {
            "provider": "twilio",
            "account_sid": "",
            "auth_token": "",
            "from_number": "",
            "default_country_code": "+1",
        }

    members_with_phone = []
    for member in members:
        sms_to = _phone_to_e164(member.get("phone", ""), sms_cfg.get("default_country_code", "+1"))
        if sms_to:
            row = dict(member)
            row["_sms_to"] = sms_to
            members_with_phone.append(row)

    if not members_with_phone:
        st.info("No members with valid phone numbers are available yet.")
    else:
        st.caption(
            "Placeholders available: {first_name}, {last_name}, {full_name}, {tier}, "
            "{next_billing_date}, {join_date}, {status}, {locker}, {email}, {phone}"
        )

        text_recipient_mode = st.radio(
            "Text recipients",
            ["All members with valid phones", "Select members"],
            horizontal=True,
            key="mass_text_recipient_mode",
        )

        text_recipient_pool = members_with_phone
        if text_recipient_mode == "Select members":
            text_lookup = {str(m["id"]): m for m in members_with_phone}
            selected_text_ids = st.multiselect(
                "Choose text recipients",
                options=list(text_lookup.keys()),
                format_func=lambda mid: member_text_label(text_lookup[mid], text_lookup[mid].get("_sms_to", "")),
                key="mass_text_selected_ids",
            )
            text_recipient_pool = [text_lookup[mid] for mid in selected_text_ids]

        text_body = st.text_area(
            "Text message",
            key="mass_text_body",
            height=140,
            help="Use placeholders to personalize outgoing Twilio text messages.",
        )
        st.caption(f"Estimated recipients: {len(text_recipient_pool)}")

        twilio_ready = bool(sms_cfg.get("account_sid") and sms_cfg.get("auth_token") and sms_cfg.get("from_number"))
        if not twilio_ready:
            st.info("Twilio SMS config is optional. Set it in Settings only if you want one-click sending from this app.")

        if st.button("Send Mass Text", type="primary", disabled=not twilio_ready):
            if not text_body.strip():
                st.warning("Message is required.")
            elif not text_recipient_pool:
                st.warning("Select at least one recipient.")
            else:
                sent_count, failures = send_mass_member_text(sms_cfg, text_recipient_pool, text_body)
                if sent_count:
                    st.success(f"Sent {sent_count} text message(s).")
                if failures:
                    failure_text = ", ".join(f"{label}: {error}" for label, error in failures[:3])
                    if len(failures) > 3:
                        failure_text += f" (+{len(failures) - 3} more)"
                    st.error(f"Some texts failed: {failure_text}")
                if sent_count and not failures:
                    queue_widget_reset(
                        {
                            "mass_text_body": "",
                            "mass_text_selected_ids": [],
                        },
                        "pending_members_widget_reset",
                    )
                    st.rerun()

    st.divider()
    st.subheader("Drink Tracker")
    month_options = []
    month_rows_all = fetch_member_monthly_drinks(pg)
    for r in month_rows_all:
        mstart = str(r.get("month_start") or "").strip()
        if mstart and mstart not in month_options:
            month_options.append(mstart)
    current_month = month_start_for()
    if current_month not in month_options:
        month_options.insert(0, current_month)
    month_labels = {m: month_label(m) for m in month_options}
    selected_month = st.selectbox(
        "Overview month",
        month_options,
        format_func=lambda x: month_labels.get(x, x),
        key="member_drink_overview_month",
    )

    selected_month_rows = fetch_member_monthly_drinks(pg, month_start=selected_month)
    by_id = {str(m.get("id")): m for m in members}
    overview_rows = []
    export_month_rows = []
    total_alc = 0
    total_non_alc = 0
    total_drinks = 0
    for r in selected_month_rows:
        mid = str(r.get("member_id"))
        member = by_id.get(mid, {})
        alc = int(r.get("alcoholic_drinks") or 0)
        non_alc = int(r.get("non_alcoholic_drinks") or 0)
        tot = int(r.get("total_drinks") or (alc + non_alc))
        total_alc += alc
        total_non_alc += non_alc
        total_drinks += tot
        overview_rows.append(
            {
                "Member": f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", "),
                "Alcoholic": alc,
                "Non-Alcoholic": non_alc,
                "Total": tot,
            }
        )
        export_month_rows.append(
            {
                "month_start": selected_month,
                "month_label": month_labels.get(selected_month, selected_month),
                "member_id": mid,
                "member_name": f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", "),
                "alcoholic_drinks": alc,
                "non_alcoholic_drinks": non_alc,
                "total_drinks": tot,
            }
        )

    k1, k2, k3 = st.columns(3)
    k1.metric(f"Alcoholic ({month_labels.get(selected_month, selected_month)})", total_alc)
    k2.metric(f"Non-Alcoholic ({month_labels.get(selected_month, selected_month)})", total_non_alc)
    k3.metric(f"Total ({month_labels.get(selected_month, selected_month)})", total_drinks)

    month_download_name = (
        f"member_drinks_{selected_month}_{datetime.date.today().strftime('%Y%m%d')}.csv"
    )
    month_export_text = ""
    if export_month_rows:
        month_export_buf = io.StringIO()
        month_writer = csv.DictWriter(
            month_export_buf,
            fieldnames=[
                "month_start",
                "month_label",
                "member_id",
                "member_name",
                "alcoholic_drinks",
                "non_alcoholic_drinks",
                "total_drinks",
            ],
        )
        month_writer.writeheader()
        for row in sorted(export_month_rows, key=lambda x: x["total_drinks"], reverse=True):
            month_writer.writerow(row)
        month_export_text = month_export_buf.getvalue()

    all_export_rows = []
    for r in month_rows_all:
        mstart = str(r.get("month_start") or "")
        mid = str(r.get("member_id") or "")
        member = by_id.get(mid, {})
        alc = int(r.get("alcoholic_drinks") or 0)
        non_alc = int(r.get("non_alcoholic_drinks") or 0)
        tot = int(r.get("total_drinks") or (alc + non_alc))
        all_export_rows.append(
            {
                "month_start": mstart,
                "month_label": month_label(mstart),
                "member_id": mid,
                "member_name": f"{member.get('last_name', '')}, {member.get('first_name', '')}".strip(", "),
                "alcoholic_drinks": alc,
                "non_alcoholic_drinks": non_alc,
                "total_drinks": tot,
            }
        )

    all_download_name = f"member_drinks_all_months_{datetime.date.today().strftime('%Y%m%d')}.csv"
    all_export_text = ""
    if all_export_rows:
        all_export_buf = io.StringIO()
        all_writer = csv.DictWriter(
            all_export_buf,
            fieldnames=[
                "month_start",
                "month_label",
                "member_id",
                "member_name",
                "alcoholic_drinks",
                "non_alcoholic_drinks",
                "total_drinks",
            ],
        )
        all_writer.writeheader()
        for row in sorted(
            all_export_rows,
            key=lambda x: (x["month_start"], x["total_drinks"]),
            reverse=True,
        ):
            all_writer.writerow(row)
        all_export_text = all_export_buf.getvalue()

    d1, d2 = st.columns(2)
    d1.download_button(
        "Download Selected Month CSV",
        data=month_export_text,
        file_name=month_download_name,
        mime="text/csv",
        disabled=not bool(month_export_text),
        width="stretch",
    )
    d2.download_button(
        "Download All Months CSV",
        data=all_export_text,
        file_name=all_download_name,
        mime="text/csv",
        disabled=not bool(all_export_text),
        width="stretch",
    )

    if overview_rows:
        st.dataframe(
            sorted(overview_rows, key=lambda x: x["Total"], reverse=True),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No drink history for this month yet.")

    st.divider()
    show_member_margin_section = _bool_setting(get_setting(pg, MEMBER_MARGIN_SECTION_KEY), True)
    if show_member_margin_section:
        render_member_purchase_margins(sales, loyalty_customers, by_id, current_month)
    else:
        st.subheader("Member Purchase Margins")
        st.caption("Hidden. Enable it in Settings -> Display.")

    st.divider()
    member_options = {
        f"{m['last_name']}, {m['first_name']} (ID {m['id']})": m for m in members
    }
    selected_label = st.selectbox("Select member for actions", list(member_options.keys()))
    selected = member_options[selected_label]

    selected_history = fetch_member_monthly_drinks(pg, member_id=selected["id"])
    if selected_history:
        latest_row = None
        for row in selected_history:
            if str(row.get("month_start")) == current_month:
                latest_row = row
                break
        if latest_row is None:
            latest_row = selected_history[0]

        s_alc = int(latest_row.get("alcoholic_drinks") or 0)
        s_non_alc = int(latest_row.get("non_alcoholic_drinks") or 0)
        s_total = int(latest_row.get("total_drinks") or (s_alc + s_non_alc))
        st.caption(
            f"{selected['first_name']} {selected['last_name']} in {month_label(str(latest_row.get('month_start')))}: "
            f"{s_alc} alcoholic, {s_non_alc} non-alcoholic, {s_total} total"
        )

    linked_sales = [
        sale
        for sale in sales
        if resolve_sale_member_id(sale, loyalty_customers) == str(selected["id"])
    ]
    if linked_sales:
        latest_sale_month = month_label(sale_month_start(linked_sales[0].get("created_at", "")))
        st.caption(
            f"Linked purchase history: {len(linked_sales)} sale(s) found, latest in {latest_sale_month}."
        )
        base_url = str(cigarpos_cfg.get("base_url") or "").strip()
        if base_url:
            st.markdown(f"CigarPOS portal: [{base_url}]({base_url})")
    else:
        st.info("No linked POS purchase history found for this member yet.")

    if "_member_edit_undo" not in st.session_state:
        try:
            st.session_state["_member_edit_undo"] = load_member_edit_undo(pg)
        except Exception:
            st.session_state["_member_edit_undo"] = []
    undo_stack = st.session_state["_member_edit_undo"]

    col_pay, col_edit, col_del = st.columns(3)

    with col_pay:
        pay_months = st.number_input(
            "Months to pay", min_value=1, max_value=12, value=1, step=1, key="pay_months"
        )
        if st.button("Process Payment", type="primary"):
            try:
                process_payment(
                    pg, selected["id"], selected["tier"],
                    selected["next_billing_date"], int(pay_months)
                )
                st.success("Payment processed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    with col_edit:
        with st.expander("Edit member"):
            mid = selected["id"]
            status_opts = ["Active", "Past Due", "Inactive", "Canceled"]
            e_fn = st.text_input("First Name", value=selected["first_name"], key=f"e_fn_{mid}")
            e_ln = st.text_input("Last Name", value=selected["last_name"], key=f"e_ln_{mid}")
            e_em = st.text_input("Email", value=selected.get("email", ""), key=f"e_em_{mid}")
            e_ph = st.text_input("Phone", value=selected.get("phone", ""), key=f"e_ph_{mid}")
            e_lk = st.text_input("Locker", value=selected.get("locker", ""), key=f"e_lk_{mid}")
            _cur_status = selected.get("status", "Active")
            e_st = st.selectbox(
                "Status", status_opts,
                index=status_opts.index(_cur_status) if _cur_status in status_opts else 0,
                key=f"e_st_{mid}",
            )
            if st.button("Save Changes", key=f"save_member_{mid}"):
                try:
                    prev_snapshot = {
                        "id": selected["id"],
                        "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "first_name": selected.get("first_name", ""),
                        "last_name": selected.get("last_name", ""),
                        "email": selected.get("email", ""),
                        "phone": selected.get("phone", ""),
                        "locker": selected.get("locker", ""),
                        "status": selected.get("status", "Active"),
                    }
                    update_member(pg, selected["id"], e_fn, e_ln, e_em, e_ph, e_lk, e_st)
                    undo_stack.append(prev_snapshot)
                    if len(undo_stack) > MAX_MEMBER_EDIT_UNDO:
                        del undo_stack[:-MAX_MEMBER_EDIT_UNDO]
                    save_member_edit_undo(pg, undo_stack)
                    clear_member_edit_widget_state(mid)
                    st.success("Updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed: {exc}")

            undo_index = None
            for idx in range(len(undo_stack) - 1, -1, -1):
                if undo_stack[idx].get("id") == mid:
                    undo_index = idx
                    break

            member_snaps = [s for s in undo_stack if s.get("id") == mid]
            restore_index = None
            if member_snaps:
                preview_rows = []
                for snap in reversed(member_snaps[-5:]):
                    preview_rows.append(
                        {
                            "Saved At": snap.get("saved_at", "(unknown)"),
                            "Name": f"{snap.get('last_name', '')}, {snap.get('first_name', '')}".strip(", "),
                            "Email": snap.get("email", ""),
                            "Phone": snap.get("phone", ""),
                            "Locker": snap.get("locker", ""),
                            "Status": snap.get("status", "Active"),
                        }
                    )
                st.caption("Recent undo snapshots (newest first)")
                st.dataframe(preview_rows, width="stretch", hide_index=True)

                snap_labels = []
                snap_map = {}
                for idx in range(len(undo_stack) - 1, -1, -1):
                    snap = undo_stack[idx]
                    if snap.get("id") != mid:
                        continue
                    label = (
                        f"{snap.get('saved_at', '(unknown)')} | "
                        f"{snap.get('last_name', '')}, {snap.get('first_name', '')} | "
                        f"Phone: {snap.get('phone', '') or '(blank)'} | "
                        f"Status: {snap.get('status', 'Active')}"
                    )
                    if label in snap_map:
                        label = f"{label} [#{idx}]"
                    snap_labels.append(label)
                    snap_map[label] = idx

                picked_label = st.selectbox(
                    "Snapshot to restore",
                    snap_labels,
                    key=f"restore_pick_{mid}",
                )
                restore_index = snap_map[picked_label]
                if st.button("Restore Selected Snapshot", key=f"restore_snap_{mid}"):
                    try:
                        snap = undo_stack[restore_index]
                        update_member(
                            pg,
                            snap["id"],
                            snap.get("first_name", ""),
                            snap.get("last_name", ""),
                            snap.get("email", ""),
                            snap.get("phone", ""),
                            snap.get("locker", ""),
                            snap.get("status", "Active"),
                        )
                        undo_stack.pop(restore_index)
                        save_member_edit_undo(pg, undo_stack)
                        clear_member_edit_widget_state(mid)
                        st.success("Selected snapshot restored.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Restore failed: {exc}")

            if undo_index is not None:
                st.caption("Undo will restore the most recent saved values for this member.")
            if st.button(
                "Undo Last Edit",
                key=f"undo_member_{mid}",
                disabled=undo_index is None,
            ):
                try:
                    snap = undo_stack[undo_index]
                    update_member(
                        pg,
                        snap["id"],
                        snap.get("first_name", ""),
                        snap.get("last_name", ""),
                        snap.get("email", ""),
                        snap.get("phone", ""),
                        snap.get("locker", ""),
                        snap.get("status", "Active"),
                    )
                    undo_stack.pop(undo_index)
                    save_member_edit_undo(pg, undo_stack)
                    clear_member_edit_widget_state(mid)
                    st.success("Last edit undone.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Undo failed: {exc}")

    with col_del:
        st.write("")
        st.write("")
        if st.button("Remove Member", type="secondary"):
            if st.session_state.get("confirm_delete") == selected["id"]:
                try:
                    delete_member(pg, selected["id"])
                    st.session_state.pop("confirm_delete", None)
                    st.success("Removed.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed: {exc}")
            else:
                st.session_state["confirm_delete"] = selected["id"]
                st.warning("Click Remove again to confirm.")

    st.divider()
    st.subheader("Email Reminders")

    try:
        smtp = load_smtp_settings(pg)
        templates = load_email_templates(pg)
    except Exception:
        smtp = {
            "host": "",
            "port": 0,
            "security": "SSL",
            "username": "",
            "password": "",
            "from_addr": "",
        }
        templates = EMAIL_TEMPLATE_DEFAULTS

    if not smtp["host"] or not smtp["port"] or not smtp["from_addr"] or not smtp["password"]:
        st.info("Configure SMTP settings in Settings before scanning.")
    else:
        auto_ran, auto_state, auto_stats = maybe_run_automated_member_reminders(
            pg,
            smtp,
            templates,
            members,
        )
        auto_enabled = _bool_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_ENABLED_KEY), False)
        auto_interval = max(5, _int_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY), 60))
        if auto_enabled:
            if auto_ran:
                if int(auto_stats.get("sent", 0)) > 0:
                    st.success(
                        f"Automated reminders sent {int(auto_stats.get('sent', 0))} email(s). "
                        f"Skipped: {int(auto_stats.get('skipped_no_email', 0))}, Failed: {int(auto_stats.get('failed', 0))}."
                    )
                elif int(auto_stats.get("pending", 0)) == 0:
                    st.caption("Automated reminders checked: no reminders were due.")
            elif auto_state == "throttled":
                mins_left = int(auto_stats.get("minutes_remaining", 0))
                st.caption(
                    f"Automated reminders are enabled (every {auto_interval} min). "
                    f"Next check in about {mins_left} min."
                )

        pending = get_pending_reminders(members, templates)
        deliverable_pending = [p for p in pending if parseaddr(str(p.get("email") or "").strip())[1].strip()]
        skipped_missing_email = len(pending) - len(deliverable_pending)

        if not deliverable_pending:
            st.success("No reminders due today.")
        else:
            st.write(f"**{len(deliverable_pending)} reminder(s) ready to send:**")
            if skipped_missing_email:
                st.caption(f"Skipped {skipped_missing_email} reminder(s) with no email address.")
            for i, p in enumerate(deliverable_pending):
                with st.expander(f"{p['subject']} → {p['email']}"):
                    subj = st.text_input("Subject", value=p["subject"], key=f"subj_{i}")
                    body = st.text_area("Body", value=p["body"], key=f"body_{i}")
                    c_send, c_skip = st.columns(2)
                    if c_send.button("Send", key=f"send_{i}"):
                        try:
                            send_email(
                                smtp["host"],
                                int(smtp["port"]),
                                smtp["username"],
                                smtp["password"],
                                p["email"],
                                subj,
                                body,
                                security=smtp["security"],
                                from_addr=smtp["from_addr"],
                            )
                            pg.from_("members").update({"last_reminder": p["target"]}).eq(
                                "id", p["id"]
                            ).execute()
                            st.success("Sent.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Send failed: {exc}")
                    if c_skip.button("Skip", key=f"skip_{i}"):
                        st.rerun()


# ── Page: Sales Ledger ────────────────────────────────────────────────────────

def page_sales_ledger(pg: SyncPostgrestClient):
    st.header("Daily Sales Ledger")
    st.caption(
        "Track each day's cash sales, credit sales, cash taken out, and final cash deposit in one place."
    )

    pending_ledger_reset = st.session_state.pop("_sales_ledger_pending_reset", None)
    if isinstance(pending_ledger_reset, dict):
        reset_widget_state(pending_ledger_reset)

    pending_deduction_reset = st.session_state.pop("_sales_ledger_pending_deduction_reset", None)
    if isinstance(pending_deduction_reset, dict):
        reset_widget_state(pending_deduction_reset)

    pending_edit_date = st.session_state.pop("_sales_ledger_edit_date", None)
    if pending_edit_date:
        try:
            st.session_state["sales_ledger_entry_date"] = datetime.datetime.strptime(
                str(pending_edit_date), "%Y-%m-%d"
            ).date()
        except Exception:
            pass

    try:
        ledger_rows = fetch_daily_sales_ledger(pg)
    except Exception as exc:
        st.error(f"Failed to load ledger: {exc}")
        return

    deduction_rows = fetch_daily_sales_cash_deductions(pg)
    audit_rows = fetch_daily_sales_ledger_audit(pg, limit=200)
    merged_rows = merge_daily_sales_ledger_rows(ledger_rows, deduction_rows)

    row_by_date = {
        str(row.get("sale_date") or ""): row
        for row in merged_rows
        if str(row.get("sale_date") or "").strip()
    }

    ledger_month_options = []
    for row in merged_rows:
        month_start = ledger_month_start(str(row.get("sale_date") or ""))
        if month_start not in ledger_month_options:
            ledger_month_options.append(month_start)
    current_month = month_start_for()
    if current_month not in ledger_month_options:
        ledger_month_options.insert(0, current_month)

    selected_month = st.selectbox(
        "Ledger month",
        ledger_month_options,
        format_func=lambda value: month_label(value),
        key="sales_ledger_month",
    )

    selected_rows = [
        row for row in merged_rows if ledger_month_start(str(row.get("sale_date") or "")) == selected_month
    ]

    if not str(get_setting(pg, SALES_LEDGER_WEEKEND_START_KEY) or "").strip():
        save_setting(pg, SALES_LEDGER_WEEKEND_START_KEY, "Friday")
    if not str(get_setting(pg, SALES_LEDGER_WEEKEND_END_KEY) or "").strip():
        save_setting(pg, SALES_LEDGER_WEEKEND_END_KEY, "Sunday")

    weekend_start_day = _normalize_weekday_name(
        st.session_state.get(
            "sales_ledger_weekend_start_day",
            get_setting(pg, SALES_LEDGER_WEEKEND_START_KEY) or "Friday",
        )
    )
    weekend_end_day = _normalize_weekday_name(
        st.session_state.get(
            "sales_ledger_weekend_end_day",
            get_setting(pg, SALES_LEDGER_WEEKEND_END_KEY) or "Sunday",
        ),
        "Sunday",
    )
    weekend_day_indices = _weekday_range_indices(weekend_start_day, weekend_end_day)

    def _is_weekend_row(row: dict) -> bool:
        sale_date_text = str(row.get("sale_date") or "").strip()
        if not sale_date_text:
            return False
        try:
            sale_day_index = datetime.datetime.strptime(sale_date_text, "%Y-%m-%d").date().weekday()
        except Exception:
            return False
        return sale_day_index in weekend_day_indices

    selected_month_weekend_closed_register_withdrawn = round(
        sum(float(row.get("closed_register_withdrawn") or 0.0) for row in selected_rows if _is_weekend_row(row)),
        2,
    )

    total_cash_sales = round(sum(float(row.get("cash_sales") or 0.0) for row in selected_rows), 2)
    total_credit_sales = round(sum(float(row.get("credit_sales") or 0.0) for row in selected_rows), 2)
    total_cash_taken = round(sum(float(row.get("cash_taken") or 0.0) for row in selected_rows), 2)
    total_cash_deposit = round(sum(float(row.get("cash_deposit") or 0.0) for row in selected_rows), 2)
    total_closed_register_withdrawn = round(
        sum(float(row.get("closed_register_withdrawn") or 0.0) for row in selected_rows),
        2,
    )
    expected_deposit_total = round(total_cash_sales - total_cash_taken, 2)
    deposit_variance_total = round(total_cash_deposit - expected_deposit_total, 2)

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("Cash Sales", f"${total_cash_sales:,.2f}")
    m2.metric("Credit Sales", f"${total_credit_sales:,.2f}")
    m3.metric("Cash Taken", f"${total_cash_taken:,.2f}")
    m4.metric("Cash Deposited", f"${total_cash_deposit:,.2f}")
    m5.metric("Deposit Variance", f"${deposit_variance_total:,.2f}")
    m6.metric("Pulled From Closed", f"${total_closed_register_withdrawn:,.2f}")
    m7.metric("Weekend Pulls", f"${selected_month_weekend_closed_register_withdrawn:,.2f}")

    with st.expander("Weekend Register Settings", expanded=False):
        c_start, c_end = st.columns(2)
        weekend_start_day = c_start.selectbox(
            "Weekend start day",
            WEEKDAY_OPTIONS,
            index=WEEKDAY_OPTIONS.index(weekend_start_day),
            key="sales_ledger_weekend_start_day",
        )
        weekend_end_day = c_end.selectbox(
            "Weekend end day",
            WEEKDAY_OPTIONS,
            index=WEEKDAY_OPTIONS.index(weekend_end_day),
            key="sales_ledger_weekend_end_day",
        )
        save_setting(pg, SALES_LEDGER_WEEKEND_START_KEY, weekend_start_day)
        save_setting(pg, SALES_LEDGER_WEEKEND_END_KEY, weekend_end_day)
        st.caption(f"Weekend register is currently set to {_weekday_label(weekend_start_day, weekend_end_day)}.")

    weekend_rows = [row for row in selected_rows if _is_weekend_row(row)]

    weekend_cash_sales = round(sum(float(row.get("cash_sales") or 0.0) for row in weekend_rows), 2)
    weekend_credit_sales = round(sum(float(row.get("credit_sales") or 0.0) for row in weekend_rows), 2)
    weekend_cash_taken = round(sum(float(row.get("cash_taken") or 0.0) for row in weekend_rows), 2)
    weekend_cash_deposit = round(sum(float(row.get("cash_deposit") or 0.0) for row in weekend_rows), 2)
    weekend_closed_register_withdrawn = round(
        sum(float(row.get("closed_register_withdrawn") or 0.0) for row in weekend_rows),
        2,
    )
    weekend_expected_deposit = round(weekend_cash_sales - weekend_cash_taken, 2)
    weekend_deposit_variance = round(weekend_cash_deposit - weekend_expected_deposit, 2)

    st.subheader(f"Weekend Register ({_weekday_label(weekend_start_day, weekend_end_day)})")
    ww1, ww2, ww3, ww4, ww5 = st.columns(5)
    ww1.metric("Cash Sales", f"${weekend_cash_sales:,.2f}")
    ww2.metric("Credit Sales", f"${weekend_credit_sales:,.2f}")
    ww3.metric("Cash Taken", f"${weekend_cash_taken:,.2f}")
    ww4.metric("Cash Deposited", f"${weekend_cash_deposit:,.2f}")
    ww5.metric("Variance", f"${weekend_deposit_variance:,.2f}")
    st.caption(
        f"Weekend rows in {month_label(selected_month)}: {len(weekend_rows)} day(s) | Pulled from closed registers: ${weekend_closed_register_withdrawn:,.2f}"
    )
    if weekend_rows:
        weekend_display_rows = [build_daily_ledger_display_row(row) for row in sorted(weekend_rows, key=lambda value: str(value.get("sale_date") or ""), reverse=True)]
        st.dataframe(weekend_display_rows, width="stretch", hide_index=True)

        weekend_export_buffer = io.StringIO()
        weekend_writer = csv.DictWriter(
            weekend_export_buffer,
            fieldnames=[
                "Date",
                "Cash Sales",
                "Credit Sales",
                "Total Sales",
                "Cash Taken",
                "Deduction Count",
                "Expected Deposit",
                "Cash Deposited",
                "Closed Register Withdrawn",
                "Remaining Closed Cash",
                "Deposit Variance",
                "Notes",
                "Updated",
            ],
        )
        weekend_writer.writeheader()
        for row in weekend_display_rows:
            weekend_writer.writerow(row)

        weekend_print_totals = {
            "cash_sales": weekend_cash_sales,
            "credit_sales": weekend_credit_sales,
            "total_sales": weekend_cash_sales + weekend_credit_sales,
            "cash_taken": weekend_cash_taken,
            "cash_deposited": weekend_cash_deposit,
            "deposit_variance": weekend_deposit_variance,
        }
        weekend_print_html = build_monthly_ledger_print_html(
            f"Weekend Register - {_weekday_label(weekend_start_day, weekend_end_day)}",
            weekend_display_rows,
            weekend_print_totals,
        )

        wexp1, wexp2 = st.columns(2)
        wexp1.download_button(
            "Download Weekend CSV",
            data=weekend_export_buffer.getvalue(),
            file_name=f"daily_sales_weekend_{selected_month}_{datetime.date.today().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            width="stretch",
        )
        wexp2.download_button(
            "Download Weekend Printable HTML",
            data=weekend_print_html,
            file_name=f"daily_sales_weekend_print_{selected_month}_{datetime.date.today().strftime('%Y%m%d')}.html",
            mime="text/html",
            width="stretch",
        )
        if st.button("Open Weekend Print Preview", key=f"weekend_ledger_print_preview_{selected_month}"):
            components.html(weekend_print_html, height=900, scrolling=True)
    else:
        st.info(f"No ledger entries fall within the selected {_weekday_label(weekend_start_day, weekend_end_day)} range for this month.")

    entry_date = st.date_input(
        "Ledger date",
        value=datetime.date.today(),
        key="sales_ledger_entry_date",
    )
    entry_date_text = entry_date.strftime("%Y-%m-%d")
    existing_row = row_by_date.get(entry_date_text, {})
    register_day_options = [str(row.get("sale_date") or "").strip() for row in merged_rows if str(row.get("sale_date") or "").strip()]
    if entry_date_text not in register_day_options:
        register_day_options.insert(0, entry_date_text)
    selected_day_deductions = [
        row
        for row in deduction_rows
        if str(row.get("source_sale_date") or row.get("sale_date") or "").strip() == entry_date_text
    ]
    entered_today_from_other_days = [
        row
        for row in deduction_rows
        if str(row.get("withdrawal_date") or "").strip() == entry_date_text
        and str(row.get("source_sale_date") or row.get("sale_date") or "").strip() != entry_date_text
    ]
    selected_day_audit = [
        row for row in audit_rows if str(row.get("sale_date") or "").strip() == entry_date_text
    ]
    selected_day_cash_taken = round(
        sum(float(row.get("amount") or 0.0) for row in selected_day_deductions),
        2,
    )
    if not selected_day_deductions:
        selected_day_cash_taken = round(float(existing_row.get("cash_taken") or 0.0), 2)
    selected_day_closed_register_withdrawn = round(
        sum(
            float(row.get("amount") or 0.0)
            for row in selected_day_deductions
            if str(row.get("withdrawal_date") or "").strip() != entry_date_text
        ),
        2,
    )
    selected_day_remaining_closed_cash = round(
        float(existing_row.get("cash_deposit") or 0.0) - selected_day_closed_register_withdrawn,
        2,
    )

    rb1, rb2, rb3 = st.columns(3)
    rb1.metric("Closed Cash", f"${float(existing_row.get('cash_deposit') or 0.0):,.2f}")
    rb2.metric("Pulled Later", f"${selected_day_closed_register_withdrawn:,.2f}")
    rb3.metric("Remaining Closed Cash", f"${selected_day_remaining_closed_cash:,.2f}")
    st.caption(
        "If you closed a day and later pull money from that same register, log it as a deduction from that older register day."
    )

    with st.form(key=f"sales_ledger_entry_form_{entry_date_text}"):
        c1, c2 = st.columns(2)
        cash_sales_key = f"sales_ledger_cash_sales_{entry_date_text}"
        if cash_sales_key not in st.session_state:
            st.session_state[cash_sales_key] = float(existing_row.get("cash_sales") or 0.0)
        cash_sales = c1.number_input(
            "Cash sales",
            min_value=0.0,
            step=1.0,
            key=cash_sales_key,
        )
        credit_sales_key = f"sales_ledger_credit_sales_{entry_date_text}"
        if credit_sales_key not in st.session_state:
            st.session_state[credit_sales_key] = float(existing_row.get("credit_sales") or 0.0)
        credit_sales = c2.number_input(
            "Credit sales",
            min_value=0.0,
            step=1.0,
            key=credit_sales_key,
        )
        c3, c4 = st.columns(2)
        c3.metric("Cash taken from log", f"${selected_day_cash_taken:,.2f}")
        c3.caption(f"{len(selected_day_deductions)} deduction(s) recorded for {entry_date_text}.")
        cash_deposit_key = f"sales_ledger_cash_deposit_{entry_date_text}"
        if cash_deposit_key not in st.session_state:
            st.session_state[cash_deposit_key] = float(existing_row.get("cash_deposit") or 0.0)
        cash_deposit = c4.number_input(
            "Cash deposited",
            min_value=0.0,
            step=1.0,
            key=cash_deposit_key,
        )
        notes_key = f"sales_ledger_notes_{entry_date_text}"
        if notes_key not in st.session_state:
            st.session_state[notes_key] = str(existing_row.get("notes") or "")
        notes = st.text_area(
            "Notes",
            height=90,
            key=notes_key,
        )
        expected_deposit = round(float(cash_sales) - float(selected_day_cash_taken), 2)
        variance_preview = round(float(cash_deposit) - expected_deposit, 2)
        st.caption(
            f"Expected deposit for {entry_date_text}: ${expected_deposit:,.2f} | Variance: ${variance_preview:,.2f}"
        )
        save_clicked = st.form_submit_button("Save Ledger Entry", type="primary")

    with st.expander(f"Cash Deduction Log: {entry_date_text}", expanded=True):
        st.caption("Add each cash pull and pick which closed register day to deduct from.")
        with st.form(key=f"sales_ledger_deduction_form_{entry_date_text}"):
            d0, d1, d2 = st.columns([1.4, 1, 2])
            deduction_source_day = d0.selectbox(
                "Deduct from register day",
                register_day_options,
                index=register_day_options.index(entry_date_text) if entry_date_text in register_day_options else 0,
                format_func=lambda value: value,
                key=f"sales_ledger_deduction_source_day_{entry_date_text}",
            )
            deduction_amount = d1.number_input(
                "Deduction amount",
                min_value=0.0,
                value=0.0,
                step=1.0,
                key=f"sales_ledger_deduction_amount_{entry_date_text}",
            )
            deduction_note = d2.text_input(
                "Reason / note",
                key=f"sales_ledger_deduction_note_{entry_date_text}",
            )
            add_deduction_clicked = st.form_submit_button("Add Deduction")

        if add_deduction_clicked:
            ok, err = add_daily_sales_cash_deduction(
                pg,
                entry_date_text,
                deduction_amount,
                deduction_source_day,
                entry_date_text,
                deduction_note,
            )
            if ok:
                if str(deduction_source_day) == entry_date_text:
                    st.success(f"Added deduction to {entry_date_text}.")
                else:
                    st.success(
                        f"Added deduction to register day {deduction_source_day} (entered on {entry_date_text})."
                    )
                queue_widget_reset(
                    {
                        f"sales_ledger_deduction_source_day_{entry_date_text}": entry_date_text,
                        f"sales_ledger_deduction_amount_{entry_date_text}": 0.0,
                        f"sales_ledger_deduction_note_{entry_date_text}": "",
                    },
                    "_sales_ledger_pending_deduction_reset",
                )
                st.rerun()
            else:
                st.error(
                    "Could not save deduction log. Make sure the updated ledger SQL has been run in Supabase. "
                    f"Details: {err}"
                )

        if selected_day_deductions:
            st.caption(f"Total deducted for the day: ${selected_day_cash_taken:,.2f}")
            deduction_table_rows = []
            for row in selected_day_deductions:
                deduction_table_rows.append(
                    {
                        "Added": _format_datetime_12h(str(row.get("created_at") or "")),
                        "Entered On": str(row.get("withdrawal_date") or ""),
                        "Amount": round(float(row.get("amount") or 0.0), 2),
                        "Note": str(row.get("note") or ""),
                    }
                )
            st.dataframe(deduction_table_rows, width="stretch", hide_index=True)

            deduction_edit_options = {
                f"${float(row.get('amount') or 0.0):,.2f} | entered {str(row.get('withdrawal_date') or '')} | {str(row.get('note') or '(no note)')}": row
                for row in selected_day_deductions
            }
            edit_pick = st.selectbox(
                "Edit withdrawal entry",
                list(deduction_edit_options.keys()),
                key=f"sales_ledger_edit_pick_{entry_date_text}",
            )
            edit_row = deduction_edit_options[edit_pick]
            with st.form(key=f"sales_ledger_edit_deduction_form_{entry_date_text}_{edit_row.get('id')}"):
                e1, e2, e3 = st.columns([1.3, 1, 2])
                edit_source_day = e1.selectbox(
                    "Register day",
                    register_day_options,
                    index=(
                        register_day_options.index(
                            str(edit_row.get("source_sale_date") or edit_row.get("sale_date") or entry_date_text)
                        )
                        if str(edit_row.get("source_sale_date") or edit_row.get("sale_date") or entry_date_text) in register_day_options
                        else 0
                    ),
                    key=f"sales_ledger_edit_deduction_source_{edit_row.get('id')}",
                )
                edit_amount = e2.number_input(
                    "Amount",
                    min_value=0.0,
                    value=float(edit_row.get("amount") or 0.0),
                    step=1.0,
                    key=f"sales_ledger_edit_deduction_amount_{edit_row.get('id')}",
                )
                edit_note = e3.text_input(
                    "Note",
                    value=str(edit_row.get("note") or ""),
                    key=f"sales_ledger_edit_deduction_note_{edit_row.get('id')}",
                )
                edit_withdrawal_date = st.date_input(
                    "Entered on",
                    value=datetime.datetime.strptime(
                        str(edit_row.get("withdrawal_date") or entry_date_text), "%Y-%m-%d"
                    ).date(),
                    key=f"sales_ledger_edit_deduction_withdrawal_{edit_row.get('id')}",
                )
                save_deduction_edit = st.form_submit_button("Save Withdrawal Edit")

            if save_deduction_edit:
                ok, err = update_daily_sales_cash_deduction(
                    pg,
                    edit_row.get("id"),
                    edit_source_day,
                    edit_withdrawal_date.strftime("%Y-%m-%d"),
                    edit_amount,
                    edit_note,
                )
                if ok:
                    st.success("Withdrawal updated.")
                    st.rerun()
                else:
                    st.error(f"Failed to update withdrawal: {err}")

            st.caption("Delete a deduction if it was entered incorrectly, then re-add the correct amount.")
            for row in selected_day_deductions:
                delete_col, detail_col = st.columns([1, 4])
                delete_label = f"Delete ${float(row.get('amount') or 0.0):,.2f}"
                if delete_col.button(delete_label, key=f"sales_ledger_delete_deduction_{row.get('id')}"):
                    ok, err = delete_daily_sales_cash_deduction(pg, row.get("id"))
                    if ok:
                        st.success("Deduction deleted.")
                        st.rerun()
                    else:
                        st.error(f"Failed to delete deduction: {err}")
                detail_col.caption(
                    f"{_format_datetime_12h(str(row.get('created_at') or ''))} | entered {str(row.get('withdrawal_date') or '')} | {str(row.get('note') or '(no note)')}"
                )
        else:
            st.info("No deductions logged for this day yet.")

        if entered_today_from_other_days:
            st.caption("Withdrawals entered today from earlier closed register days")
            borrowed_rows = []
            for row in entered_today_from_other_days:
                borrowed_rows.append(
                    {
                        "From Register Day": str(row.get("source_sale_date") or row.get("sale_date") or ""),
                        "Amount": round(float(row.get("amount") or 0.0), 2),
                        "Note": str(row.get("note") or ""),
                        "Added": _format_datetime_12h(str(row.get("created_at") or "")),
                    }
                )
            st.dataframe(borrowed_rows, width="stretch", hide_index=True)

    action_col, info_col = st.columns([1, 2])
    delete_clicked = action_col.button(
        "Delete Selected Day",
        key=f"sales_ledger_delete_{entry_date_text}",
        disabled=entry_date_text not in row_by_date,
    )
    if entry_date_text in row_by_date:
        info_col.caption(f"Editing existing entry for {entry_date_text}.")
    else:
        info_col.caption(f"No saved entry yet for {entry_date_text}.")

    if save_clicked:
        ok, err = save_daily_sales_ledger_entry(
            pg,
            entry_date_text,
            cash_sales,
            credit_sales,
            selected_day_cash_taken,
            cash_deposit,
            notes,
        )
        if ok:
            queue_widget_reset(
                {
                    f"sales_ledger_cash_sales_{entry_date_text}": 0.0,
                    f"sales_ledger_credit_sales_{entry_date_text}": 0.0,
                    f"sales_ledger_cash_deposit_{entry_date_text}": 0.0,
                    f"sales_ledger_notes_{entry_date_text}": "",
                },
                "_sales_ledger_pending_reset",
            )
            st.success(f"Saved ledger entry for {entry_date_text}.")
            st.rerun()
        else:
            st.error(
                "Could not save ledger data. If this is the first time using this page, run "
                "supabase/create_daily_sales_ledger_table.sql in the Supabase SQL editor. "
                f"Details: {err}"
            )

    if delete_clicked:
        ok, err = delete_daily_sales_ledger_entry(pg, entry_date_text)
        if ok:
            st.success(f"Deleted ledger entry for {entry_date_text}.")
            st.rerun()
        else:
            st.error(f"Failed to delete ledger entry: {err}")

    st.divider()
    st.subheader(f"{month_label(selected_month)} Entries")

    display_rows = [build_daily_ledger_display_row(row) for row in selected_rows]
    if display_rows:
        edit_day_options = [row["Date"] for row in sorted(display_rows, key=lambda value: value["Date"], reverse=True)]
        pick_col, button_col = st.columns([2, 1])
        selected_edit_day = pick_col.selectbox(
            "Saved day",
            edit_day_options,
            key=f"sales_ledger_saved_day_pick_{selected_month}",
        )
        if button_col.button("Edit Selected Day", key=f"sales_ledger_edit_day_btn_{selected_month}"):
            st.session_state["_sales_ledger_edit_date"] = selected_edit_day
            st.rerun()

        st.dataframe(display_rows, width="stretch", hide_index=True)

        export_buffer = io.StringIO()
        writer = csv.DictWriter(
            export_buffer,
            fieldnames=[
                "Date",
                "Cash Sales",
                "Credit Sales",
                "Total Sales",
                "Cash Taken",
                "Deduction Count",
                "Expected Deposit",
                "Cash Deposited",
                "Closed Register Withdrawn",
                "Remaining Closed Cash",
                "Deposit Variance",
                "Notes",
                "Updated",
            ],
        )
        writer.writeheader()
        for row in sorted(display_rows, key=lambda value: value["Date"], reverse=True):
            writer.writerow(row)

        st.download_button(
            "Download Ledger CSV",
            data=export_buffer.getvalue(),
            file_name=f"daily_sales_ledger_{selected_month}_{datetime.date.today().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            width="stretch",
        )

        print_totals = {
            "cash_sales": total_cash_sales,
            "credit_sales": total_credit_sales,
            "total_sales": total_cash_sales + total_credit_sales,
            "cash_taken": total_cash_taken,
            "cash_deposited": total_cash_deposit,
            "deposit_variance": deposit_variance_total,
        }
        print_html = build_monthly_ledger_print_html(
            month_label(selected_month),
            display_rows,
            print_totals,
        )

        st.download_button(
            "Download Printable HTML",
            data=print_html,
            file_name=f"daily_sales_ledger_print_{selected_month}_{datetime.date.today().strftime('%Y%m%d')}.html",
            mime="text/html",
            width="stretch",
        )
        if st.button("Open Print Preview", key=f"ledger_print_preview_{selected_month}"):
            components.html(print_html, height=900, scrolling=True)
    else:
        st.info("No ledger entries saved for this month yet.")

    st.divider()
    st.subheader(f"Audit Trail: {entry_date_text}")
    cross_day_audit = [
        row for row in audit_rows if format_daily_sales_audit_summary(row).find(f"entered {entry_date_text}") >= 0
    ]
    shown_audit = selected_day_audit
    if cross_day_audit:
        shown_audit = selected_day_audit + [
            row for row in cross_day_audit if row.get("id") not in {r.get("id") for r in selected_day_audit}
        ]

    if shown_audit:
        audit_display_rows = []
        for row in shown_audit:
            audit_display_rows.append(
                {
                    "When": _format_datetime_12h(str(row.get("created_at") or "")),
                    "Action": str(row.get("action") or ""),
                    "Type": str(row.get("entity_type") or ""),
                    "Summary": format_daily_sales_audit_summary(row),
                }
            )
        st.dataframe(audit_display_rows, width="stretch", hide_index=True)
    else:
        st.info("No audit history for this day yet.")


# ── Page: POS ──────────────────────────────────────────────────────────────────

def page_schedule(pg: SyncPostgrestClient):
    st.header("Schedule")
    st.caption("Set recurring monthly reminders and track upcoming store events.")

    try:
        monthly_reminders = load_monthly_schedule_reminders(pg)
        store_events = load_store_events_schedule(pg)
    except Exception as exc:
        st.error(f"Failed to load schedule data: {exc}")
        return

    today = datetime.date.today()
    due_soon = [
        reminder
        for reminder in monthly_reminders
        if reminder.get("enabled")
        and 0 <= (_next_monthly_due_date(int(reminder.get("day_of_month") or 1), today) - today).days <= 7
    ]
    upcoming_events = [
        event
        for event in store_events
        if str(event.get("event_date") or "") >= today.strftime("%Y-%m-%d")
    ]

    m1, m2, m3 = st.columns(3)
    m1.metric("Monthly reminders", len(monthly_reminders))
    m2.metric("Due in next 7 days", len(due_soon))
    m3.metric("Upcoming events", len(upcoming_events))

    st.divider()
    st.subheader("Schedule Email Digest")
    try:
        smtp_cfg = load_smtp_settings(pg)
    except Exception:
        smtp_cfg = {
            "host": "",
            "port": 0,
            "security": "SSL",
            "username": "",
            "password": "",
            "from_addr": "",
        }

    schedule_to = str(get_setting(pg, SCHEDULE_EMAIL_TO_KEY) or "").strip()
    auto_enabled = _bool_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_ENABLED_KEY), False)
    auto_interval = max(15, _int_setting(get_setting(pg, SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY), 1440))
    auto_last_run = str(get_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY) or "").strip()
    auto_last_result = str(get_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY) or "").strip()

    smtp_ready = (
        bool(smtp_cfg.get("host"))
        and bool(smtp_cfg.get("port"))
        and bool(smtp_cfg.get("from_addr"))
        and bool(smtp_cfg.get("password"))
    )

    if smtp_ready:
        auto_ran, auto_state, auto_stats = maybe_run_automated_schedule_digest(
            pg,
            smtp_cfg,
            monthly_reminders,
            store_events,
        )
        if auto_ran:
            st.success(
                "Auto schedule digest sent "
                f"({int(auto_stats.get('due_count', 0))} due reminder(s), {int(auto_stats.get('events_count', 0))} event(s))."
            )
        elif auto_state == "throttled":
            mins_left = int(auto_stats.get("minutes_remaining", 0))
            st.caption(f"Auto digest throttled. Next run in about {mins_left} min.")
        elif auto_state == "missing_recipient" and auto_enabled:
            st.warning("Auto digest is enabled but recipient email is empty.")
    else:
        st.caption("Configure SMTP in Settings before using schedule digest emails.")

    s1, s2 = st.columns(2)
    digest_to_input = s1.text_input(
        "Digest recipient email",
        value=schedule_to or str(smtp_cfg.get("from_addr") or smtp_cfg.get("username") or ""),
        key="schedule_digest_to",
        help="Where schedule summary emails are delivered.",
    )
    digest_auto_enabled = s2.checkbox(
        "Enable auto digest",
        value=auto_enabled,
        key="schedule_digest_auto_enabled",
    )
    digest_interval = st.number_input(
        "Auto digest interval (minutes)",
        min_value=15,
        max_value=10080,
        value=int(auto_interval),
        step=15,
        key="schedule_digest_auto_interval",
        help="Use 1440 for daily digest delivery.",
    )

    b1, b2 = st.columns(2)
    if b1.button("Save Digest Settings", key="schedule_digest_save"):
        try:
            save_setting(pg, SCHEDULE_EMAIL_TO_KEY, digest_to_input.strip())
            save_setting(pg, SCHEDULE_EMAIL_AUTO_ENABLED_KEY, "1" if digest_auto_enabled else "0")
            save_setting(pg, SCHEDULE_EMAIL_AUTO_INTERVAL_MIN_KEY, str(int(digest_interval)))
            st.success("Schedule digest settings saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to save digest settings: {exc}")

    if b2.button("Send Digest Now", key="schedule_digest_send_now"):
        recipient_now = digest_to_input.strip() or str(smtp_cfg.get("from_addr") or "")
        if not smtp_ready:
            st.warning("Configure SMTP in Settings before sending digest emails.")
        elif not recipient_now:
            st.warning("Recipient email is required.")
        else:
            try:
                stats = send_schedule_digest_email(
                    smtp_cfg,
                    recipient_now,
                    monthly_reminders,
                    store_events,
                    lookahead_days=31,
                )
                now_txt = datetime.datetime.now().isoformat(timespec="seconds")
                save_setting(pg, SCHEDULE_EMAIL_AUTO_LAST_RUN_KEY, now_txt)
                save_setting(
                    pg,
                    SCHEDULE_EMAIL_AUTO_LAST_RESULT_KEY,
                    (
                        f"sent={stats.get('sent', 0)}; recipient={stats.get('recipient', '')}; "
                        f"due={stats.get('due_count', 0)}; events={stats.get('events_count', 0)}"
                    ),
                )
                st.success(
                    f"Digest sent to {stats.get('recipient', '')}. "
                    f"Included {int(stats.get('due_count', 0))} due reminder(s) and {int(stats.get('events_count', 0))} event(s)."
                )
            except Exception as exc:
                st.error(f"Failed to send digest: {exc}")

    st.caption(f"Last digest run: {_format_datetime_12h(auto_last_run) or 'never'}")
    if auto_last_result:
        st.caption(f"Last digest result: {auto_last_result}")

    tab_monthly, tab_events = st.tabs(["Monthly Reminders", "Store Events"])

    with tab_monthly:
        st.subheader("Monthly Filing Reminders")
        st.caption("Great for sales tax filings and other monthly tasks.")

        with st.form("schedule_add_monthly_reminder"):
            c1, c2, c3 = st.columns([2, 1, 1])
            reminder_title = c1.text_input("Reminder title", value="Monthly Sales Tax Filing")
            reminder_day = c2.number_input("Day of month", min_value=1, max_value=31, value=1, step=1)
            reminder_enabled = c3.checkbox("Enabled", value=True)
            reminder_notes = st.text_area("Notes (optional)", height=90)
            add_monthly_clicked = st.form_submit_button("Add Reminder", type="primary")

        if add_monthly_clicked:
            clean_title = reminder_title.strip()
            if not clean_title:
                st.warning("Reminder title is required.")
            else:
                new_row = {
                    "id": hashlib.sha1(
                        f"{clean_title.lower()}|{int(reminder_day)}|{datetime.datetime.now().isoformat()}".encode("utf-8")
                    ).hexdigest()[:12],
                    "title": clean_title,
                    "day_of_month": int(reminder_day),
                    "notes": reminder_notes.strip(),
                    "enabled": bool(reminder_enabled),
                }
                monthly_reminders.append(new_row)
                save_monthly_schedule_reminders(pg, monthly_reminders)
                st.success("Monthly reminder saved.")
                st.rerun()

        if not monthly_reminders:
            st.info("No monthly reminders yet.")
        else:
            for reminder in monthly_reminders:
                reminder_id = str(reminder.get("id") or "")
                next_due = _next_monthly_due_date(int(reminder.get("day_of_month") or 1), today)
                days_left = (next_due - today).days
                status_label = "Enabled" if reminder.get("enabled") else "Disabled"

                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(
                    f"**{reminder.get('title', '')}**  \n"
                    f"Day {int(reminder.get('day_of_month') or 1)} each month | {status_label} | "
                    f"Next due: {next_due.strftime('%Y-%m-%d')} ({days_left} day(s))"
                )
                if c2.button(
                    "Disable" if reminder.get("enabled") else "Enable",
                    key=f"schedule_toggle_monthly_{reminder_id}",
                ):
                    updated = []
                    for row in monthly_reminders:
                        if str(row.get("id") or "") == reminder_id:
                            changed = dict(row)
                            changed["enabled"] = not bool(row.get("enabled"))
                            updated.append(changed)
                        else:
                            updated.append(row)
                    save_monthly_schedule_reminders(pg, updated)
                    st.rerun()
                if c3.button("Delete", key=f"schedule_delete_monthly_{reminder_id}"):
                    updated = [
                        row for row in monthly_reminders if str(row.get("id") or "") != reminder_id
                    ]
                    save_monthly_schedule_reminders(pg, updated)
                    st.rerun()
                if str(reminder.get("notes") or "").strip():
                    st.caption(str(reminder.get("notes") or "").strip())
                st.divider()

    with tab_events:
        st.subheader("Store Events")
        st.caption("Track tastings, launches, and in-store events.")

        e1, e2, e3 = st.columns([2, 1, 1])
        event_title = e1.text_input("Event title", key="schedule_event_title")
        event_date = e2.date_input("Date", value=today, key="schedule_event_date")
        event_all_day = e3.checkbox("All day", value=True, key="schedule_event_all_day")
        if event_all_day:
            event_hour = 6
            event_minute = 0
            event_period = "PM"
            st.caption("All-day event: no start time required.")
        else:
            t1, t2, t3 = st.columns([1, 1, 1])
            event_hour = t1.selectbox("Hour", list(range(1, 13)), index=5, key="schedule_event_hour")
            event_minute = t2.selectbox(
                "Minute",
                list(range(0, 60)),
                index=0,
                format_func=lambda value: f"{value:02d}",
                key="schedule_event_minute",
            )
            event_period = t3.selectbox("AM/PM", ["AM", "PM"], index=1, key="schedule_event_period")
        event_location = st.text_input("Location (optional)", key="schedule_event_location")
        event_notes = st.text_area("Notes (optional)", height=100, key="schedule_event_notes")
        add_event_clicked = st.button("Add Event", type="primary", key="schedule_add_event_btn")

        if add_event_clicked:
            clean_title = event_title.strip()
            if not clean_title:
                st.warning("Event title is required.")
            else:
                event_date_text = event_date.strftime("%Y-%m-%d")
                start_time = "" if event_all_day else _time_parts_to_24h(event_hour, event_minute, event_period)
                new_event = {
                    "id": hashlib.sha1(
                        f"{clean_title.lower()}|{event_date_text}|{start_time}|{datetime.datetime.now().isoformat()}".encode("utf-8")
                    ).hexdigest()[:12],
                    "title": clean_title,
                    "event_date": event_date_text,
                    "all_day": bool(event_all_day),
                    "start_time": start_time,
                    "location": event_location.strip(),
                    "notes": event_notes.strip(),
                }
                store_events.append(new_event)
                save_store_events_schedule(pg, store_events)
                st.success("Event saved.")
                queue_widget_reset(
                    {
                        "schedule_event_title": "",
                        "schedule_event_date": today,
                        "schedule_event_all_day": True,
                        "schedule_event_hour": 6,
                        "schedule_event_minute": 0,
                        "schedule_event_period": "PM",
                        "schedule_event_location": "",
                        "schedule_event_notes": "",
                    },
                    GLOBAL_PENDING_WIDGET_RESET_KEY,
                )
                st.rerun()

        show_past = st.checkbox("Show past events", value=False, key="schedule_show_past_events")
        visible_events = []
        for event in store_events:
            event_date_text = str(event.get("event_date") or "")
            if show_past or event_date_text >= today.strftime("%Y-%m-%d"):
                visible_events.append(event)

        if not visible_events:
            st.info("No events to show.")
        else:
            for event in visible_events:
                event_id = str(event.get("id") or "")
                date_text = str(event.get("event_date") or "")
                when_text = date_text
                if not bool(event.get("all_day")) and str(event.get("start_time") or "").strip():
                    when_text = f"{date_text} at {_format_time_12h(str(event.get('start_time') or '').strip())}"

                c1, c2 = st.columns([4, 1])
                title = str(event.get("title") or "")
                location = str(event.get("location") or "").strip()
                if location:
                    c1.markdown(f"**{title}**  \n{when_text} | {location}")
                else:
                    c1.markdown(f"**{title}**  \n{when_text}")

                if c2.button("Delete", key=f"schedule_delete_event_{event_id}"):
                    updated = [
                        row for row in store_events if str(row.get("id") or "") != event_id
                    ]
                    save_store_events_schedule(pg, updated)
                    st.rerun()

                with st.expander("Edit event", expanded=False):
                    try:
                        default_edit_date = datetime.datetime.strptime(date_text, "%Y-%m-%d").date()
                    except Exception:
                        default_edit_date = today
                    edit_all_day_default = bool(event.get("all_day"))
                    default_hour, default_minute, default_period = _time_24h_to_parts(
                        str(event.get("start_time") or "18:00").strip() or "18:00",
                        default_hour=6,
                        default_minute=0,
                        default_period="PM",
                    )

                    ec1, ec2, ec3 = st.columns([2, 1, 1])
                    edit_title = ec1.text_input(
                        "Title",
                        value=title,
                        key=f"schedule_edit_title_{event_id}",
                    )
                    edit_date = ec2.date_input(
                        "Date",
                        value=default_edit_date,
                        key=f"schedule_edit_date_{event_id}",
                    )
                    edit_all_day = ec3.checkbox(
                        "All day",
                        value=edit_all_day_default,
                        key=f"schedule_edit_all_day_{event_id}",
                    )
                    if edit_all_day:
                        edit_hour = default_hour
                        edit_minute = default_minute
                        edit_period = default_period
                        st.caption("All-day event: no start time required.")
                    else:
                        et1, et2, et3 = st.columns([1, 1, 1])
                        edit_hour = et1.selectbox(
                            "Hour",
                            list(range(1, 13)),
                            index=max(0, min(11, default_hour - 1)),
                            key=f"schedule_edit_hour_{event_id}",
                        )
                        edit_minute = et2.selectbox(
                            "Minute",
                            list(range(0, 60)),
                            index=max(0, min(59, default_minute)),
                            format_func=lambda value: f"{value:02d}",
                            key=f"schedule_edit_minute_{event_id}",
                        )
                        edit_period = et3.selectbox(
                            "AM/PM",
                            ["AM", "PM"],
                            index=0 if default_period == "AM" else 1,
                            key=f"schedule_edit_period_{event_id}",
                        )
                    edit_location = st.text_input(
                        "Location",
                        value=location,
                        key=f"schedule_edit_location_{event_id}",
                    )
                    edit_notes = st.text_area(
                        "Notes",
                        value=str(event.get("notes") or ""),
                        height=100,
                        key=f"schedule_edit_notes_{event_id}",
                    )
                    if st.button("Save Changes", key=f"schedule_save_event_{event_id}"):
                        clean_edit_title = edit_title.strip()
                        if not clean_edit_title:
                            st.warning("Event title is required.")
                        else:
                            updated = []
                            for row in store_events:
                                if str(row.get("id") or "") == event_id:
                                    updated.append(
                                        {
                                            "id": event_id,
                                            "title": clean_edit_title,
                                            "event_date": edit_date.strftime("%Y-%m-%d"),
                                            "all_day": bool(edit_all_day),
                                            "start_time": "" if edit_all_day else _time_parts_to_24h(edit_hour, edit_minute, edit_period),
                                            "location": edit_location.strip(),
                                            "notes": edit_notes.strip(),
                                        }
                                    )
                                else:
                                    updated.append(row)
                            save_store_events_schedule(pg, updated)
                            st.success("Event updated.")
                            st.rerun()

                if str(event.get("notes") or "").strip():
                    st.caption(str(event.get("notes") or "").strip())
                st.divider()


# ── Page: POS ──────────────────────────────────────────────────────────────────

def page_pos(pg: SyncPostgrestClient):
    st.header("Cigar POS")

    pending_pos_reset = st.session_state.pop("pending_pos_widget_reset", None)
    if isinstance(pending_pos_reset, dict):
        reset_widget_state(pending_pos_reset)

    auto_ran, auto_msg = maybe_auto_sync_cigarpos(pg)
    if auto_ran:
        st.info(auto_msg)

    try:
        inventory = load_pos_inventory(pg)
    except Exception:
        inventory = []
    try:
        promotions = load_pos_promotions(pg)
    except Exception:
        promotions = []
    try:
        sales = load_pos_sales(pg)
    except Exception:
        sales = []
    try:
        customer_groups = load_pos_customer_groups(pg)
    except Exception:
        customer_groups = []
    try:
        loyalty_settings = load_pos_loyalty_settings(pg)
    except Exception:
        loyalty_settings = {
            "enabled": True,
            "earn_points_per_dollar": 1.0,
            "redeem_dollars_per_point": 0.01,
        }
    try:
        loyalty_points = load_pos_loyalty_points(pg)
    except Exception:
        loyalty_points = {}
    try:
        loyalty_customers = load_pos_loyalty_customers(pg)
    except Exception:
        loyalty_customers = []
    try:
        members = fetch_members(pg)
    except Exception:
        members = []

    loyalty_customers, loyalty_points, linked_count = reconcile_loyalty_contacts_with_members(
        loyalty_customers,
        members,
        loyalty_points,
    )
    if linked_count:
        save_pos_loyalty_customers(pg, loyalty_customers)
        save_pos_loyalty_points(pg, loyalty_points)

    sales, backfilled_count = backfill_pos_sales_member_ids(sales, loyalty_customers)
    if backfilled_count:
        save_pos_sales(pg, sales)

    cart = st.session_state.setdefault("pos_cart", [])

    last_sync = get_setting(pg, POS_LAST_SYNC_KEY)
    last_error = get_setting(pg, POS_LAST_SYNC_ERROR_KEY)
    sync_cfg = load_cigarpos_settings(pg)
    scan_channel = _sanitize_scan_channel(get_setting(pg, POS_SCAN_CHANNEL_KEY) or "main")

    with st.expander("Wireless UPC Scanner", expanded=True):
        st.caption("Use a phone on the Scanner page to push UPC/SKU scans into this POS cart.")
        c_sc1, c_sc2 = st.columns([2, 1])
        ch_value = c_sc1.text_input("Scanner Channel", value=scan_channel, key="pos_scan_channel_input")
        if c_sc2.button("Save Channel"):
            save_setting(pg, POS_SCAN_CHANNEL_KEY, _sanitize_scan_channel(ch_value))
            st.success("Scanner channel saved.")
            st.rerun()

        active_channel = _sanitize_scan_channel(ch_value)
        pending_count = len(load_scan_queue(pg, active_channel))
        st.caption(f"Active channel: {active_channel} | Pending scans: {pending_count}")

        ar1, ar2 = st.columns([2, 1])
        auto_scan = ar1.checkbox(
            "Live import scans (auto)",
            value=st.session_state.get("pos_auto_scan", True),
            key="pos_auto_scan",
        )
        auto_scan_sec = ar2.number_input(
            "Refresh (sec)",
            min_value=2,
            max_value=60,
            value=int(st.session_state.get("pos_auto_scan_sec", 5)),
            step=1,
            key="pos_auto_scan_sec",
        )
        if auto_scan:
            trigger_optional_autorefresh(int(auto_scan_sec) * 1000, key="pos_auto_scan_refresh")

        if auto_scan:
            added_auto, misses_auto = import_scans_to_cart(pg, active_channel, inventory, cart, limit=120)
            if added_auto:
                st.info(f"Auto-imported {added_auto} scan(s).")
            if misses_auto:
                st.warning(f"{len(misses_auto)} auto-import scan(s) had no inventory match.")

        if st.button("Import Pending Scans", key="pos_import_pending_scans"):
            added, misses = import_scans_to_cart(pg, active_channel, inventory, cart, limit=120)
            if not added and not misses:
                st.info("No pending scans.")
            else:
                if added:
                    st.success(f"Added {added} scanned item(s) to cart.")
                if misses:
                    sample = ", ".join(misses[:6])
                    st.warning(f"{len(misses)} scan(s) did not match inventory: {sample}")
                st.rerun()

    s1, s2, s3 = st.columns([2, 2, 1])
    s1.metric("Last Remote Sync", _format_datetime_12h(last_sync) or "Never")
    s2.metric("Auto Sync", "On" if sync_cfg.get("auto_sync") else "Off")
    if s3.button("Sync Now", key="pos_sync_now"):
        try:
            count, when = run_cigarpos_inventory_sync(pg, merge_mode=True)
            st.success(f"Synced {count} inventory item(s) from CigarPOS at {_format_datetime_12h(when)}.")
            st.rerun()
        except Exception as exc:
            save_setting(pg, POS_LAST_SYNC_ERROR_KEY, str(exc))
            st.error(f"Sync failed: {exc}")
    if last_error:
        st.warning(f"Last sync error: {last_error}")

    inventory_by_sku = {str(i.get("sku", "")).strip().lower(): i for i in inventory if i.get("sku")}

    with st.expander("Inventory", expanded=True):
        st.caption(
            "Upload CSV columns like: sku/stock code, name/item name, category, "
            "price/retail/selling price, stock/qty on hand, cost, taxable."
        )
        upload = st.file_uploader("Inventory CSV", type=["csv"], key="pos_inventory_csv")
        merge_mode = st.checkbox("Merge by SKU (unchecked = replace all)", value=True, key="pos_merge_inventory")
        if st.button("Import Inventory CSV") and upload is not None:
            try:
                parsed, skipped = parse_inventory_csv(upload.getvalue().decode("utf-8"))
                if merge_mode:
                    merged = {str(i.get("sku", "")).strip().lower(): i for i in inventory if i.get("sku")}
                    for row in parsed:
                        merged[str(row.get("sku", "")).strip().lower()] = row
                    inventory = list(merged.values())
                else:
                    inventory = parsed
                save_pos_inventory(pg, inventory)
                st.success(f"Imported {len(parsed)} item(s). Skipped {skipped} row(s).")
                st.rerun()
            except Exception as exc:
                st.error(f"Import failed: {exc}")

        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        sku = c1.text_input("SKU", key="pos_sku")
        name = c2.text_input("Name", key="pos_name")
        category = c3.text_input("Category", key="pos_category")
        barcode = c4.text_input("Barcode / UPC", key="pos_barcode")
        price = c5.number_input("Price", min_value=0.0, value=0.0, step=0.01, key="pos_price")
        stock = c6.number_input("Stock", min_value=0, value=0, step=1, key="pos_stock")
        cost = c7.number_input("Cost", min_value=0.0, value=0.0, step=0.01, key="pos_cost")
        taxable = st.checkbox("Taxable", value=False, key="pos_taxable")
        a1, a2 = st.columns(2)
        if a1.button("Add / Update Item"):
            if not sku.strip() or not name.strip():
                st.warning("SKU and Name are required.")
            else:
                inventory_by_sku[sku.strip().lower()] = {
                    "sku": sku.strip(),
                    "name": name.strip(),
                    "category": category.strip(),
                    "barcode": barcode.strip(),
                    "price": round(float(price), 2),
                    "cost": round(float(cost), 2),
                    "stock": int(stock),
                    "taxable": bool(taxable),
                }
                save_pos_inventory(pg, list(inventory_by_sku.values()))
                st.success("Item saved.")
                queue_widget_reset(
                    {
                        "pos_sku": "",
                        "pos_name": "",
                        "pos_category": "",
                        "pos_barcode": "",
                        "pos_price": 0.0,
                        "pos_stock": 0,
                        "pos_cost": 0.0,
                        "pos_taxable": False,
                    },
                    "pending_pos_widget_reset",
                )
                st.rerun()
        if a2.button("Clear Inventory"):
            save_pos_inventory(pg, [])
            st.session_state["pos_cart"] = []
            st.success("Inventory cleared.")
            st.rerun()

        if inventory:
            inv_rows = []
            for item in inventory:
                inv_rows.append(
                    {
                        "SKU": item.get("sku", ""),
                        "Barcode": item.get("barcode", ""),
                        "Name": item.get("name", ""),
                        "Category": item.get("category", ""),
                        "Price": float(item.get("price", 0.0)),
                        "Stock": int(item.get("stock", 0)),
                    }
                )
            st.dataframe(inv_rows, width="stretch", hide_index=True)
            total_stock_value = sum(float(i.get("price", 0.0)) * int(i.get("stock", 0)) for i in inventory)
            st.caption(f"Inventory items: {len(inventory)} | Estimated retail value: ${total_stock_value:,.2f}")
        else:
            st.info("No inventory loaded yet.")

    with st.expander("Promotions & Discounts", expanded=True):
        p1, p2, p3, p4, p5 = st.columns(5)
        promo_name = p1.text_input("Promo name", key="promo_name")
        promo_kind = p2.selectbox("Type", ["Percent", "Fixed"], key="promo_kind")
        promo_value = p3.number_input("Value", min_value=0.0, value=0.0, step=0.5, key="promo_value")
        promo_apply_to = p4.selectbox(
            "Applies to",
            ["All", "Members only", "Non-members only", "Tier", "SKU", "Category", "Customer Group"],
            key="promo_apply_to",
        )
        group_name_to_id = {
            str(g.get("name", "")).strip(): str(g.get("id", "")).strip()
            for g in customer_groups
            if g.get("name") and g.get("id")
        }
        if promo_apply_to == "Customer Group" and group_name_to_id:
            chosen_group_name = p5.selectbox("Target Group", list(group_name_to_id.keys()), key="promo_target_group")
            promo_target = group_name_to_id[chosen_group_name]
        else:
            promo_target = p5.text_input("Target (Tier/SKU/Category)", key="promo_target")
        active = st.checkbox("Active", value=True, key="promo_active")
        if st.button("Add Promotion"):
            if not promo_name.strip() or promo_value <= 0:
                st.warning("Promo name and value are required.")
            else:
                promotions.append(
                    {
                        "id": datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
                        "name": promo_name.strip(),
                        "kind": promo_kind,
                        "value": float(promo_value),
                        "apply_to": promo_apply_to,
                        "target": promo_target.strip(),
                        "active": bool(active),
                    }
                )
                save_pos_promotions(pg, promotions)
                st.success("Promotion added.")
                queue_widget_reset(
                    {
                        "promo_name": "",
                        "promo_value": 0.0,
                        "promo_target": "",
                        "promo_active": True,
                    },
                    "pending_pos_widget_reset",
                )
                st.rerun()

        if promotions:
            promo_rows = []
            group_id_to_name = {str(g.get("id", "")): str(g.get("name", "")) for g in customer_groups}
            for p in promotions:
                target_text = p.get("target", "")
                if p.get("apply_to") == "Customer Group":
                    target_text = group_id_to_name.get(str(target_text), target_text)
                promo_rows.append(
                    {
                        "Name": p.get("name", ""),
                        "Type": p.get("kind", "Percent"),
                        "Value": p.get("value", 0),
                        "Applies To": p.get("apply_to", "All"),
                        "Target": target_text,
                        "Active": bool(p.get("active", True)),
                    }
                )
            st.dataframe(promo_rows, width="stretch", hide_index=True)
            delete_opts = {f"{p.get('name', 'Promo')} ({p.get('id', '')})": p.get("id") for p in promotions}
            d1, d2 = st.columns(2)
            del_label = d1.selectbox("Select promotion", list(delete_opts.keys()), key="promo_delete_pick")
            if d2.button("Delete Promotion"):
                del_id = delete_opts[del_label]
                promotions = [p for p in promotions if p.get("id") != del_id]
                save_pos_promotions(pg, promotions)
                st.success("Promotion deleted.")
                st.rerun()
        else:
            st.info("No promotions configured.")

    with st.expander("Loyalty & Customer Groups", expanded=True):
        t_loy, t_grp = st.tabs(["Loyalty Program", "Customer Groups"])

        with t_loy:
            loy_enabled = st.checkbox(
                "Enable loyalty points",
                value=bool(loyalty_settings.get("enabled", True)),
                key="loy_enabled",
            )
            loy_earn = st.number_input(
                "Points earned per $1",
                min_value=0.0,
                value=float(loyalty_settings.get("earn_points_per_dollar", 1.0)),
                step=0.1,
                key="loy_earn",
            )
            loy_redeem = st.number_input(
                "Dollar value per 1 point",
                min_value=0.0,
                value=float(loyalty_settings.get("redeem_dollars_per_point", 0.01)),
                step=0.001,
                format="%.3f",
                key="loy_redeem",
            )
            if st.button("Save Loyalty Settings", key="save_loy"):
                save_pos_loyalty_settings(
                    pg,
                    {
                        "enabled": bool(loy_enabled),
                        "earn_points_per_dollar": float(loy_earn),
                        "redeem_dollars_per_point": float(loy_redeem),
                    },
                )
                st.success("Loyalty settings saved.")
                st.rerun()

            st.divider()
            st.caption("Import and manage loyalty-only customers (name/phone). They can be converted to members later.")
            last_sales_sync = get_setting(pg, CIGARPOS_SALES_LAST_SYNC_KEY)
            if last_sales_sync:
                st.caption(f"Last member sales sync: {_format_datetime_12h(last_sales_sync)}")
            if st.button("Sync CigarPOS Member Purchase History", key="sync_cigarpos_member_sales"):
                try:
                    result = sync_cigarpos_member_sales(pg)
                    st.success(
                        "Sales sync complete: "
                        f"endpoint {result['endpoint']}, "
                        f"remote rows {result['remote_rows']}, "
                        f"member matches {result['matched_member_rows']}, "
                        f"imported {result['imported']}, updated {result['updated']}."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sales sync failed: {exc}")

            with st.expander("Debug: Raw CigarPOS Sale Fields", expanded=False):
                st.caption(
                    "Fetches the first few sales rows from CigarPOS and shows every field returned. "
                    "Use this to diagnose why phone / customer matching is not working."
                )
                if st.button("Inspect CigarPOS Sales Fields", key="debug_cigarpos_sales"):
                    try:
                        cfg_s = load_cigarpos_settings(pg)
                        raw_rows, ep = fetch_cigarpos_sales_rows(
                            cfg_s["base_url"], cfg_s["username"], cfg_s["password"]
                        )
                        st.caption(f"Endpoint used: {ep} — {len(raw_rows)} total row(s) returned")
                        for i, row in enumerate(raw_rows[:5]):
                            st.write(f"**Sale row {i + 1}:**")
                            field_rows = [{"Field": k, "Value": str(v)[:200]} for k, v in sorted(row.items())]
                            st.dataframe(field_rows, width="stretch", hide_index=True)
                            # show nested customer dict if present
                            if isinstance(row.get("customer"), dict):
                                st.write("↳ customer sub-object:")
                                cust_rows = [{"Field": k, "Value": str(v)[:200]} for k, v in sorted(row["customer"].items())]
                                st.dataframe(cust_rows, width="stretch", hide_index=True)
                        if not raw_rows:
                            st.info("No sale rows returned from CigarPOS.")
                    except Exception as exc:
                        st.error(f"Debug fetch failed: {exc}")

            if st.button("Import Loyalty Customers From CigarPOS", key="import_loyalty_cigarpos"):
                try:
                    imported = fetch_cigarpos_loyalty_customers(
                        sync_cfg.get("base_url", ""),
                        sync_cfg.get("username", ""),
                        sync_cfg.get("password", ""),
                    )
                    loyalty_customers, added_count, _, imported_points = merge_loyalty_contacts(loyalty_customers, imported)
                    for pkey, pval in (imported_points or {}).items():
                        loyalty_points[str(pkey)] = max(
                            int(loyalty_points.get(str(pkey), 0)),
                            int(pval),
                        )
                    loyalty_customers, loyalty_points, _ = reconcile_loyalty_contacts_with_members(
                        loyalty_customers,
                        members,
                        loyalty_points,
                    )
                    save_pos_loyalty_customers(pg, loyalty_customers)
                    save_pos_loyalty_points(pg, loyalty_points)
                    st.success(f"Imported {len(imported)} loyalty customer row(s). Added {added_count} new contact(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Loyalty import failed: {exc}")

            lc1, lc2, lc3, lc4, lc5 = st.columns(5)
            new_loy_first = lc1.text_input("First Name", key="new_loy_first")
            new_loy_last = lc2.text_input("Last Name", key="new_loy_last")
            new_loy_phone = lc3.text_input("Phone", key="new_loy_phone")
            new_loy_email = lc4.text_input("Email", key="new_loy_email")
            if lc5.button("Add Loyalty Contact", key="add_loy_contact"):
                if not new_loy_first.strip() and not new_loy_phone.strip():
                    st.warning("Enter at least first name or phone.")
                else:
                    now_id = "lc_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
                    incoming = [
                        {
                            "id": now_id,
                            "first_name": new_loy_first.strip(),
                            "last_name": new_loy_last.strip(),
                            "phone": new_loy_phone.strip(),
                            "email": new_loy_email.strip(),
                            "member_id": None,
                            "external_id": "",
                            "source": "manual",
                        }
                    ]
                    loyalty_customers, added_count, _, _ = merge_loyalty_contacts(loyalty_customers, incoming)
                    save_pos_loyalty_customers(pg, loyalty_customers)
                    if added_count:
                        st.success("Loyalty contact added.")
                    else:
                        st.info("Existing loyalty contact matched; details were merged.")
                    queue_widget_reset(
                        {
                            "new_loy_first": "",
                            "new_loy_last": "",
                            "new_loy_phone": "",
                            "new_loy_email": "",
                        },
                        "pending_pos_widget_reset",
                    )
                    st.rerun()

            if loyalty_customers:
                member_name_by_id = {
                    str(m.get("id")): f"{m.get('first_name', '')} {m.get('last_name', '')}".strip()
                    for m in members
                }
                show_only_matched = st.checkbox(
                    "Show only loyalty customers matching member phone numbers",
                    value=True,
                    key="loyalty_members_only_view",
                )
                display_customers = loyalty_customers
                if show_only_matched:
                    display_customers = [c for c in loyalty_customers if str(c.get("member_id") or "").strip()]

                if not display_customers:
                    st.info("No loyalty customers currently match member phone numbers.")
                else:
                    loyalty_rows = []
                    indexed_customers = list(enumerate(display_customers))
                    base_url = _normalize_base_url(sync_cfg.get("base_url", ""))
                    customer_page_url = f"{base_url}/admin/?nocache=1777407513#!customers" if base_url else ""
                    for _, c in indexed_customers:
                        cid = str(c.get("id"))
                        linked_mid = c.get("member_id")
                        linked_mid_txt = str(linked_mid) if linked_mid is not None else ""
                        loyalty_rows.append(
                            {
                                "Name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
                                "Phone": c.get("phone", ""),
                                "Email": c.get("email", ""),
                                "Linked Member": member_name_by_id.get(linked_mid_txt, ""),
                                "Points": int(loyalty_points.get(linked_mid_txt or cid, 0)),
                                "Source": c.get("source", "manual"),
                                "CigarPOS": customer_page_url,
                            }
                        )
                    if customer_page_url:
                        st.dataframe(
                            loyalty_rows,
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "CigarPOS": st.column_config.LinkColumn(
                                    "CigarPOS",
                                    display_text="Open",
                                )
                            },
                        )
                    else:
                        st.dataframe(loyalty_rows, width="stretch", hide_index=True)

                    pick_map = {
                        _loyalty_contact_label(c): orig_idx
                        for orig_idx, c in indexed_customers
                    }
                    chosen_label = st.selectbox("Edit Loyalty Contact", list(pick_map.keys()), key="edit_loy_pick")
                    chosen_idx = pick_map[chosen_label]
                    chosen = loyalty_customers[chosen_idx]
                    e1, e2, e3, e4 = st.columns(4)
                    edit_first = e1.text_input("First", value=chosen.get("first_name", ""), key="edit_loy_first")
                    edit_last = e2.text_input("Last", value=chosen.get("last_name", ""), key="edit_loy_last")
                    edit_phone = e3.text_input("Phone", value=chosen.get("phone", ""), key="edit_loy_phone")
                    edit_email = e4.text_input("Email", value=chosen.get("email", ""), key="edit_loy_email")
                    eb1, eb2 = st.columns(2)
                    if eb1.button("Save Loyalty Contact", key="save_loy_contact"):
                        chosen["first_name"] = edit_first.strip()
                        chosen["last_name"] = edit_last.strip()
                        chosen["phone"] = edit_phone.strip()
                        chosen["email"] = edit_email.strip()
                        loyalty_customers[chosen_idx] = chosen
                        save_pos_loyalty_customers(pg, loyalty_customers)
                        st.success("Loyalty contact saved.")
                        st.rerun()
                    if eb2.button("Delete Loyalty Contact", key="delete_loy_contact"):
                        old_id = str(chosen.get("id"))
                        linked_mid = chosen.get("member_id")
                        linked_key = str(linked_mid) if linked_mid is not None else ""
                        if old_id in loyalty_points and linked_key and linked_key in loyalty_points:
                            loyalty_points[linked_key] = int(loyalty_points.get(linked_key, 0)) + int(loyalty_points.get(old_id, 0))
                        loyalty_points.pop(old_id, None)
                        loyalty_customers.pop(chosen_idx)
                        save_pos_loyalty_customers(pg, loyalty_customers)
                        save_pos_loyalty_points(pg, loyalty_points)
                        st.success("Loyalty contact deleted.")
                        st.rerun()

                    if chosen.get("member_id") in {None, ""}:
                        st.caption("Convert this loyalty contact into a full member and keep loyalty points.")
                        cv1, cv2, cv3 = st.columns(3)
                        convert_tier = cv1.selectbox("Tier", ["Monthly", "Annual"], key="convert_loy_tier")
                        convert_locker = cv2.text_input("Locker", value="", key="convert_loy_locker")
                        convert_months = cv3.number_input("Pay Months", min_value=1, max_value=12, value=1, step=1, key="convert_loy_months")
                        if st.button("Convert To Member", key="convert_loy_member"):
                            try:
                                new_member_id = create_member_from_loyalty_contact(
                                    pg,
                                    chosen,
                                    convert_tier,
                                    convert_locker,
                                    int(convert_months),
                                )
                                old_key = str(chosen.get("id"))
                                new_key = str(new_member_id)
                                moved_points = int(loyalty_points.get(old_key, 0))
                                if moved_points:
                                    loyalty_points[new_key] = int(loyalty_points.get(new_key, 0)) + moved_points
                                    loyalty_points.pop(old_key, None)
                                chosen["member_id"] = new_key
                                loyalty_customers[chosen_idx] = chosen
                                save_pos_loyalty_customers(pg, loyalty_customers)
                                save_pos_loyalty_points(pg, loyalty_points)
                                st.session_state["nav_page_next"] = "Members"
                                st.success("Loyalty contact converted to member and points carried over.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Conversion failed: {exc}")
                    else:
                        st.success("This loyalty contact is already linked to a member.")
            else:
                st.info("No loyalty contacts yet.")

        with t_grp:
            member_label_to_id = {
                f"{m.get('last_name', '')}, {m.get('first_name', '')} (ID {m.get('id')})": str(m.get("id"))
                for m in members
            }
            group_name = st.text_input("New Group Name", key="new_group_name")
            group_member_labels = st.multiselect(
                "Group Members",
                list(member_label_to_id.keys()),
                key="new_group_members",
            )
            if st.button("Create Group", key="create_group"):
                if not group_name.strip():
                    st.warning("Group name is required.")
                else:
                    customer_groups.append(
                        {
                            "id": datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
                            "name": group_name.strip(),
                            "member_ids": [member_label_to_id[x] for x in group_member_labels],
                        }
                    )
                    save_pos_customer_groups(pg, customer_groups)
                    st.success("Group created.")
                    queue_widget_reset(
                        {
                            "new_group_name": "",
                            "new_group_members": [],
                        },
                        "pending_pos_widget_reset",
                    )
                    st.rerun()

            if customer_groups:
                group_opts = {
                    f"{g.get('name', 'Group')} ({len(g.get('member_ids', []))} members)": idx
                    for idx, g in enumerate(customer_groups)
                }
                g_pick = st.selectbox("Edit Group", list(group_opts.keys()), key="edit_group_pick")
                g_idx = group_opts[g_pick]
                g_data = customer_groups[g_idx]
                current_member_ids = {str(x) for x in (g_data.get("member_ids") or [])}
                preselected = [
                    label for label, mid in member_label_to_id.items() if str(mid) in current_member_ids
                ]
                g_new_name = st.text_input("Group Name", value=g_data.get("name", ""), key="edit_group_name")
                g_new_members = st.multiselect(
                    "Members",
                    list(member_label_to_id.keys()),
                    default=preselected,
                    key="edit_group_members",
                )
                eg1, eg2 = st.columns(2)
                if eg1.button("Save Group", key="save_group"):
                    customer_groups[g_idx] = {
                        "id": g_data.get("id"),
                        "name": g_new_name.strip() or g_data.get("name", "Group"),
                        "member_ids": [member_label_to_id[x] for x in g_new_members],
                    }
                    save_pos_customer_groups(pg, customer_groups)
                    st.success("Group updated.")
                    st.rerun()
                if eg2.button("Delete Group", key="delete_group"):
                    customer_groups.pop(g_idx)
                    save_pos_customer_groups(pg, customer_groups)
                    st.success("Group deleted.")
                    st.rerun()
            else:
                st.info("No customer groups yet.")

    st.subheader("Checkout")
    if not inventory:
        st.info("Load inventory first to start checkout.")
    else:
        customer_type = st.radio("Customer type", ["Guest", "Loyalty", "Member"], horizontal=True, key="pos_customer_type")
        selected_member = None
        selected_loyalty = None
        if customer_type == "Loyalty" and loyalty_customers:
            loyalty_opts = {_loyalty_contact_label(c): c for c in loyalty_customers}
            loyalty_label = st.selectbox("Loyalty Customer", list(loyalty_opts.keys()), key="pos_loyalty_pick")
            selected_loyalty = loyalty_opts[loyalty_label]
        if customer_type == "Member" and members:
            member_opts = {
                f"{m['last_name']}, {m['first_name']} (ID {m['id']})": m for m in members
            }
            member_label = st.selectbox("Member", list(member_opts.keys()), key="pos_member_pick")
            selected_member = member_opts[member_label]
        member_by_id = {str(m.get("id")): m for m in members}

        with st.expander("Register Controls", expanded=True):
            rc1, rc2, rc3 = st.columns([3, 1, 1])
            quick_code = rc1.text_input("Add by SKU / UPC", key="pos_quick_code")
            quick_qty = rc2.number_input("Qty", min_value=1, max_value=50, value=1, step=1, key="pos_quick_qty")
            if rc3.button("Add Code", key="pos_quick_add"):
                item = find_inventory_item_by_code(inventory, quick_code)
                if not item:
                    st.warning("No inventory item matched that code.")
                elif int(item.get("stock", 0)) < int(quick_qty):
                    st.warning("Not enough stock for that quantity.")
                else:
                    matched_sku = item.get("sku", "")
                    found = None
                    for line in cart:
                        if line.get("sku") == matched_sku:
                            found = line
                            break
                    if found:
                        found["qty"] = int(found.get("qty", 0)) + int(quick_qty)
                    else:
                        cart.append(
                            {
                                "sku": matched_sku,
                                "name": item.get("name", ""),
                                "category": item.get("category", ""),
                                "price": float(item.get("price", 0.0)),
                                "cost": float(item.get("cost", 0.0)),
                                "qty": int(quick_qty),
                            }
                        )
                    st.success("Item added.")
                    queue_widget_reset(
                        {
                            "pos_quick_code": "",
                            "pos_quick_qty": 1,
                        },
                        "pending_pos_widget_reset",
                    )
                    st.rerun()

            n1, n2, n3 = st.columns([3, 1, 1])
            name_labels = {
                f"{i.get('name', '')} ({i.get('sku', '')})": i
                for i in inventory
            }
            name_pick = n1.selectbox("Add by Name", list(name_labels.keys()), key="pos_name_pick")
            name_qty = n2.number_input("Qty", min_value=1, max_value=50, value=1, step=1, key="pos_name_qty")
            if n3.button("Add Name", key="pos_name_add"):
                item = name_labels[name_pick]
                if int(item.get("stock", 0)) < int(name_qty):
                    st.warning("Not enough stock for that quantity.")
                else:
                    matched_sku = item.get("sku", "")
                    found = None
                    for line in cart:
                        if line.get("sku") == matched_sku:
                            found = line
                            break
                    if found:
                        found["qty"] = int(found.get("qty", 0)) + int(name_qty)
                    else:
                        cart.append(
                            {
                                "sku": matched_sku,
                                "name": item.get("name", ""),
                                "category": item.get("category", ""),
                                "price": float(item.get("price", 0.0)),
                                "cost": float(item.get("cost", 0.0)),
                                "qty": int(name_qty),
                            }
                        )
                    st.success("Item added by name.")
                    st.rerun()

            k1, k2, k3 = st.columns(3)
            if k1.button("Suspend Cart", key="pos_suspend_cart"):
                st.session_state["pos_suspended_sale"] = {
                    "cart": [dict(x) for x in cart],
                    "customer_type": customer_type,
                    "member_id": (selected_member or {}).get("id"),
                    "loyalty_contact_id": (selected_loyalty or {}).get("id"),
                }
                st.session_state["pos_cart"] = []
                st.success("Cart suspended.")
                st.rerun()
            if k2.button("Recall Suspended", key="pos_recall_cart"):
                suspended = st.session_state.get("pos_suspended_sale")
                if suspended and suspended.get("cart"):
                    st.session_state["pos_cart"] = [dict(x) for x in suspended.get("cart", [])]
                    st.success("Suspended cart recalled.")
                    st.rerun()
                else:
                    st.info("No suspended cart available.")
            if k3.button("Void Last Item", key="pos_void_last"):
                if cart:
                    cart.pop()
                    st.rerun()
                else:
                    st.info("Cart is already empty.")

        product_labels = {
            f"{i.get('name', '')} | {i.get('sku', '')} | ${float(i.get('price', 0.0)):.2f} | Stock {int(i.get('stock', 0))}": i
            for i in inventory
        }
        c1, c2 = st.columns([4, 1])
        prod_label = c1.selectbox("Product", list(product_labels.keys()), key="pos_product_pick")
        qty = c2.number_input("Qty", min_value=1, max_value=200, value=1, step=1, key="pos_qty")
        if st.button("Add To Cart"):
            item = product_labels[prod_label]
            line_sku = item.get("sku", "")
            if int(item.get("stock", 0)) < int(qty):
                st.warning("Not enough stock for that quantity.")
            else:
                existing = None
                for line in cart:
                    if line.get("sku") == line_sku:
                        existing = line
                        break
                if existing:
                    existing["qty"] = int(existing.get("qty", 0)) + int(qty)
                else:
                    cart.append(
                        {
                            "sku": line_sku,
                            "name": item.get("name", ""),
                            "category": item.get("category", ""),
                            "price": float(item.get("price", 0.0)),
                            "cost": float(item.get("cost", 0.0)),
                            "qty": int(qty),
                        }
                    )
                st.success("Added to cart.")
                st.rerun()

        if cart:
            cart_rows = []
            subtotal = 0.0
            for line in cart:
                line_total = float(line.get("price", 0.0)) * int(line.get("qty", 0))
                subtotal += line_total
                cart_rows.append(
                    {
                        "SKU": line.get("sku", ""),
                        "Name": line.get("name", ""),
                        "Qty": int(line.get("qty", 0)),
                        "Price": float(line.get("price", 0.0)),
                        "Line Total": round(line_total, 2),
                    }
                )
            st.dataframe(cart_rows, width="stretch", hide_index=True)

            line_opts = {
                f"{line.get('name', '')} ({line.get('sku', '')})": idx
                for idx, line in enumerate(cart)
            }
            r1, r2, r3 = st.columns(3)
            rm_label = r1.selectbox("Cart item", list(line_opts.keys()), key="pos_remove_pick")
            if r2.button("Remove Item"):
                cart.pop(line_opts[rm_label])
                st.rerun()
            if r3.button("Clear Cart"):
                st.session_state["pos_cart"] = []
                st.rerun()

            active_promos = []
            selected_member_id = (selected_member or {}).get("id")
            selected_loyalty_id = (selected_loyalty or {}).get("id")
            linked_member_id = (selected_loyalty or {}).get("member_id") if selected_loyalty else None
            resolved_member_id = selected_member_id if selected_member_id not in {None, ""} else linked_member_id
            resolved_member_id = str(resolved_member_id) if resolved_member_id not in {None, ""} else None
            resolved_member = member_by_id.get(resolved_member_id) if resolved_member_id else selected_member
            is_member = resolved_member is not None
            member_tier = (resolved_member or {}).get("tier", "")
            loyalty_actor_key = ""
            if resolved_member_id is not None:
                loyalty_actor_key = str(resolved_member_id)
            elif selected_loyalty is not None:
                loyalty_actor_key = str(selected_loyalty_id)
            current_member_points = int(loyalty_points.get(loyalty_actor_key, 0)) if loyalty_actor_key else 0
            for p in promotions:
                if _promotion_applies(p, cart, is_member, member_tier, resolved_member_id, customer_groups):
                    active_promos.append(p)

            with st.expander("Discount / Promotion", expanded=True):
                promo_choices = {"No promotion": None}
                for p in active_promos:
                    label = f"{p.get('name', 'Promo')} ({p.get('kind', 'Percent')} {p.get('value', 0)})"
                    promo_choices[label] = p

                picked_promo_label = st.selectbox("Promotion", list(promo_choices.keys()), key="pos_promo_apply")
                chosen_promo = promo_choices[picked_promo_label]

                d1, d2, d3 = st.columns(3)
                manual_kind = d1.selectbox("Manual Discount Type", ["Percent", "Fixed"], key="pos_manual_disc_type")
                manual_value = d2.number_input("Manual Discount Value", min_value=0.0, value=0.0, step=0.5, key="pos_manual_disc_value")
                if d3.button("Apply Manual Discount", key="pos_apply_manual_disc"):
                    st.session_state["pos_manual_discount"] = {
                        "kind": manual_kind,
                        "value": float(manual_value),
                    }
                    st.success("Manual discount applied.")

                if st.button("Clear Manual Discount", key="pos_clear_manual_disc"):
                    st.session_state.pop("pos_manual_discount", None)
                    st.success("Manual discount cleared.")
                    st.rerun()

            loyalty_redeem_points = 0
            loyalty_discount = 0.0
            if loyalty_settings.get("enabled", True) and loyalty_actor_key:
                with st.expander("Loyalty", expanded=True):
                    earn_rate = float(loyalty_settings.get("earn_points_per_dollar", 1.0) or 0.0)
                    redeem_value = float(loyalty_settings.get("redeem_dollars_per_point", 0.01) or 0.0)
                    l1, l2 = st.columns(2)
                    l1.metric("Current Points", f"{current_member_points}")
                    est_earn = int(max(0.0, subtotal) * max(0.0, earn_rate))
                    l2.metric("Est. Points Earned", f"{est_earn}")
                    loyalty_redeem_points = st.number_input(
                        "Redeem Points",
                        min_value=0,
                        max_value=max(0, current_member_points),
                        value=0,
                        step=1,
                        key="pos_loyalty_redeem_points",
                    )
                    loyalty_discount = min(subtotal, round(float(loyalty_redeem_points) * max(0.0, redeem_value), 2))
                    st.caption(f"Loyalty discount value: ${loyalty_discount:,.2f}")

            promo_discount = calculate_discount(subtotal, chosen_promo) if chosen_promo else 0.0
            manual_discount_data = st.session_state.get("pos_manual_discount")
            manual_discount = calculate_discount(subtotal, manual_discount_data) if manual_discount_data else 0.0
            discount_mode_opts = ["Promotion", "Manual", "Loyalty", "None"]
            discount_mode = st.radio("Discount Source", discount_mode_opts, horizontal=True, key="pos_discount_source")
            if discount_mode == "Promotion":
                discount = promo_discount
            elif discount_mode == "Manual":
                discount = manual_discount
            elif discount_mode == "Loyalty":
                discount = loyalty_discount
            else:
                discount = 0.0

            total = max(0.0, round(subtotal - discount, 2))

            m1, m2, m3 = st.columns(3)
            m1.metric("Subtotal", f"${subtotal:,.2f}")
            m2.metric("Discount", f"-${discount:,.2f}")
            m3.metric("Total", f"${total:,.2f}")

            payment_method = None
            p1, p2, p3, p4, p5 = st.columns(5)
            if p1.button("Pay Cash", type="primary", key="pos_pay_cash"):
                payment_method = "Cash"
            if p2.button("Pay Card", key="pos_pay_card"):
                payment_method = "Card"
            if p3.button("Pay Other", key="pos_pay_other"):
                payment_method = "Other"
            if p4.button("Suspend", key="pos_pay_suspend"):
                st.session_state["pos_suspended_sale"] = {
                    "cart": [dict(x) for x in cart],
                    "customer_type": customer_type,
                    "member_id": (selected_member or {}).get("id"),
                    "loyalty_contact_id": (selected_loyalty or {}).get("id"),
                }
                st.session_state["pos_cart"] = []
                st.success("Cart suspended.")
                st.rerun()
            if p5.button("Cancel", key="pos_pay_cancel"):
                st.session_state["pos_cart"] = []
                st.success("Cart canceled.")
                st.rerun()

            if payment_method:
                try:
                    inv_map = {str(i.get("sku", "")).strip(): i for i in inventory if i.get("sku")}
                    for line in cart:
                        sku = str(line.get("sku", "")).strip()
                        item = inv_map.get(sku)
                        if not item:
                            raise ValueError(f"Missing inventory item for SKU {sku}.")
                        if int(item.get("stock", 0)) < int(line.get("qty", 0)):
                            raise ValueError(f"Not enough stock for {line.get('name', sku)}.")
                    for line in cart:
                        sku = str(line.get("sku", "")).strip()
                        inv_map[sku]["stock"] = int(inv_map[sku].get("stock", 0)) - int(line.get("qty", 0))
                    save_pos_inventory(pg, list(inv_map.values()))

                    sale_items = []
                    total_discount_to_allocate = round(float(discount), 2)
                    running_allocated = 0.0
                    cart_len = len(cart)
                    sale_cost_total = 0.0

                    for idx, line in enumerate(cart):
                        sku = str(line.get("sku", "")).strip()
                        inv_item = inv_map.get(sku, {})
                        qty = int(line.get("qty", 0))
                        unit_price = round(float(line.get("price", inv_item.get("price", 0.0)) or 0.0), 2)
                        unit_cost = round(float(line.get("cost", inv_item.get("cost", 0.0)) or 0.0), 2)
                        regular_line_total = round(unit_price * qty, 2)

                        if idx == cart_len - 1:
                            allocated_discount = round(total_discount_to_allocate - running_allocated, 2)
                        else:
                            share = (regular_line_total / subtotal) if subtotal > 0 else 0.0
                            allocated_discount = round(total_discount_to_allocate * share, 2)
                            running_allocated += allocated_discount

                        discounted_line_total = max(0.0, round(regular_line_total - allocated_discount, 2))
                        line_regular_margin = round(regular_line_total - (unit_cost * qty), 2)
                        line_discounted_margin = round(discounted_line_total - (unit_cost * qty), 2)

                        sale_items.append(
                            {
                                "sku": sku,
                                "name": line.get("name", ""),
                                "category": line.get("category", ""),
                                "qty": qty,
                                "price": unit_price,
                                "unit_cost": unit_cost,
                                "regular_line_total": regular_line_total,
                                "allocated_discount": allocated_discount,
                                "discounted_line_total": discounted_line_total,
                                "regular_margin": line_regular_margin,
                                "discounted_margin": line_discounted_margin,
                            }
                        )
                        sale_cost_total += unit_cost * qty

                    sale_cost_total = round(sale_cost_total, 2)
                    discounted_total = round(total, 2)
                    regular_total = round(subtotal, 2)
                    member_discount_total = round(float(discount), 2) if resolved_member_id is not None else 0.0

                    sale = {
                        "id": datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
                        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "customer": (
                            f"{selected_member.get('first_name', '')} {selected_member.get('last_name', '')}".strip()
                            if selected_member
                            else (
                                f"{selected_loyalty.get('first_name', '')} {selected_loyalty.get('last_name', '')}".strip()
                                if selected_loyalty
                                else "Guest"
                            )
                        ),
                        "member_id": resolved_member_id,
                        "loyalty_contact_id": (selected_loyalty or {}).get("id"),
                        "subtotal": round(subtotal, 2),
                        "discount": round(discount, 2),
                        "total": round(total, 2),
                        "regular_total": regular_total,
                        "discounted_total": discounted_total,
                        "cost_total": sale_cost_total,
                        "member_discount_total": member_discount_total,
                        "gross_margin_regular": round(regular_total - sale_cost_total, 2),
                        "gross_margin_discounted": round(discounted_total - sale_cost_total, 2),
                        "promotion": (chosen_promo or {}).get("name", ""),
                        "discount_source": discount_mode,
                        "payment_method": payment_method,
                        "loyalty_points_redeemed": int(loyalty_redeem_points) if discount_mode == "Loyalty" else 0,
                        "items": sale_items,
                    }

                    if loyalty_settings.get("enabled", True) and loyalty_actor_key:
                        earn_rate = float(loyalty_settings.get("earn_points_per_dollar", 1.0) or 0.0)
                        earned_points = int(max(0.0, total) * max(0.0, earn_rate))
                        used_points = int(loyalty_redeem_points) if discount_mode == "Loyalty" else 0
                        new_points = max(0, current_member_points - used_points) + max(0, earned_points)
                        loyalty_points[loyalty_actor_key] = int(new_points)
                        save_pos_loyalty_points(pg, loyalty_points)
                        sale["loyalty_points_earned"] = int(earned_points)
                        sale["loyalty_points_balance"] = int(new_points)

                    sales.append(sale)
                    sales = sales[-500:]
                    save_pos_sales(pg, sales)
                    st.session_state["pos_last_receipt"] = sale
                    st.session_state["pos_cart"] = []
                    st.success(f"Sale completed via {payment_method} and inventory updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Checkout failed: {exc}")
        else:
            st.caption("Cart is empty.")

    with st.expander("Receipt Preview", expanded=True):
        receipt = st.session_state.get("pos_last_receipt")
        if not receipt:
            st.info("No receipt yet. Complete a sale to preview the latest receipt.")
        else:
            lines = []
            lines.append("LIBERTY SMOKES")
            lines.append("------------------------------")
            lines.append(f"Date: {_format_datetime_12h(str(receipt.get('created_at', '')))}")
            lines.append(f"Customer: {receipt.get('customer', 'Guest')}")
            lines.append(f"Payment: {receipt.get('payment_method', '')}")
            lines.append("------------------------------")
            for item in receipt.get("items", []):
                qty = int(item.get("qty", 0))
                price = float(item.get("price", 0.0))
                line_total = qty * price
                lines.append(f"{qty} x {item.get('name', '')}")
                lines.append(f"  @{price:,.2f} = ${line_total:,.2f}")
            lines.append("------------------------------")
            lines.append(f"Subtotal: ${float(receipt.get('subtotal', 0.0)):,.2f}")
            lines.append(f"Discount: -${float(receipt.get('discount', 0.0)):,.2f}")
            lines.append(f"Total: ${float(receipt.get('total', 0.0)):,.2f}")
            lines.append("Thank you for your business")
            st.code("\n".join(lines), language="text")

    with st.expander("Recent Sales", expanded=False):
        if not sales:
            st.info("No sales yet.")
        else:
            history_rows = []
            for sale in reversed(sales[-20:]):
                history_rows.append(
                    {
                        "When": _format_datetime_12h(str(sale.get("created_at", ""))),
                        "Customer": sale.get("customer", ""),
                        "Payment": sale.get("payment_method", ""),
                        "Loyalty Earned": sale.get("loyalty_points_earned", 0),
                        "Loyalty Redeemed": sale.get("loyalty_points_redeemed", 0),
                        "Subtotal": sale.get("subtotal", 0.0),
                        "Discount": sale.get("discount", 0.0),
                        "Discount Source": sale.get("discount_source", ""),
                        "Total": sale.get("total", 0.0),
                        "Promo": sale.get("promotion", ""),
                    }
                )
            st.dataframe(history_rows, width="stretch", hide_index=True)


def page_scanner(pg: SyncPostgrestClient):
    st.header("Wireless Scanner")
    st.caption("Open this page on a phone to send UPC/SKU scans to the POS page.")

    default_channel = _sanitize_scan_channel(get_setting(pg, POS_SCAN_CHANNEL_KEY) or "main")
    channel = st.text_input("Scanner Channel", value=default_channel, key="scanner_channel")
    active_channel = _sanitize_scan_channel(channel)
    source_name = st.text_input("Device Name", value="phone", key="scanner_source")

    with st.form("scanner_send_form", clear_on_submit=True):
        code = st.text_input("UPC / Barcode / SKU", key="scanner_code")
        qty = st.number_input("Quantity", min_value=1, max_value=50, value=1, step=1, key="scanner_qty")
        submitted = st.form_submit_button("Send To POS")
        if submitted:
            clean_code = (code or "").strip()
            if not clean_code:
                st.warning("Enter a code first.")
            else:
                for _ in range(int(qty)):
                    enqueue_scan(pg, active_channel, clean_code, source=source_name or "phone")
                st.success(f"Sent {int(qty)} scan(s) to channel '{active_channel}'.")

    qrows = load_scan_queue(pg, active_channel)
    st.caption(f"Pending scans on channel '{active_channel}': {len(qrows)}")
    if qrows:
        preview = []
        for row in reversed(qrows[-10:]):
            preview.append(
                {
                    "At": row.get("at", ""),
                    "Code": row.get("code", ""),
                    "Source": row.get("source", ""),
                }
            )
        st.dataframe(preview, width="stretch", hide_index=True)


# ── Page: Settings ─────────────────────────────────────────────────────────────

def page_settings(pg: SyncPostgrestClient):
    st.header("Settings")

    with st.expander("Display", expanded=True):
        st.checkbox(
            "Enable mobile layout optimizations",
            key="mobile_mode",
            help="Turn responsive mobile styling on or off.",
        )
        st.divider()
        st.caption("Sidebar navigation visibility")
        prev_show_pos = bool(st.session_state.get("show_pos_nav", True))
        prev_show_scanner = bool(st.session_state.get("show_scanner_nav", True))
        prev_show_member_margin = _bool_setting(get_setting(pg, MEMBER_MARGIN_SECTION_KEY), True)
        cfg_show_pos = st.checkbox(
            "Show POS in sidebar",
            value=prev_show_pos,
            key="cfg_show_pos_nav",
        )
        cfg_show_scanner = st.checkbox(
            "Show Scanner in sidebar",
            value=prev_show_scanner,
            key="cfg_show_scanner_nav",
        )
        cfg_show_member_margin = st.checkbox(
            "Show Member Purchase Margins in Members page",
            value=prev_show_member_margin,
            key="cfg_show_member_margin",
        )
        if (
            cfg_show_pos != prev_show_pos
            or cfg_show_scanner != prev_show_scanner
            or cfg_show_member_margin != prev_show_member_margin
        ):
            save_setting(pg, NAV_SHOW_POS_KEY, "1" if cfg_show_pos else "0")
            save_setting(pg, NAV_SHOW_SCANNER_KEY, "1" if cfg_show_scanner else "0")
            save_setting(pg, MEMBER_MARGIN_SECTION_KEY, "1" if cfg_show_member_margin else "0")
            st.session_state["show_pos_nav"] = bool(cfg_show_pos)
            st.session_state["show_scanner_nav"] = bool(cfg_show_scanner)
            st.rerun()

    with st.expander("Drink Limits", expanded=True):
        st.number_input(
            "Member drink limit",
            min_value=1, max_value=20,
            value=st.session_state.get("drink_limit", 3),
            step=1,
            key="drink_limit",
            help="Applied when the seated customer name matches a member on the Members page.",
        )
        st.number_input(
            "Non-member drink limit",
            min_value=1, max_value=20,
            value=st.session_state.get("non_member_limit", 1),
            step=1,
            key="non_member_limit",
            help="Applied when the seated customer is not found in the members list.",
        )

    with st.expander("Drink Catalog", expanded=True):
        st.caption("Add drinks with cost and alcohol category. Seats will use this list for per-chair tracking.")
        try:
            drink_catalog = load_drink_catalog(pg)
        except Exception:
            drink_catalog = []

        dc1, dc2, dc3 = st.columns(3)
        new_drink_name = dc1.text_input("Drink name", key="cfg_drink_name")
        new_drink_category = dc2.selectbox(
            "Category",
            ["alcoholic", "non_alcoholic"],
            format_func=lambda x: "Alcoholic" if x == "alcoholic" else "Non-Alcoholic",
            key="cfg_drink_category",
        )
        new_drink_cost = dc3.number_input(
            "Cost",
            min_value=0.0,
            max_value=1000.0,
            value=0.0,
            step=0.25,
            key="cfg_drink_cost",
        )

        if st.button("Add Drink", key="cfg_add_drink"):
            clean_name = new_drink_name.strip()
            if not clean_name:
                st.warning("Enter a drink name.")
            else:
                exists = False
                for item in drink_catalog:
                    if str(item.get("name", "")).strip().lower() == clean_name.lower():
                        exists = True
                        break
                if exists:
                    st.warning("That drink already exists. Remove it first if you need to replace it.")
                else:
                    drink_catalog.append(
                        {
                            "id": hashlib.sha1(
                                f"{clean_name.lower()}|{new_drink_category}|{datetime.datetime.now().isoformat()}".encode("utf-8")
                            ).hexdigest()[:12],
                            "name": clean_name,
                            "category": new_drink_category,
                            "cost": round(float(new_drink_cost or 0.0), 2),
                        }
                    )
                    save_drink_catalog(pg, drink_catalog)
                    st.success(f"Added {clean_name}.")
                    st.rerun()

        if drink_catalog:
            catalog_rows = []
            for item in drink_catalog:
                catalog_rows.append(
                    {
                        "Name": item.get("name", ""),
                        "Category": "Alcoholic" if item.get("category") == "alcoholic" else "Non-Alcoholic",
                        "Cost": float(item.get("cost") or 0.0),
                    }
                )
            st.dataframe(catalog_rows, width="stretch", hide_index=True)

            editable = {
                f"{item.get('name', '')} ({'Alcoholic' if item.get('category') == 'alcoholic' else 'Non-Alcoholic'})": item
                for item in drink_catalog
            }
            edit_pick = st.selectbox(
                "Edit drink",
                list(editable.keys()),
                key="cfg_edit_drink_pick",
            )
            edit_selected = editable[edit_pick]

            ec1, ec2, ec3 = st.columns(3)
            edit_name = ec1.text_input(
                "Edit name",
                value=str(edit_selected.get("name") or ""),
                key=f"cfg_edit_name_{edit_selected.get('id')}",
            )
            edit_category = ec2.selectbox(
                "Edit category",
                ["alcoholic", "non_alcoholic"],
                index=0 if str(edit_selected.get("category") or "") == "alcoholic" else 1,
                format_func=lambda x: "Alcoholic" if x == "alcoholic" else "Non-Alcoholic",
                key=f"cfg_edit_category_{edit_selected.get('id')}",
            )
            edit_cost = ec3.number_input(
                "Edit cost",
                min_value=0.0,
                max_value=1000.0,
                value=float(edit_selected.get("cost") or 0.0),
                step=0.25,
                key=f"cfg_edit_cost_{edit_selected.get('id')}",
            )

            if st.button("Save Drink Changes", key="cfg_save_drink_btn"):
                clean_name = edit_name.strip()
                if not clean_name:
                    st.warning("Drink name cannot be blank.")
                else:
                    exists = False
                    for item in drink_catalog:
                        if str(item.get("id")) == str(edit_selected.get("id")):
                            continue
                        if str(item.get("name", "")).strip().lower() == clean_name.lower():
                            exists = True
                            break
                    if exists:
                        st.warning("Another drink already uses that name.")
                    else:
                        updated = []
                        for item in drink_catalog:
                            if str(item.get("id")) == str(edit_selected.get("id")):
                                row = dict(item)
                                row["name"] = clean_name
                                row["category"] = edit_category
                                row["cost"] = round(float(edit_cost or 0.0), 2)
                                updated.append(row)
                            else:
                                updated.append(item)
                        save_drink_catalog(pg, updated)
                        st.success("Drink updated.")
                        st.rerun()

            st.divider()

            removable = {
                f"{item.get('name', '')} ({'Alcoholic' if item.get('category') == 'alcoholic' else 'Non-Alcoholic'})": item
                for item in drink_catalog
            }
            remove_pick = st.selectbox(
                "Remove drink",
                list(removable.keys()),
                key="cfg_remove_drink_pick",
            )
            if st.button("Remove Selected Drink", key="cfg_remove_drink_btn"):
                selected = removable[remove_pick]
                remaining = [
                    item for item in drink_catalog
                    if str(item.get("id")) != str(selected.get("id"))
                ]
                save_drink_catalog(pg, remaining)
                st.success("Drink removed.")
                st.rerun()
        else:
            st.info("No drinks configured yet.")

    with st.expander("Email Config (SMTP)", expanded=True):
        try:
            smtp = load_smtp_settings(pg)
        except Exception:
            smtp = {
                "host": "smtp.gmail.com",
                "port": 465,
                "security": "SSL",
                "username": "",
                "password": "",
                "from_addr": "",
            }

        st.caption("Use any SMTP provider (Gmail, Outlook, custom domain, etc.).")

        preset_map = {
            "Gmail": {"host": "smtp.gmail.com", "port": 465, "security": "SSL"},
            "Outlook / Microsoft 365": {
                "host": "smtp.office365.com",
                "port": 587,
                "security": "STARTTLS",
            },
            "Yahoo": {"host": "smtp.mail.yahoo.com", "port": 465, "security": "SSL"},
            "Custom (manual)": None,
        }

        p1, p2 = st.columns([2, 1])
        selected_preset = p1.selectbox(
            "Provider preset",
            list(preset_map.keys()),
            key="cfg_smtp_preset",
            help="Apply host/port/security defaults for common providers.",
        )
        if p2.button("Apply Preset"):
            preset = preset_map.get(selected_preset)
            if preset:
                st.session_state["cfg_smtp_host"] = preset["host"]
                st.session_state["cfg_smtp_port"] = preset["port"]
                st.session_state["cfg_smtp_security"] = preset["security"]
                st.rerun()

        host = st.text_input("SMTP host", value=smtp["host"], key="cfg_smtp_host")
        port = st.number_input("SMTP port", min_value=1, max_value=65535, value=int(smtp["port"]), step=1, key="cfg_smtp_port")
        sec_opts = ["SSL", "STARTTLS", "NONE"]
        security = st.selectbox(
            "Security",
            sec_opts,
            index=sec_opts.index(smtp["security"]) if smtp["security"] in sec_opts else 0,
            key="cfg_smtp_security",
        )
        user = st.text_input("SMTP username", value=smtp["username"], key="cfg_smtp_user")
        from_addr = st.text_input("From email", value=smtp["from_addr"] or smtp["username"], key="cfg_smtp_from")
        pw = st.text_input("SMTP password / app password / API key", type="password", key="cfg_smtp_pw")

        c1, c2 = st.columns(2)
        if c1.button("Save / Update Email Config"):
            try:
                save_setting(pg, "smtp_host", host.strip())
                save_setting(pg, "smtp_port", str(int(port)))
                save_setting(pg, "smtp_security", security)
                save_setting(pg, "smtp_username", user.strip())
                save_setting(pg, "smtp_from", from_addr.strip())
                if pw.strip():
                    save_setting(pg, "smtp_password", pw.strip())
                # Keep legacy key synced for backward compatibility.
                save_setting(pg, "smtp_email", user.strip())
                st.success("Saved.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

        st.divider()
        st.caption("Send a quick SMTP test message.")
        test_to = st.text_input(
            "Test recipient",
            value=(from_addr.strip() or user.strip()),
            key="cfg_test_to",
            help="Leave as your own email, or enter another address.",
        )
        if st.button("Send Test Email"):
            smtp_host = host.strip() or smtp["host"]
            smtp_port = int(port) if int(port) > 0 else int(smtp["port"])
            smtp_security = security
            smtp_user = user.strip() or smtp["username"]
            smtp_pass = pw.strip() or smtp["password"]
            smtp_from = from_addr.strip() or smtp_user
            recipient = test_to.strip() or smtp_from
            if not smtp_host or not smtp_port or not smtp_from or not smtp_pass or not recipient:
                st.warning("Set SMTP host, port, from email, password, and recipient before testing.")
            else:
                try:
                    send_email(
                        smtp_host,
                        smtp_port,
                        smtp_user,
                        smtp_pass,
                        recipient,
                        "Liberty Smokes SMTP Test",
                        "This is a test email from your Liberty Smokes dashboard.",
                        security=smtp_security,
                        from_addr=smtp_from,
                    )
                    st.success(f"Test email sent to {recipient}.")
                except Exception as exc:
                    st.error(f"Test failed: {exc}")
        if c2.button("Clear Saved Email Config"):
            try:
                clear_setting(pg, "smtp_host")
                clear_setting(pg, "smtp_port")
                clear_setting(pg, "smtp_security")
                clear_setting(pg, "smtp_username")
                clear_setting(pg, "smtp_from")
                clear_setting(pg, "smtp_email")
                clear_setting(pg, "smtp_password")
                st.session_state["cfg_smtp_host"] = "smtp.gmail.com"
                st.session_state["cfg_smtp_port"] = 465
                st.session_state["cfg_smtp_security"] = "SSL"
                st.session_state["cfg_smtp_preset"] = "Gmail"
                st.session_state["cfg_smtp_user"] = ""
                st.session_state["cfg_smtp_from"] = ""
                st.session_state["cfg_smtp_pw"] = ""
                st.success("Email settings cleared.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    with st.expander("Sales Rep Email Config (Optional)", expanded=False):
        try:
            ordering_smtp = load_ordering_smtp_settings(pg)
        except Exception:
            ordering_smtp = {
                "host": "",
                "port": 465,
                "security": "SSL",
                "username": "",
                "password": "",
                "from_addr": "",
            }

        st.caption("Use this only if you want order emails to send from a different mailbox than member email.")
        sec_opts = ["SSL", "STARTTLS", "NONE"]

        o_host = st.text_input("SMTP host", value=ordering_smtp["host"], key="cfg_ordering_smtp_host")
        o_port = st.number_input(
            "SMTP port",
            min_value=1,
            max_value=65535,
            value=int(ordering_smtp["port"]),
            step=1,
            key="cfg_ordering_smtp_port",
        )
        o_security = st.selectbox(
            "Security",
            sec_opts,
            index=sec_opts.index(ordering_smtp["security"]) if ordering_smtp["security"] in sec_opts else 0,
            key="cfg_ordering_smtp_security",
        )
        o_user = st.text_input("SMTP username", value=ordering_smtp["username"], key="cfg_ordering_smtp_user")
        o_from = st.text_input("From email", value=ordering_smtp["from_addr"] or ordering_smtp["username"], key="cfg_ordering_smtp_from")
        o_pw = st.text_input("SMTP password / app password", type="password", key="cfg_ordering_smtp_pw")

        oc1, oc2 = st.columns(2)
        if oc1.button("Save Sales Rep Email Config", key="cfg_ordering_smtp_save"):
            try:
                save_setting(pg, "ordering_smtp_host", o_host.strip())
                save_setting(pg, "ordering_smtp_port", str(int(o_port)))
                save_setting(pg, "ordering_smtp_security", o_security)
                save_setting(pg, "ordering_smtp_username", o_user.strip())
                save_setting(pg, "ordering_smtp_from", o_from.strip())
                if o_pw.strip():
                    save_setting(pg, "ordering_smtp_password", o_pw.strip())
                st.success("Sales rep email settings saved.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

        st.divider()
        o_test_to = st.text_input(
            "Test recipient",
            value=(o_from.strip() or o_user.strip()),
            key="cfg_ordering_smtp_test_to",
        )
        if st.button("Send Sales Rep SMTP Test", key="cfg_ordering_smtp_test"):
            smtp_host = o_host.strip() or ordering_smtp["host"]
            smtp_port = int(o_port) if int(o_port) > 0 else int(ordering_smtp["port"])
            smtp_security = o_security
            smtp_user = o_user.strip() or ordering_smtp["username"]
            smtp_pass = o_pw.strip() or ordering_smtp["password"]
            smtp_from = o_from.strip() or smtp_user
            recipient = o_test_to.strip() or smtp_from
            if not smtp_host or not smtp_port or not smtp_from or not smtp_pass or not recipient:
                st.warning("Set SMTP host, port, from email, password, and recipient before testing.")
            else:
                try:
                    send_email(
                        smtp_host,
                        smtp_port,
                        smtp_user,
                        smtp_pass,
                        recipient,
                        "Liberty Smokes Sales Rep SMTP Test",
                        "This is a test email from the Sales Rep SMTP profile.",
                        security=smtp_security,
                        from_addr=smtp_from,
                    )
                    st.success(f"Test email sent to {recipient}.")
                except Exception as exc:
                    st.error(f"Test failed: {exc}")

        if oc2.button("Clear Sales Rep Email Config", key="cfg_ordering_smtp_clear"):
            try:
                clear_setting(pg, "ordering_smtp_host")
                clear_setting(pg, "ordering_smtp_port")
                clear_setting(pg, "ordering_smtp_security")
                clear_setting(pg, "ordering_smtp_username")
                clear_setting(pg, "ordering_smtp_from")
                clear_setting(pg, "ordering_smtp_password")
                st.session_state["cfg_ordering_smtp_host"] = ""
                st.session_state["cfg_ordering_smtp_port"] = 465
                st.session_state["cfg_ordering_smtp_security"] = "SSL"
                st.session_state["cfg_ordering_smtp_user"] = ""
                st.session_state["cfg_ordering_smtp_from"] = ""
                st.session_state["cfg_ordering_smtp_pw"] = ""
                st.success("Sales rep email settings cleared.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    with st.expander("Email Templates", expanded=False):
        st.caption(
            "Available placeholders: {first_name}, {last_name}, {full_name}, {tier}, "
            "{next_billing_date}, {join_date}, {status}, {locker}, {email}, {phone}"
        )

        try:
            tpl = load_email_templates(pg)
        except Exception:
            tpl = EMAIL_TEMPLATE_DEFAULTS

        welcome_subject = st.text_input(
            "Welcome email subject",
            value=tpl["welcome_subject"],
            key="tpl_welcome_subject",
        )
        welcome_body = st.text_area(
            "Welcome email body",
            value=tpl["welcome_body"],
            key="tpl_welcome_body",
            height=120,
        )
        renewal_subject = st.text_input(
            "7-day reminder subject",
            value=tpl["renewal_subject"],
            key="tpl_renewal_subject",
        )
        renewal_body = st.text_area(
            "7-day reminder body",
            value=tpl["renewal_body"],
            key="tpl_renewal_body",
            height=100,
        )
        past_due_subject = st.text_input(
            "Past-due reminder subject",
            value=tpl["past_due_subject"],
            key="tpl_past_due_subject",
        )
        past_due_body = st.text_area(
            "Past-due reminder body",
            value=tpl["past_due_body"],
            key="tpl_past_due_body",
            height=100,
        )

        t1, t2 = st.columns(2)
        if t1.button("Save Email Templates"):
            try:
                save_setting(pg, "email_tpl_welcome_subject", welcome_subject)
                save_setting(pg, "email_tpl_welcome_body", welcome_body)
                save_setting(pg, "email_tpl_renewal_subject", renewal_subject)
                save_setting(pg, "email_tpl_renewal_body", renewal_body)
                save_setting(pg, "email_tpl_past_due_subject", past_due_subject)
                save_setting(pg, "email_tpl_past_due_body", past_due_body)
                st.success("Email templates saved.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

        if t2.button("Reset Templates to Default"):
            try:
                clear_setting(pg, "email_tpl_welcome_subject")
                clear_setting(pg, "email_tpl_welcome_body")
                clear_setting(pg, "email_tpl_renewal_subject")
                clear_setting(pg, "email_tpl_renewal_body")
                clear_setting(pg, "email_tpl_past_due_subject")
                clear_setting(pg, "email_tpl_past_due_body")
                st.session_state["tpl_welcome_subject"] = EMAIL_TEMPLATE_DEFAULTS["welcome_subject"]
                st.session_state["tpl_welcome_body"] = EMAIL_TEMPLATE_DEFAULTS["welcome_body"]
                st.session_state["tpl_renewal_subject"] = EMAIL_TEMPLATE_DEFAULTS["renewal_subject"]
                st.session_state["tpl_renewal_body"] = EMAIL_TEMPLATE_DEFAULTS["renewal_body"]
                st.session_state["tpl_past_due_subject"] = EMAIL_TEMPLATE_DEFAULTS["past_due_subject"]
                st.session_state["tpl_past_due_body"] = EMAIL_TEMPLATE_DEFAULTS["past_due_body"]
                st.success("Templates reset to defaults.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    with st.expander("Automated Reminder Emails", expanded=False):
        auto_enabled = _bool_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_ENABLED_KEY), False)
        auto_interval = max(5, _int_setting(get_setting(pg, EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY), 60))
        auto_last_run = get_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY)
        auto_last_result = get_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY)

        a1, a2 = st.columns(2)
        cfg_auto_enabled = a1.checkbox(
            "Enable automatic reminder sending",
            value=auto_enabled,
            key="cfg_auto_email_reminders",
            help="This switch controls all automated reminder runs, including the Windows scheduled task.",
        )
        cfg_auto_interval = a2.number_input(
            "Run every N minutes",
            min_value=5,
            max_value=1440,
            value=int(auto_interval),
            step=5,
            key="cfg_auto_email_interval_min",
        )

        s1, s2 = st.columns(2)
        if s1.button("Save Automation Settings", key="cfg_save_auto_email_reminders"):
            save_setting(pg, EMAIL_REMINDERS_AUTO_ENABLED_KEY, "1" if cfg_auto_enabled else "0")
            save_setting(pg, EMAIL_REMINDERS_AUTO_INTERVAL_MIN_KEY, str(int(cfg_auto_interval)))
            st.success("Automation settings saved.")
            st.rerun()

        if s2.button("Run Reminder Cycle Now", key="cfg_run_auto_email_now"):
            try:
                smtp_cfg = load_smtp_settings(pg)
                templates_cfg = load_email_templates(pg)
                members_cfg = fetch_members(pg)
                if (
                    not smtp_cfg.get("host")
                    or not smtp_cfg.get("port")
                    or not smtp_cfg.get("from_addr")
                    or not smtp_cfg.get("password")
                ):
                    st.warning("Configure SMTP settings first.")
                else:
                    stats = run_pending_member_reminders(pg, smtp_cfg, templates_cfg, members_cfg)
                    now_txt = datetime.datetime.now().isoformat(timespec="seconds")
                    save_setting(pg, EMAIL_REMINDERS_AUTO_LAST_RUN_KEY, now_txt)
                    save_setting(
                        pg,
                        EMAIL_REMINDERS_AUTO_LAST_RESULT_KEY,
                        (
                            f"sent={stats.get('sent', 0)}; pending={stats.get('pending', 0)}; "
                            f"skipped_no_email={stats.get('skipped_no_email', 0)}; failed={stats.get('failed', 0)}"
                        ),
                    )
                    st.success(
                        f"Reminder cycle complete. Sent {int(stats.get('sent', 0))}, "
                        f"Skipped {int(stats.get('skipped_no_email', 0))}, Failed {int(stats.get('failed', 0))}."
                    )
            except Exception as exc:
                st.error(f"Reminder cycle failed: {exc}")

        st.caption(
            "This toggle must be enabled for both in-app automation and the Windows scheduled task. "
            "Reminder runs now catch members due within 7 days and any member already past due if they have not been emailed yet."
        )
        st.caption(f"Last run: {_format_datetime_12h(auto_last_run) or 'never'}")
        if auto_last_result:
            st.caption(f"Last result: {auto_last_result}")

    with st.expander("SMS Config (Twilio)", expanded=False):
        try:
            sms = load_sms_settings(pg)
        except Exception:
            sms = {
                "provider": "twilio",
                "account_sid": "",
                "auth_token": "",
                "from_number": "",
                "default_country_code": "+1",
            }

        st.caption("Set up Twilio to send text messages from the Members page.")
        sid = st.text_input("Twilio Account SID", value=sms["account_sid"], key="cfg_sms_sid")
        token = st.text_input("Twilio Auth Token", type="password", key="cfg_sms_token")
        from_number = st.text_input(
            "Twilio From Number (E.164)",
            value=sms["from_number"],
            key="cfg_sms_from",
            help="Example: +16135551234",
        )
        default_cc = st.text_input(
            "Default country code",
            value=sms.get("default_country_code", "+1"),
            key="cfg_sms_default_cc",
            help="Used to normalize 10-digit phone numbers.",
        )

        s1, s2 = st.columns(2)
        if s1.button("Save / Update SMS Config"):
            try:
                save_setting(pg, "sms_provider", "twilio")
                save_setting(pg, "twilio_account_sid", sid.strip())
                save_setting(pg, "twilio_from_number", from_number.strip())
                save_setting(pg, "sms_default_country_code", (default_cc.strip() or "+1"))
                if token.strip():
                    save_setting(pg, "twilio_auth_token", encrypt_secret(token.strip()))
                st.success("SMS settings saved.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

        st.divider()
        test_to = st.text_input("Test phone", key="cfg_sms_test_to", help="Example: +16135559876")
        if st.button("Send Test Text"):
            account_sid = sid.strip() or sms["account_sid"]
            auth_token = token.strip() or sms["auth_token"]
            sender = from_number.strip() or sms["from_number"]
            country = (default_cc.strip() or sms.get("default_country_code", "+1"))
            recipient = _phone_to_e164(test_to.strip(), country)

            if not account_sid or not auth_token or not sender or not recipient:
                st.warning("Set account SID, auth token, from number, and valid test phone before sending.")
            else:
                try:
                    send_sms_twilio(
                        account_sid,
                        auth_token,
                        sender,
                        recipient,
                        "Liberty Smokes SMS test from your dashboard.",
                    )
                    st.success(f"Test text sent to {recipient}.")
                except Exception as exc:
                    st.error(f"Test failed: {exc}")

        if s2.button("Clear Saved SMS Config"):
            try:
                clear_setting(pg, "sms_provider")
                clear_setting(pg, "twilio_account_sid")
                clear_setting(pg, "twilio_auth_token")
                clear_setting(pg, "twilio_from_number")
                clear_setting(pg, "sms_default_country_code")
                st.session_state["cfg_sms_sid"] = ""
                st.session_state["cfg_sms_token"] = ""
                st.session_state["cfg_sms_from"] = ""
                st.session_state["cfg_sms_default_cc"] = "+1"
                st.success("SMS settings cleared.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed: {exc}")

    with st.expander("CigarPOS Integration", expanded=False):
        cfg = load_cigarpos_settings(pg)
        base_url = st.text_input(
            "CigarPOS URL",
            value=cfg.get("base_url", ""),
            key="cpos_base_url",
            help="Example: https://libertysmokes.cigarspos.com",
        )
        if base_url.strip():
            st.markdown(f"Open CigarPOS Portal: [{base_url.strip()}]({base_url.strip()})")
        username = st.text_input(
            "CigarPOS Username",
            value=cfg.get("username", ""),
            key="cpos_username",
        )
        saved_pw = cfg.get("password", "")
        password = st.text_input(
            "CigarPOS Password",
            value="",
            type="password",
            key="cpos_password",
            help="Leave blank to keep the currently stored password.",
        )
        auto_sync = st.checkbox(
            "Enable auto sync",
            value=bool(cfg.get("auto_sync", False)),
            key="cpos_auto_sync",
        )
        auto_sync_min = st.number_input(
            "Auto sync interval (minutes)",
            min_value=5,
            max_value=1440,
            value=int(cfg.get("auto_sync_min", 60)),
            step=5,
            key="cpos_auto_sync_min",
        )
        merge_remote = st.checkbox(
            "Merge remote inventory by SKU",
            value=True,
            key="cpos_merge_mode",
        )

        if not has_fernet_support():
            st.warning("Install cryptography for encrypted credential storage. Fallback storage is plain text.")

        b1, b2, b3 = st.columns(3)
        if b1.button("Save CigarPOS Settings"):
            try:
                save_cigarpos_settings(
                    pg,
                    base_url=base_url,
                    username=username,
                    password=password or saved_pw,
                    auto_sync=bool(auto_sync),
                    auto_sync_min=int(auto_sync_min),
                )
                st.success("CigarPOS settings saved.")
            except Exception as exc:
                st.error(f"Failed: {exc}")

        if b2.button("Test CigarPOS Connection"):
            ok, msg = test_cigarpos_connection(base_url, username, password or saved_pw)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        if b3.button("Sync Inventory Now"):
            try:
                save_cigarpos_settings(
                    pg,
                    base_url=base_url,
                    username=username,
                    password=password or saved_pw,
                    auto_sync=bool(auto_sync),
                    auto_sync_min=int(auto_sync_min),
                )
                count, when = run_cigarpos_inventory_sync(pg, merge_mode=bool(merge_remote))
                st.success(f"Synced {count} item(s) from CigarPOS at {_format_datetime_12h(when)}.")
            except Exception as exc:
                save_setting(pg, POS_LAST_SYNC_ERROR_KEY, str(exc))
                st.error(f"Sync failed: {exc}")

        with st.expander("Debug: Raw CigarPOS Item Fields", expanded=False):
            st.caption(
                "Fetches the first item from CigarPOS and shows every field it returns. "
                "Use this if stock quantities aren't syncing correctly — look for the field name "
                "that holds the quantity and report it."
            )
            if st.button("Fetch Raw Item Sample", key="cpos_debug_raw"):
                try:
                    session = _cigarpos_login_session(
                        base_url, username, password or saved_pw
                    )
                    base_n = _normalize_base_url(base_url)
                    r = session.get(f"{base_n}/api/items/get", timeout=30)
                    if r.status_code == 405:
                        r = session.post(
                            f"{base_n}/api/items/get",
                            data={"data": json.dumps({})},
                            timeout=30,
                        )
                    r.raise_for_status()
                    raw_payload = r.json()
                    raw_rows = _extract_remote_items_payload(raw_payload.get("data"))
                    if raw_rows:
                        first_item = raw_rows[0] if isinstance(raw_rows[0], dict) else {}
                        stock_keys = [
                            "total_stock",
                            "total stock",
                            "seprate_total_stock",
                            "seprate total stock",
                            "qty",
                            "quantity",
                            "stock",
                            "onhand",
                            "on_hand",
                            "qoh",
                            "qtyonhand",
                            "qty_on_hand",
                            "stockqty",
                            "stock_qty",
                            "quantityonhand",
                            "quantity_on_hand",
                            "qty on hand",
                            "quantity on hand",
                            "qtyinstock",
                            "currentstock",
                            "available_qty",
                        ]
                        norm_first = {str(k or "").strip().lower(): v for k, v in first_item.items()}
                        chosen_stock_key = ""
                        chosen_stock_value = None
                        for key in stock_keys:
                            if key in norm_first and norm_first.get(key) not in (None, ""):
                                val = norm_first.get(key)
                                if isinstance(val, bool):
                                    continue
                                if isinstance(val, str) and val.strip().lower() in {"true", "false", "yes", "no", "y", "n", "t", "f"}:
                                    continue
                                chosen_stock_key = key
                                chosen_stock_value = val
                                break
                        st.write("**Fields returned by CigarPOS for the first item:**")
                        field_rows = [
                            {"Field": k, "Value": str(v)}
                            for k, v in sorted(first_item.items())
                        ]
                        st.dataframe(field_rows, width="stretch", hide_index=True)
                        if chosen_stock_key:
                            st.caption(
                                f"Stock field selected by mapper: {chosen_stock_key} = {chosen_stock_value}"
                            )
                        else:
                            st.caption("Stock field selected by mapper: none (defaults to 0)")
                        mapped = _map_remote_item_to_inventory(first_item)
                        if mapped:
                            st.write("**How it maps to local inventory:**")
                            st.json(mapped)
                    else:
                        st.info("No items returned from CigarPOS.")
                except Exception as exc:
                    st.error(f"Debug fetch failed: {exc}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    st.title("Liberty Smokes Central Dashboard")
    st.session_state.setdefault("mobile_mode", True)
    apply_mobile_styles(bool(st.session_state.get("mobile_mode", True)))
    pending_global_reset = st.session_state.pop(GLOBAL_PENDING_WIDGET_RESET_KEY, None)
    if isinstance(pending_global_reset, dict):
        reset_widget_state(pending_global_reset)
    pending_nav = st.session_state.pop("nav_page_next", None)
    if pending_nav:
        st.session_state["nav_page"] = pending_nav
    pg = get_postgrest_client()
    if "show_pos_nav" not in st.session_state:
        raw_show_pos = str(get_setting(pg, NAV_SHOW_POS_KEY) or "1").strip().lower()
        st.session_state["show_pos_nav"] = raw_show_pos in {"1", "true", "yes", "y", "on"}
    if "show_scanner_nav" not in st.session_state:
        raw_show_scanner = str(get_setting(pg, NAV_SHOW_SCANNER_KEY) or "1").strip().lower()
        st.session_state["show_scanner_nav"] = raw_show_scanner in {"1", "true", "yes", "y", "on"}

    with st.sidebar:
        logo_path = get_sidebar_logo_path()
        if logo_path is not None:
            st.image(str(logo_path), width="stretch")
        show_pos_nav = bool(st.session_state.get("show_pos_nav", True))
        show_scanner_nav = bool(st.session_state.get("show_scanner_nav", True))

        nav_pages = ["Seats", "Members", "Sales Ledger", "Schedule", "Ordering"]
        if show_pos_nav:
            nav_pages.append("POS")
        if show_scanner_nav:
            nav_pages.append("Scanner")
        nav_pages.append("Settings")
        if st.session_state.get("nav_page") not in nav_pages:
            st.session_state["nav_page"] = "Seats"
        page = st.radio(
            "Navigate",
            nav_pages,
            key="nav_page",
            label_visibility="collapsed",
        )

    if page == "Seats":
        page_seats(pg)
    elif page == "Members":
        page_members(pg)
    elif page == "Sales Ledger":
        page_sales_ledger(pg)
    elif page == "Schedule":
        page_schedule(pg)
    elif page == "Ordering":
        page_ordering(pg)
    elif page == "POS":
        page_pos(pg)
    elif page == "Scanner":
        page_scanner(pg)
    elif page == "Settings":
        page_settings(pg)


if __name__ == "__main__":
    main()
