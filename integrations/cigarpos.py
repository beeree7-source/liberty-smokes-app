"""CigarPOS integration, credential, and inventory synchronization helpers."""

import base64
import datetime
import hashlib
import importlib
import json

import requests
import streamlit as st
from postgrest import SyncPostgrestClient

from data.settings import clear_setting, get_setting, save_setting
from pos.local import load_pos_inventory, save_pos_inventory
from pos.loyalty import _truthy


POS_LAST_SYNC_KEY = "pos_last_sync_v1"
POS_LAST_SYNC_ERROR_KEY = "pos_last_sync_error_v1"
CIGARPOS_BASE_URL_KEY = "cigarpos_base_url"
CIGARPOS_USERNAME_KEY = "cigarpos_username"
CIGARPOS_PASSWORD_KEY = "cigarpos_password_enc"
CIGARPOS_AUTO_SYNC_KEY = "cigarpos_auto_sync_enabled"
CIGARPOS_AUTO_SYNC_MIN_KEY = "cigarpos_auto_sync_min"
CIGARPOS_SALES_LAST_SYNC_KEY = "cigarpos_sales_last_sync_v1"


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
    key = str(st.secrets.get("APP_ENCRYPTION_KEY") or "").strip().encode("utf-8")
    if not key:
        return None
    try:
        return fernet_cls(key)
    except Exception:
        return None


def _legacy_fernet_from_service_key():
    """Read values encrypted before APP_ENCRYPTION_KEY was introduced."""
    try:
        fernet_mod = importlib.import_module("cryptography.fernet")
        fernet_cls = getattr(fernet_mod, "Fernet", None)
    except Exception:
        return None
    if fernet_cls is None:
        return None
    secret_seed = str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not secret_seed:
        return None
    key = base64.urlsafe_b64encode(
        hashlib.sha256((secret_seed + "|cigarpos-sync").encode("utf-8")).digest()
    )
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
        raise RuntimeError(
            "APP_ENCRYPTION_KEY must be configured before saving credentials. "
            "Generate one with Fernet.generate_key() and store it only in Streamlit secrets."
        )
    token = f.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    return "fernet:" + token


def decrypt_secret(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    if cipher_text.startswith("plain:"):
        return cipher_text[len("plain:"):]
    if cipher_text.startswith("fernet:"):
        token = cipher_text[len("fernet:"):]
        for fernet in (_fernet_from_app_secret(), _legacy_fernet_from_service_key()):
            if fernet is None:
                continue
            try:
                return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except Exception:
                continue
        return ""
    # Existing plaintext settings remain readable so they can be re-saved encrypted.
    return cipher_text


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
