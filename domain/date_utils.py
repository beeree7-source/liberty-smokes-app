import calendar
import datetime


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


def _parse_iso_date(value: str | None) -> datetime.date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def member_active_months(member: dict, as_of: datetime.date | None = None) -> int:
    join_date = _parse_iso_date(member.get("join_date"))
    if not join_date:
        return 0
    as_of = as_of or datetime.date.today()
    if join_date > as_of:
        return 0
    months = (as_of.year - join_date.year) * 12 + (as_of.month - join_date.month) + 1
    if as_of.day < join_date.day:
        months -= 1
    return max(1, months)


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
