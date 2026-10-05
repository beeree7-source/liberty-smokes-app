"""POS loyalty settings, contacts, and member-linking helpers."""

import datetime
import hashlib
import json

from postgrest import SyncPostgrestClient

from data.settings import (
    _load_json_list_setting,
    _save_json_list_setting,
    get_setting,
    save_setting,
)
from domain.date_utils import advance_billing


POS_LOYALTY_SETTINGS_KEY = "pos_loyalty_settings_v1"
POS_LOYALTY_POINTS_KEY = "pos_loyalty_points_v1"
POS_LOYALTY_CUSTOMERS_KEY = "pos_loyalty_customers_v1"


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

    from app import fetch_members

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
