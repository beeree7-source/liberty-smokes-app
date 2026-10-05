"""Local POS catalog, inventory, promotion, and scanner helpers."""

import csv
import datetime
import hashlib
import io
import json

from postgrest import SyncPostgrestClient

from data.settings import (
    _load_json_list_setting,
    _save_json_list_setting,
)


POS_INVENTORY_KEY = "pos_inventory_v1"
POS_PROMOTIONS_KEY = "pos_promotions_v1"
POS_SALES_KEY = "pos_sales_v1"
POS_CUSTOMER_GROUPS_KEY = "pos_customer_groups_v1"
DRINK_CATALOG_KEY = "drink_catalog_v1"


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


def complete_pos_sale(pg: SyncPostgrestClient, cart: list[dict], sale: dict) -> tuple[list[dict], list[dict]]:
    """Atomically validate stock, deduct it, and persist the POS sale in Supabase."""
    try:
        response = pg.rpc(
            "complete_pos_sale",
            {"p_cart": cart, "p_sale": sale},
        ).execute()
    except Exception as exc:
        message = str(exc)
        if "complete_pos_sale" in message and "does not exist" in message:
            raise RuntimeError(
                "POS checkout is not configured. Run supabase/harden_production_access.sql "
                "in the Supabase SQL editor before accepting sales."
            ) from exc
        raise

    payload = response.data
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        raise RuntimeError("Checkout transaction returned an invalid response.")

    inventory = payload.get("inventory")
    sales = payload.get("sales")
    if not isinstance(inventory, list) or not isinstance(sales, list):
        raise RuntimeError("Checkout transaction returned incomplete data.")
    return inventory, sales


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
